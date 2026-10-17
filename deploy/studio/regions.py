import base64
import contextlib
import functools
import hashlib
import hmac
import html
import io
import json
import math
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageStat

from .config import StudioSettings
from .models3d import format_display_timestamp
from .store import (
    ensure_safe_subpath,
    read_bytes_no_follow,
    user_librechat_images_dir,
    user_studio_dir,
    validate_user_id,
)

# 选区形状点数上限、形状数量上限、最小有效选区像素数与标识符正则
MAX_POINTS = 4096
MAX_SHAPES = 64
MIN_SELECTED_PIXELS = 16
MAX_IMAGE_PIXELS = 24_000_000
SELECTION_ID_RE = re.compile(r"^sel_[0-9a-f]{32}$")
JOB_ID_RE = re.compile(r"^img_[0-9a-f]{32}$")
REGION_TOKEN_RE = re.compile(r"^([A-Za-z0-9_-]{16,512})\.([0-9a-f]{64})$")
SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
SIZES: dict[str, int] = {"1K": 1024, "2K": 2048}

# 与Mac插件保持完全一致的SAM 2.1 hiera-tiny模型仓库、固定revision与safetensors权重SHA256
MODEL_REPO = "facebook/sam2.1-hiera-tiny"
MODEL_REVISION = "de431c4043854a71d8101e17995dfe596bf101a5"
MODEL_SHA256 = "48c14467e5cf9e51870511feb72c89688e82dd74523142c0538b663e193ac2a7"
MODEL_FILE_SHA256S: dict[str, str] = {
    "model.safetensors": MODEL_SHA256,
    "config.json": "860aff9751b139d83a4ad7df1e5535416fded533e0ead02625edbefcb9953cce",
    "preprocessor_config.json": "6ebf229ee259368ce4a8d4f2fe893a72b053023710853e257253939e601f583d",
    "processor_config.json": "f8a68e865cfad115c1c2763f3d93eca7b1c622da06da2a9273eb437fb2389b6d",
}
DEFAULT_SAM2_MODEL_DIR = Path("/opt/studio-models/sam2.1-hiera-tiny")
DEFAULT_SAM2_ONNX_DIR = DEFAULT_SAM2_MODEL_DIR
DISALLOWED_WEIGHT_SUFFIXES = (".bin", ".pt", ".pth", ".ckpt", ".pkl", ".pickle")


# 校验归一化选区坐标值是否位于0到999之间并四舍五入为整数
def _coord(value: Any) -> int:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not 0 <= value <= 999
    ):
        raise ValueError("Selection coordinate is out of range [0, 999]")
    return round(float(value))


# 校验并规范化选区形状结构（支持rect、polygon、stroke及add/subtract布尔运算）
def selection_value(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("version", 1) != 1:
        raise ValueError("Invalid selection format or version")
    shapes = value.get("shapes")
    if not isinstance(shapes, list) or not 1 <= len(shapes) <= MAX_SHAPES:
        raise ValueError("Selection must contain between 1 and 64 shapes")
    normalized: list[dict[str, Any]] = []
    total_points = 0
    for shape in shapes:
        if type(shape) is not dict:
            raise ValueError("Invalid selection shape object")
        kind = shape.get("type")
        op = shape.get("op", "add")
        points = shape.get("points")
        if (
            kind not in ("rect", "polygon", "stroke")
            or op not in ("add", "subtract")
            or not isinstance(points, list)
        ):
            raise ValueError("Unsupported selection shape type or operation")
        total_points += len(points)
        if total_points > MAX_POINTS:
            raise ValueError(
                "Selection point count exceeds limit; please simplify the selection"
            )
        coords: list[dict[str, int]] = []
        for point in points:
            if type(point) is not dict:
                raise ValueError("Invalid selection point object")
            coords.append({"x": _coord(point.get("x")), "y": _coord(point.get("y"))})
        if kind == "rect":
            if len(coords) != 2:
                raise ValueError("Rectangle selection requires exactly 2 points")
            x1 = min(coords[0]["x"], coords[1]["x"])
            y1 = min(coords[0]["y"], coords[1]["y"])
            x2 = max(coords[0]["x"], coords[1]["x"])
            y2 = max(coords[0]["y"], coords[1]["y"])
            if x2 <= x1 or y2 <= y1:
                raise ValueError("Selected rectangle is too small")
            normalized.append(
                {
                    "type": "rect",
                    "op": op,
                    "points": [{"x": x1, "y": y1}, {"x": x2, "y": y2}],
                }
            )
        elif kind == "polygon":
            if not 3 <= len(coords) <= 1024:
                raise ValueError("Polygon selection requires between 3 and 1024 points")
            dedup = [
                pt for idx, pt in enumerate(coords) if idx == 0 or pt != coords[idx - 1]
            ]
            if len(dedup) < 3:
                raise ValueError(
                    "Polygon selection requires at least 3 distinct points"
                )
            normalized.append({"type": "polygon", "op": op, "points": dedup})
        else:
            radius = shape.get("radius", 18)
            if (
                not isinstance(radius, (int, float))
                or isinstance(radius, bool)
                or not 1 <= radius <= 150
            ):
                raise ValueError("Brush stroke radius must be between 1 and 150")
            if not 1 <= len(coords) <= 1024:
                raise ValueError(
                    "Brush stroke selection requires between 1 and 1024 points"
                )
            dedup = [
                pt for idx, pt in enumerate(coords) if idx == 0 or pt != coords[idx - 1]
            ]
            normalized.append(
                {
                    "type": "stroke",
                    "op": op,
                    "radius": round(float(radius)),
                    "points": dedup,
                }
            )
    if not any(item["op"] == "add" for item in normalized):
        raise ValueError("Selection must include at least one additive shape")
    return {"version": 1, "shapes": normalized}


# 将归一化选区结构光栅化为目标像素尺寸的单通道二值掩码图像
def mask_for(selection: dict[str, Any], size: tuple[int, int]) -> Image.Image:
    normalized = selection_value(selection)
    if not isinstance(size, (tuple, list)) or len(size) != 2:
        raise ValueError("Invalid target image size for mask rasterization")
    w, h = int(size[0]), int(size[1])
    if w < 8 or h < 8:
        raise ValueError("Target image dimensions are too small for local selection")
    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)

    # 将0到999归一化坐标映射为目标图像像素坐标
    def pt(point: dict[str, int]) -> tuple[int, int]:
        return (
            round(point["x"] / 999 * (w - 1)),
            round(point["y"] / 999 * (h - 1)),
        )

    for shape in normalized["shapes"]:
        fill = 255 if shape["op"] == "add" else 0
        kind = shape["type"]
        if kind == "rect":
            x1, y1 = pt(shape["points"][0])
            x2, y2 = pt(shape["points"][1])
            draw.rectangle(
                (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)),
                fill=fill,
            )
        elif kind == "polygon":
            draw.polygon([pt(p) for p in shape["points"]], fill=fill)
        elif kind == "stroke":
            radius = max(1, round(shape["radius"] / 999 * min(w, h)))
            coords = [pt(p) for p in shape["points"]]
            if len(coords) > 1:
                draw.line(coords, fill=fill, width=radius * 2, joint="curve")
            for cx, cy in coords:
                draw.ellipse(
                    (cx - radius, cy - radius, cx + radius, cy + radius),
                    fill=fill,
                )
    if not mask.getbbox() or sum(mask.histogram()[1:]) < MIN_SELECTED_PIXELS:
        raise ValueError(
            "Selected region is too small; please expand the selection area"
        )
    return mask


# 打开图像并按EXIF方向自动摆正（识别调色板tRNS透明与Alpha通道并保留ICC色彩配置）
def open_image(
    path: str | Path | bytes | bytearray | io.BytesIO,
    *,
    keep_alpha: bool = False,
) -> Image.Image:
    source = io.BytesIO(path) if isinstance(path, (bytes, bytearray)) else path
    with Image.open(source) as raw:
        if raw.width * raw.height > MAX_IMAGE_PIXELS:
            raise ValueError("Image pixel count exceeds safety limit")
        icc = raw.info.get("icc_profile")
        has_alpha = "A" in raw.getbands() or "transparency" in raw.info
        image = ImageOps.exif_transpose(raw)
        has_alpha = has_alpha or "A" in image.getbands() or "transparency" in image.info
        if keep_alpha and has_alpha:
            out = image.convert("RGBA")
        elif has_alpha:
            rgba = image.convert("RGBA")
            background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            out = Image.alpha_composite(background, rgba).convert("RGB")
        else:
            out = image.convert("RGB")
        if icc:
            out.info["icc_profile"] = icc
        return out


# 将PIL图像编码为PNG格式的Data URI字符串
def image_data_uri(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


# 计算选区掩码周围带上下文边距的局部裁切框（极端长宽比时回退至整图）
def context_box(mask: Image.Image) -> tuple[int, int, int, int]:
    box = mask.getbbox()
    if box is None:
        raise ValueError("Selection mask is empty")
    x1, y1, x2, y2 = box
    w, h = mask.size
    margin = max(32, round(max(x2 - x1, y2 - y1) * 0.25))
    crop = (
        max(0, x1 - margin),
        max(0, y1 - margin),
        min(w, x2 + margin),
        min(h, y2 + margin),
    )
    ratio = (crop[2] - crop[0]) / max(1, crop[3] - crop[1])
    return (0, 0, w, h) if ratio > 8 or ratio < 1 / 8 else crop


# 计算选区在局部裁切框内部的0到999归一化包围盒坐标
def region_in_crop(
    selection: dict[str, Any],
    size: tuple[int, int],
    crop: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    mask = mask_for(selection, size).crop(crop)
    w, h = crop[2] - crop[0], crop[3] - crop[1]
    box = mask.getbbox()
    if box is None or w < 1 or h < 1:
        raise ValueError("Selected region falls outside the local crop box")
    x1, y1, x2, y2 = box
    return (
        round(x1 / max(1, w - 1) * 999),
        round(y1 / max(1, h - 1) * 999),
        round(max(x1 + 1, x2 - 1) / max(1, w - 1) * 999),
        round(max(y1 + 1, y2 - 1) / max(1, h - 1) * 999),
    )


# 提取原图在选区周边的局部上下文裁切图及选区在裁切图内的归一化包围盒
def local_context(path: str | Path, selection: dict[str, Any]) -> dict[str, Any]:
    image = open_image(path)
    mask = mask_for(selection, image.size)
    crop = context_box(mask)
    cropped = image.crop(crop)
    return {
        "data_uri": image_data_uri(cropped),
        "crop": crop,
        "size": (crop[2] - crop[0], crop[3] - crop[1]),
        "region": region_in_crop(selection, image.size, crop),
        "base_size": image.size,
    }


# 按局部裁切图宽高比计算适配生图接口的16像素对齐目标宽高并满足2.5%宽高比误差上限
def source_dimensions(size: str, source_size: tuple[int, int]) -> tuple[int, int]:
    if (
        size not in SIZES
        or not isinstance(source_size, (tuple, list))
        or len(source_size) != 2
    ):
        raise ValueError("Invalid target size tier or crop dimensions")
    w, h = int(source_size[0]), int(source_size[1])
    if w < 1 or h < 1 or not (1 / 16 <= w / h <= 16):
        raise ValueError("Crop aspect ratio is outside the supported range")
    ratio = w / h
    target_area = SIZES[size] ** 2
    scale = math.sqrt(target_area / (w * h))
    base_w = max(16, round(w * scale / 16) * 16)
    base_h = max(16, round(h * scale / 16) * 16)
    if (
        921600 <= base_w * base_h <= 4624220
        and 1 / 16 <= base_w / base_h <= 16
        and abs((base_w / base_h) / ratio - 1.0) <= 0.025
    ):
        return base_w, base_h

    best_pair: tuple[int, int] | None = None
    best_key: tuple[float, float] | None = None
    for dw in range(-20, 21):
        cw = base_w + dw * 16
        if cw < 16:
            continue
        for dh in range(-20, 21):
            ch = base_h + dh * 16
            if ch < 16:
                continue
            area = cw * ch
            if not (921600 <= area <= 4624220):
                continue
            cand_ratio = cw / ch
            if not (1 / 16 <= cand_ratio <= 16):
                continue
            discrepancy = abs(cand_ratio / ratio - 1.0)
            if discrepancy > 0.025:
                continue
            key = (discrepancy, float(abs(area - target_area)))
            if best_key is None or key < best_key:
                best_key = key
                best_pair = (cw, ch)

    if best_pair is None:
        raise ValueError("Unable to compute valid generation dimensions for crop ratio")
    return best_pair


# 生成选区与底图绑定的加密校验凭据（包含底图文件SHA256与掩码像素SHA256）
def selection_proof(
    path: str | Path | bytes | bytearray,
    selection: dict[str, Any],
) -> dict[str, Any]:
    if isinstance(path, (bytes, bytearray)):
        raw_bytes = bytes(path)
        image = open_image(raw_bytes)
    else:
        resolved_path = Path(path)
        raw_bytes = resolved_path.read_bytes()
        image = open_image(raw_bytes)
    mask = mask_for(selection, image.size)
    return {
        "image_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "mask_sha256": hashlib.sha256(mask.tobytes()).hexdigest(),
        "width": image.width,
        "height": image.height,
        "selected_pixels": int(sum(mask.histogram()[1:])),
    }


# 将参考对象的色彩均值与标准差温和对齐至底图选区环境色调
def match_color(
    source: Image.Image,
    reference: Image.Image,
    mask: Image.Image | None = None,
) -> Image.Image:
    rgb = source.convert("RGB")
    ref = reference.convert("RGB")
    src_alpha = source.getchannel("A") if "A" in source.getbands() else None
    src_mask = (
        src_alpha.point(lambda p: 255 if p > 24 else 0)
        if src_alpha is not None
        else None
    )
    ref_mask = mask.point(lambda p: 255 if p > 0 else 0) if mask is not None else None

    src_stat = ImageStat.Stat(rgb, mask=src_mask)
    ref_stat = ImageStat.Stat(ref, mask=ref_mask)
    if sum(src_stat.count) == 0 or sum(ref_stat.count) == 0:
        return source

    channels = list(rgb.split())
    adjusted_channels: list[Image.Image] = []
    for idx, ch in enumerate(channels):
        src_mean = float(src_stat.mean[idx])
        ref_mean = float(ref_stat.mean[idx])
        src_std = max(float(src_stat.stddev[idx]), 12.0)
        ref_std = float(ref_stat.stddev[idx])
        gain = min(1.18, max(0.82, ref_std / src_std))
        shift = max(-28.0, min(28.0, ref_mean - src_mean))
        lut = [
            max(
                0,
                min(
                    255,
                    round(
                        val * 0.65 + ((val - src_mean) * gain + src_mean + shift) * 0.35
                    ),
                ),
            )
            for val in range(256)
        ]
        adjusted_channels.append(ch.point(lut))

    merged = Image.merge("RGB", adjusted_channels)
    if src_alpha is not None:
        merged.putalpha(src_alpha)
        return merged
    return merged


# 提取参考对象前景包围盒区域（若存在有效透明通道则自动裁掉透明留白）
def _alpha_box(image: Image.Image) -> tuple[int, int, int, int]:
    if "A" not in image.getbands():
        return (0, 0, image.width, image.height)
    alpha = image.getchannel("A")
    lo, hi = alpha.getextrema()
    if hi <= 16 or lo >= 250:
        return (0, 0, image.width, image.height)
    box = alpha.point(lambda p: 255 if p > 16 else 0).getbbox()
    return box or (0, 0, image.width, image.height)


# 将参考对象按原样缩放并贴入底图选区中心，在生图前严格校验完整变换轮廓绝不越出二值选区掩码
def object_placement(
    base_path: str | Path,
    selection: dict[str, Any],
    reference_path: str | Path,
    *,
    reference_selection: dict[str, Any] | None = None,
    target_selection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = open_image(base_path)
    mask = mask_for(selection, base.size)
    mask_arr = np.asarray(mask, dtype=np.uint8)
    target_mask = mask_for(target_selection, base.size) if target_selection else mask
    target_arr = np.asarray(target_mask, dtype=np.uint8)
    if target_selection and bool(np.any((target_arr > 0) & (mask_arr == 0))):
        raise ValueError(
            "Target placement area extends outside the confirmed selection"
        )

    crop = context_box(mask)
    x1, y1, x2, y2 = target_mask.getbbox()
    target_w = max(8, x2 - x1)
    target_h = max(8, y2 - y1)

    source = open_image(reference_path, keep_alpha=True).convert("RGBA")
    if reference_selection:
        source_mask = mask_for(reference_selection, source.size)
        box = source_mask.getbbox()
        source = source.crop(box)
        alpha = ImageChops.multiply(source.getchannel("A"), source_mask.crop(box))
        source.putalpha(alpha)
    else:
        source = source.crop(_alpha_box(source))

    source = match_color(
        source,
        base.crop((x1, y1, x2, y2)),
        target_mask.crop((x1, y1, x2, y2)),
    )
    scale = min(
        target_w * 0.92 / max(1, source.width),
        target_h * 0.92 / max(1, source.height),
    )
    placed_w = max(8, round(source.width * scale))
    placed_h = max(8, round(source.height * scale))
    resized = source.resize((placed_w, placed_h), Image.Resampling.LANCZOS)

    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    paste_x = x1 + (target_w - placed_w) // 2
    paste_y = y1 + (target_h - placed_h) // 2
    layer.paste(resized, (paste_x, paste_y))

    # 在做掩码裁剪前检查变换后对象的全部非透明像素，若越出不规则或镂空选区则立即拒绝
    footprint = np.asarray(layer.getchannel("A"), dtype=np.uint8)
    if not bool(np.any(footprint > 0)):
        raise ValueError("Reference object has no visible foreground pixels")
    if target_selection and bool(np.any((footprint > 0) & (target_arr == 0))):
        raise ValueError(
            "Original object placement extends outside the target placement area"
        )
    if bool(np.any((footprint > 0) & (mask_arr == 0))):
        raise ValueError(
            "Original object placement extends outside the confirmed selection"
        )

    feather = mask.filter(ImageFilter.GaussianBlur(1))
    alpha = ImageChops.multiply(layer.getchannel("A"), feather)
    canvas = base.convert("RGBA")
    canvas.paste(layer, (0, 0), alpha)
    return {
        "data_uri": image_data_uri(canvas.crop(crop).convert("RGB")),
        "crop": crop,
        "size": (crop[2] - crop[0], crop[3] - crop[1]),
        "region": region_in_crop(selection, base.size, crop),
        "base_size": base.size,
        "instruction": (
            "图1已将选定参考对象按原样放入选区。仅融合对象边缘、接触阴影、透视与环境光，"
            "保持对象花色、文字、图案、比例与主体细节不变，不重绘选区外内容。"
        ),
    }


# 统计两张同尺寸同模式图像在选区掩码外部（mask==0）发生像素变化的像素点总数
def count_outside_changes(
    before_img: Image.Image,
    after_img: Image.Image,
    mask: Image.Image,
) -> int:
    if before_img.size != after_img.size or before_img.size != mask.size:
        raise ValueError("Image and mask dimensions must match for pixel verification")
    if before_img.mode != after_img.mode:
        raise ValueError("Image modes must match for pixel verification")
    diff = ImageChops.difference(before_img, after_img)
    channel_max = functools.reduce(ImageChops.lighter, diff.split())
    outside_mask = mask.point(lambda val: 0 if val else 255)
    outside_diff = ImageChops.multiply(channel_max, outside_mask)
    return int(sum(outside_diff.histogram()[1:]))


# 以0600权限将图像原子保存为无损PNG文件并返回SHA256摘要
def save_exact(image: Image.Image, destination: str | Path) -> str:
    dest_path = Path(destination)
    dest_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    buffer = io.BytesIO()
    save_kwargs: dict[str, Any] = {"format": "PNG"}
    icc = image.info.get("icc_profile")
    if icc:
        save_kwargs["icc_profile"] = icc
    image.save(buffer, **save_kwargs)
    raw_bytes = buffer.getvalue()
    temp_path = dest_path.with_name(f"{dest_path.name}.part.{secrets.token_hex(6)}")
    descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, dest_path)
    except BaseException:
        with contextlib.suppress(OSError):
            temp_path.unlink(missing_ok=True)
        raise
    return hashlib.sha256(raw_bytes).hexdigest()


# 将局部生成结果按内向羽化掩码合回底图并严格校验选区外每一像素字节零改动
def composite_local(
    base_path: str | Path,
    generated_path: str | Path,
    selection: dict[str, Any],
    crop: tuple[int, int, int, int],
    destination: str | Path,
    *,
    expected_proof: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base_bytes = Path(base_path).read_bytes()
    if expected_proof is not None:
        current_proof = selection_proof(base_bytes, selection)
        for key in (
            "image_sha256",
            "mask_sha256",
            "width",
            "height",
            "selected_pixels",
        ):
            if current_proof.get(key) != expected_proof.get(key):
                raise ValueError(
                    f"Selection proof mismatch on {key} before local composite; refusing to composite"
                )
    original = open_image(base_bytes, keep_alpha=True)
    generated = open_image(generated_path, keep_alpha=True)
    mask = mask_for(selection, original.size)
    x1, y1, x2, y2 = (int(crop[0]), int(crop[1]), int(crop[2]), int(crop[3]))
    if not (0 <= x1 < x2 <= original.width and 0 <= y1 < y2 <= original.height):
        raise ValueError("Invalid local crop coordinates for base image")
    crop_ratio = (x2 - x1) / (y2 - y1)
    generated_ratio = generated.width / generated.height
    if abs(generated_ratio / crop_ratio - 1) > 0.025:
        raise ValueError(
            "Generated crop aspect ratio does not match the selected crop region"
        )

    changed = original.copy()
    patch = generated.convert(original.mode).resize(
        (x2 - x1, y2 - y1), Image.Resampling.LANCZOS
    )
    changed.paste(patch, (x1, y1))

    # 仅在选区内部做1-4像素内向羽化，通过与二值mask相乘保证mask==0处alpha严格为0
    radius = max(1, min(4, round(min(original.size) / 600)))
    alpha = ImageChops.multiply(mask, mask.filter(ImageFilter.GaussianBlur(radius)))
    result = Image.composite(changed, original, alpha)
    if original.info.get("icc_profile"):
        result.info["icc_profile"] = original.info["icc_profile"]

    changed_outside = count_outside_changes(original, result, mask)
    if changed_outside != 0:
        raise RuntimeError(
            f"Local composite modified {changed_outside} pixels outside the selection"
        )

    dest_path = Path(destination)
    sha256 = save_exact(result, dest_path)

    # 重新从磁盘读取落盘后的PNG文件，二次验证尺寸、模式及RGBA空间选区外像素100%字节级无改动
    saved = open_image(dest_path, keep_alpha=True)
    if (
        saved.size != original.size
        or saved.mode != original.mode
        or count_outside_changes(original, saved, mask) != 0
        or count_outside_changes(original.convert("RGBA"), saved.convert("RGBA"), mask)
        != 0
    ):
        with contextlib.suppress(OSError):
            dest_path.unlink(missing_ok=True)
        raise RuntimeError(
            "Saved local edit image failed byte-identical verification outside the selection"
        )

    return {
        "path": dest_path,
        "width": result.width,
        "height": result.height,
        "sha256": sha256,
        "selected_pixels": int(sum(mask.histogram()[1:])),
        "outside_changed_pixels": 0,
    }


# 以0600权限和fsync原子写入局部重绘待合成恢复记录local_recovery.json
def write_local_recovery(directory: Path, values: dict[str, Any]) -> None:
    target = Path(directory) / "local_recovery.json"
    temporary = Path(directory) / f"local_recovery.tmp.{secrets.token_hex(4)}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(values, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise


# 恢复已完成付费裁切但本地合成中断的待合成任务，校验裁块与底图凭据后合回original.png且绝不重复计费调用
def finish_region_edit(
    settings: StudioSettings,
    user_id: str,
    job_id: str,
) -> dict[str, Any]:
    uid = validate_user_id(user_id)
    cleaned_job = job_id.strip() if isinstance(job_id, str) else ""
    if not JOB_ID_RE.fullmatch(cleaned_job):
        raise ValueError("Invalid studio job ID for local region recovery")
    studio_dir = user_studio_dir(settings, uid, create=False)
    job_dir = studio_dir / "jobs" / cleaned_job
    if (
        not studio_dir.exists()
        or studio_dir.is_symlink()
        or not job_dir.exists()
        or job_dir.is_symlink()
        or not job_dir.is_dir()
    ):
        raise ValueError(
            f"Recoverable region job not found for current user: {cleaned_job}"
        )
    ensure_safe_subpath(job_dir, studio_dir)

    receipt_path = job_dir / "receipt.json"
    receipt_data: dict[str, Any] = {}
    if receipt_path.is_file() and not receipt_path.is_symlink():
        raw_rec = read_bytes_no_follow(receipt_path, studio_dir, max_bytes=1024 * 1024)
        parsed_rec = json.loads(raw_rec.decode("utf-8"))
        if isinstance(parsed_rec, dict):
            receipt_data = parsed_rec

    recovery_path = job_dir / "local_recovery.json"
    if (
        not recovery_path.exists()
        or recovery_path.is_symlink()
        or not recovery_path.is_file()
    ):
        if (
            receipt_data.get("state") == "saved"
            and (job_dir / "original.png").is_file()
        ):
            orig_path = job_dir / "original.png"
            raw_bytes = read_bytes_no_follow(orig_path, studio_dir)
            return {
                "id": cleaned_job,
                "image_id": cleaned_job,
                "base_id": str(receipt_data.get("base_id") or ""),
                "selection_id": str(receipt_data.get("selection_id") or ""),
                "path": orig_path,
                "raw_bytes": raw_bytes,
                "width": int(receipt_data.get("width") or 0),
                "height": int(receipt_data.get("height") or 0),
                "sha256": str(
                    receipt_data.get("sha256") or hashlib.sha256(raw_bytes).hexdigest()
                ),
                "selected_pixels": int(receipt_data.get("selected_pixels") or 0),
                "outside_changed_pixels": 0,
            }
        raise ValueError(
            f"No recoverable local crop record found for job: {cleaned_job}"
        )

    raw_recovery = read_bytes_no_follow(
        recovery_path, studio_dir, max_bytes=1024 * 1024
    )
    recovery = json.loads(raw_recovery.decode("utf-8"))
    if not isinstance(recovery, dict) or recovery.get("job_id") != cleaned_job:
        raise ValueError("Invalid local recovery metadata")

    crop_file = str(recovery.get("crop_file") or "")
    if crop_file not in ("crop.png", "crop.jpg", "crop.webp"):
        raise ValueError("Invalid saved crop filename in recovery metadata")
    crop_path = job_dir / crop_file
    if not crop_path.exists() or crop_path.is_symlink() or not crop_path.is_file():
        raise ValueError("Saved paid crop file is missing on disk")
    crop_bytes = read_bytes_no_follow(crop_path, studio_dir)
    expected_crop_sha = str(recovery.get("crop_sha256") or "")
    if (
        not SHA256_HEX_RE.fullmatch(expected_crop_sha)
        or hashlib.sha256(crop_bytes).hexdigest() != expected_crop_sha
    ):
        raise ValueError("Saved crop SHA-256 does not match recovery metadata")

    base_path = Path(str(recovery.get("base_path") or ""))
    allowed_roots = (
        user_librechat_images_dir(settings, uid),
        studio_dir,
        Path(settings.kb_root),
    )
    matched_root: Path | None = None
    for root in allowed_roots:
        if not root.exists() or root.is_symlink():
            continue
        try:
            ensure_safe_subpath(base_path, root)
            matched_root = root
            break
        except (OSError, ValueError):
            continue
    if matched_root is None or not base_path.is_file() or base_path.is_symlink():
        raise ValueError(
            "Original base image for recoverable job is no longer accessible"
        )

    raw_crop_box = recovery.get("crop")
    if not isinstance(raw_crop_box, list) or len(raw_crop_box) != 4:
        raise ValueError("Invalid crop coordinates in recovery metadata")
    crop_box = (
        int(raw_crop_box[0]),
        int(raw_crop_box[1]),
        int(raw_crop_box[2]),
        int(raw_crop_box[3]),
    )
    selection = selection_value(recovery.get("selection"))
    expected_proof = (
        recovery.get("proof") if isinstance(recovery.get("proof"), dict) else None
    )
    original_path = job_dir / "original.png"
    comp = composite_local(
        base_path,
        crop_path,
        selection,
        crop_box,
        original_path,
        expected_proof=expected_proof,
    )
    final_bytes = read_bytes_no_follow(original_path, studio_dir)

    receipt_data.update(
        state="saved",
        recoverable=False,
        finished=time.time(),
        width=comp["width"],
        height=comp["height"],
        file=original_path.name,
        sha256=comp["sha256"],
        selected_pixels=comp["selected_pixels"],
        outside_changed_pixels=0,
    )
    receipt_Data_pop = dict(receipt_data)
    receipt_Data_pop.pop("error", None)
    temp_rec = job_dir / f"receipt.tmp.{secrets.token_hex(4)}"
    fd = os.open(temp_rec, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(receipt_Data_pop, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_rec, receipt_path)
    except BaseException:
        with contextlib.suppress(OSError):
            temp_rec.unlink(missing_ok=True)
        raise

    with contextlib.suppress(OSError):
        recovery_path.unlink(missing_ok=True)

    return {
        "id": cleaned_job,
        "image_id": cleaned_job,
        "base_id": str(recovery.get("base_id") or ""),
        "selection_id": str(recovery.get("selection_id") or ""),
        "path": original_path,
        "raw_bytes": final_bytes,
        "width": comp["width"],
        "height": comp["height"],
        "sha256": comp["sha256"],
        "selected_pixels": comp["selected_pixels"],
        "outside_changed_pixels": 0,
    }


# 校验已固定revision与SHA256的本地SAM 2.1 hiera-tiny safetensors模型目录（严禁pickle权重与运行时网络下载）
def verify_sam2_model_dir(
    model_dir: str | Path | None = None,
    *,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    base = Path(
        model_dir
        or os.environ.get("STUDIO_SAM2_MODEL_DIR")
        or os.environ.get("STUDIO_SAM2_ONNX_DIR")
        or DEFAULT_SAM2_MODEL_DIR
    )
    if not base.exists() or base.is_symlink() or not base.is_dir():
        raise ValueError(f"Verified SAM 2.1 model directory not found: {base}")

    for child in base.iterdir():
        if child.is_symlink():
            raise ValueError(
                f"Symlink file is not allowed in SAM 2.1 model directory: {child}"
            )
        if child.suffix.lower() in DISALLOWED_WEIGHT_SUFFIXES:
            raise ValueError(
                f"Disallowed non-safetensors weight file in SAM 2.1 model directory: {child.name}"
            )

    manifest_path = base / "manifest.json"
    file_hashes: dict[str, str] = dict(MODEL_FILE_SHA256S)
    if expected_sha256 is not None:
        file_hashes["model.safetensors"] = expected_sha256.strip().lower()
    elif manifest_path.is_file() and not manifest_path.is_symlink():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if type(manifest) is not dict or type(manifest.get("files")) is not dict:
            raise ValueError("SAM 2.1 model manifest.json must contain a files mapping")
        file_hashes = {}
        for name, entry in manifest["files"].items():
            sha = (
                entry.get("sha256")
                if isinstance(entry, dict)
                else entry
                if isinstance(entry, str)
                else ""
            )
            if not isinstance(sha, str) or not SHA256_HEX_RE.fullmatch(
                sha.strip().lower()
            ):
                raise ValueError(
                    f"SAM 2.1 manifest entry for {name} is missing a pinned 64-hex sha256"
                )
            file_hashes[name] = sha.strip().lower()
        if "model.safetensors" not in file_hashes:
            raise ValueError("SAM 2.1 model manifest must include model.safetensors")

    verified_paths: dict[str, Path] = {}
    for required_name, pinned_sha in file_hashes.items():
        if not SHA256_HEX_RE.fullmatch(pinned_sha):
            raise ValueError(f"Invalid pinned SHA-256 for {required_name}")
        file_path = base / required_name
        if not file_path.exists() or file_path.is_symlink() or not file_path.is_file():
            raise ValueError(f"SAM 2.1 model file not found: {file_path}")
        actual_sha = hashlib.sha256(file_path.read_bytes()).hexdigest()
        if actual_sha != pinned_sha:
            raise ValueError(f"SAM 2.1 model SHA-256 mismatch for {required_name}")
        verified_paths[required_name] = file_path

    for cfg_name in (
        "config.json",
        "preprocessor_config.json",
        "processor_config.json",
    ):
        cfg_path = base / cfg_name
        if not cfg_path.exists() or cfg_path.is_symlink() or not cfg_path.is_file():
            raise ValueError(f"SAM 2.1 config file not found: {cfg_path}")
        parsed_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        if type(parsed_cfg) is not dict:
            raise ValueError(f"SAM 2.1 config file must be a JSON object: {cfg_name}")
        verified_paths[cfg_name] = cfg_path

    return {
        "model_dir": base,
        "weights_path": verified_paths["model.safetensors"],
        "files": verified_paths,
        "repo": MODEL_REPO,
        "revision": MODEL_REVISION,
        "sha256": file_hashes["model.safetensors"],
    }


# 兼容旧函数名别名
verify_onnx_model_dir = verify_sam2_model_dir


# 使用OpenCV RETR_TREE提取多边形外轮廓与内部孔洞（与Mac studio_regions.py一致）
def _mask_shapes(
    mask: Any, size: tuple[int, int], *, op: str = "add"
) -> list[dict[str, Any]]:
    if op not in ("add", "subtract"):
        raise ValueError("Invalid segmentation operation")
    width, height = size
    arr = np.asarray(mask)
    binary = (arr > 0).astype(np.uint8) * 255
    if int((binary > 0).sum()) < MIN_SELECTED_PIXELS:
        raise ValueError("Point segmentation did not match a valid region")

    try:
        import cv2

        contours, hierarchy = cv2.findContours(
            binary, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE
        )
        if not contours or hierarchy is None:
            raise ValueError("Point segmentation did not match a valid region")
        tree = hierarchy[0]

        def _depth(index: int) -> int:
            level = 0
            parent = int(tree[index][3])
            while parent >= 0:
                level += 1
                parent = int(tree[parent][3])
            return level

        hole_op = "subtract" if op == "add" else "add"
        items: list[tuple[int, float, dict[str, Any]]] = []
        for idx, contour in enumerate(contours):
            area = float(cv2.contourArea(contour))
            if area < 24:
                continue
            perimeter = float(cv2.arcLength(contour, True))
            approx = cv2.approxPolyDP(contour, max(1.2, perimeter * 0.0025), True)
            pts = [
                {
                    "x": round(int(pt[0][0]) / max(1, width - 1) * 999),
                    "y": round(int(pt[0][1]) / max(1, height - 1) * 999),
                }
                for pt in approx[:256]
            ]
            if len(pts) >= 3:
                depth = _depth(idx)
                shape_op = op if depth % 2 == 0 else hole_op
                items.append(
                    (depth, -area, {"type": "polygon", "op": shape_op, "points": pts})
                )
        items.sort(key=lambda item: (item[0], item[1]))
        shapes = [item[2] for item in items[:16]]
        if shapes:
            return selection_value({"version": 1, "shapes": shapes})["shapes"]
    except ImportError:
        pass

    # 无OpenCV环境下的纯Pillow轮廓提取回退：按行采样左右边界构造闭合多边形
    mask_img = (
        mask if isinstance(mask, Image.Image) else Image.fromarray(binary, mode="L")
    )
    box = mask_img.getbbox()
    if box is None:
        raise ValueError("Point segmentation did not match a valid region")
    raw = mask_img.load()
    left_pts: list[dict[str, int]] = []
    right_pts: list[dict[str, int]] = []
    x1, y1, x2, y2 = box
    step = max(1, (y2 - y1) // 48)
    ys = list(range(y1, y2, step))
    if (y2 - 1) not in ys:
        ys.append(y2 - 1)
    for y in ys:
        row_xs = [x for x in range(x1, x2) if raw[x, y] > 0]
        if row_xs:
            left_pts.append(
                {
                    "x": round(row_xs[0] / max(1, width - 1) * 999),
                    "y": round(y / max(1, height - 1) * 999),
                }
            )
            right_pts.append(
                {
                    "x": round(row_xs[-1] / max(1, width - 1) * 999),
                    "y": round(y / max(1, height - 1) * 999),
                }
            )
    polygon_pts = left_pts + list(reversed(right_pts))
    if len(polygon_pts) < 3:
        raise ValueError("Point segmentation did not match a valid region")
    return selection_value(
        {"version": 1, "shapes": [{"type": "polygon", "op": op, "points": polygon_pts}]}
    )["shapes"]


# 将二值分割掩码图像转换为0到999归一化多边形顶点列表
def mask_array_to_shapes(
    mask_img: Image.Image | np.ndarray,
    size: tuple[int, int] | None = None,
    *,
    op: str = "add",
) -> list[dict[str, Any]]:
    resolved_size = (
        size
        if size is not None
        else mask_img.size
        if isinstance(mask_img, Image.Image)
        else (int(mask_img.shape[1]), int(mask_img.shape[0]))
    )
    return _mask_shapes(mask_img, resolved_size, op=op)


# 基于PyTorch CPU + transformers（Sam2Model）的SAM 2.1 hiera-tiny点选分割器（首次使用时懒加载、单图embedding缓存、串行推理）
class Segmenter:
    def __init__(
        self,
        model_dir: str | Path | None = None,
        *,
        model_loader: Any | None = None,
    ) -> None:
        self._model_dir = model_dir
        self._model_loader = model_loader
        self._lock = threading.Lock()
        self._processor: Any | None = None
        self._model: Any | None = None
        self._cache_key: tuple[str, int, int, tuple[int, int]] | None = None
        self._embedding: Any | None = None

    # 检查本地是否已具备通过SHA256校验的SAM 2.1 hiera-tiny safetensors模型与PyTorch/transformers运行时
    def ready(self) -> bool:
        try:
            if self._model_loader is None:
                import safetensors  # noqa: F401
                import torch  # noqa: F401
                from transformers import Sam2Model, Sam2Processor  # noqa: F401
            verify_sam2_model_dir(self._model_dir)
            return True
        except (ImportError, ValueError, OSError):
            return False

    # 在首次调用时校验safetensors SHA256并懒加载Sam2Processor与Sam2Model（仅限本地文件与CPU）
    def _ensure_loaded_locked(self) -> tuple[Any, Any]:
        if self._processor is not None and self._model is not None:
            return self._processor, self._model
        verified = verify_sam2_model_dir(self._model_dir)
        if self._model_loader is not None:
            self._processor, self._model = self._model_loader(verified["model_dir"])
            return self._processor, self._model

        import torch
        from transformers import Sam2Model, Sam2Processor

        torch.set_num_threads(min(4, max(1, os.cpu_count() or 2)))
        model_path_str = str(verified["model_dir"])
        self._processor = Sam2Processor.from_pretrained(
            model_path_str,
            local_files_only=True,
        )
        self._model = Sam2Model.from_pretrained(
            model_path_str,
            local_files_only=True,
            use_safetensors=True,
            dtype=torch.float32,
        )
        self._model.to("cpu")
        self._model.eval()
        return self._processor, self._model

    # 对指定图片执行点提示分割并返回归一化多边形选区形状（单图embedding缓存+串行锁）
    def segment(
        self,
        image_path: str | Path,
        points: list[dict[str, Any]],
        existing: dict[str, Any] | None = None,
        operation: str = "add",
    ) -> dict[str, Any]:
        if not isinstance(points, list) or not 1 <= len(points) <= 24:
            raise ValueError("Point segmentation requires between 1 and 24 points")
        if operation not in ("add", "subtract"):
            raise ValueError("Segmentation operation must be 'add' or 'subtract'")
        normalized_pts: list[tuple[int, int, int]] = []
        for point in points:
            if type(point) is not dict:
                raise ValueError("Invalid segmentation point object")
            label = point.get("label", 1)
            if label not in (0, 1) or isinstance(label, bool):
                raise ValueError("Segmentation point label must be 0 or 1")
            normalized_pts.append(
                (_coord(point.get("x")), _coord(point.get("y")), label)
            )
        if not any(label == 1 for _, _, label in normalized_pts):
            raise ValueError("At least one foreground point (label=1) is required")
        if operation == "subtract" and not existing:
            raise ValueError("Cannot subtract without an existing selection")

        import torch

        resolved = Path(image_path).resolve()
        stat = resolved.stat()

        with self._lock:
            processor, model = self._ensure_loaded_locked()
            image = open_image(resolved)
            full_size = image.size
            image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            w, h = image.size
            key = (str(resolved), stat.st_mtime_ns, stat.st_size, (w, h))
            coords = [
                [
                    [
                        [
                            round(x / 999.0 * max(1, w - 1), 2),
                            round(y / 999.0 * max(1, h - 1), 2),
                        ]
                        for x, y, _ in normalized_pts
                    ]
                ]
            ]
            labels = [[[label for _, _, label in normalized_pts]]]
            inputs = processor(
                images=image,
                input_points=coords,
                input_labels=labels,
                return_tensors="pt",
            )
            if hasattr(inputs, "to") and hasattr(model, "device"):
                inputs = inputs.to(model.device)
            with torch.inference_mode():
                if self._cache_key == key and self._embedding is not None:
                    cached_inputs = dict(inputs)
                    cached_inputs.pop("pixel_values", None)
                    output = model(
                        **cached_inputs,
                        image_embeddings=self._embedding,
                        multimask_output=True,
                    )
                else:
                    output = model(**inputs, multimask_output=True)
                    self._cache_key = key
                    self._embedding = output.image_embeddings

            masks = processor.post_process_masks(
                output.pred_masks.cpu(),
                inputs["original_sizes"].cpu(),
            )[0]
            scores_flat = output.iou_scores.detach().cpu().reshape(-1)
            best_idx = int(scores_flat.argmax().item())
            best_score = float(scores_flat[best_idx].item())
            mask_2d = (masks[0, best_idx].detach().cpu().numpy() > 0).astype(
                np.uint8
            ) * 255

        if existing:
            previous = np.asarray(mask_for(existing, (w, h)))
            combined_mask = (
                np.maximum(previous, mask_2d)
                if operation == "add"
                else np.where(mask_2d > 0, 0, previous).astype(np.uint8)
            )
        else:
            combined_mask = mask_2d

        shapes = _mask_shapes(combined_mask, (w, h), op="add")
        final_mask = mask_for({"version": 1, "shapes": shapes}, full_size)
        selected_pixels = int(sum(final_mask.histogram()[1:]))
        if selected_pixels < MIN_SELECTED_PIXELS:
            raise ValueError(
                "Point segmentation did not leave a valid active selection"
            )
        return {
            "version": 1,
            "shapes": shapes,
            "score": round(best_score, 3),
            "selected_pixels": selected_pixels,
        }


# 保持旧类名兼容
OnnxSamSegmenter = Segmenter


# 选区编辑器令牌最短/最大有效时长（秒）、单次确认随机数正则与重放异常类型
EDITOR_TOKEN_MAX_TTL_SECONDS = 900
EDITOR_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


# 选区编辑器令牌已被消费后再次提交confirm时的重放拒绝异常
class EditorTokenReplayError(ValueError):
    pass


# 校验选区编号格式（sel_<32位小写十六进制>）
def validate_selection_id(selection_id: str | None) -> str:
    cleaned = selection_id.strip() if isinstance(selection_id, str) else ""
    if not SELECTION_ID_RE.fullmatch(cleaned):
        raise ValueError("Invalid selection ID")
    return cleaned


# 在指定用户的专属目录下持久化已确认选区、掩码PNG与加密proof凭据（支持单次nonce消费防重放）
def save_confirmed_selection(
    settings: StudioSettings,
    user_id: str,
    image_id: str,
    image_path: str | Path,
    selection: dict[str, Any],
    *,
    expected_image_sha256: str | None = None,
    editor_nonce: str | None = None,
) -> dict[str, Any]:
    uid = validate_user_id(user_id)
    normalized_sel = selection_value(selection)
    resolved_image_path = Path(image_path)
    proof = selection_proof(resolved_image_path, normalized_sel)
    if (
        expected_image_sha256 is not None
        and proof["image_sha256"] != expected_image_sha256
    ):
        raise ValueError(
            "Base image content changed since selection editor token was issued"
        )
    image = open_image(resolved_image_path)
    mask = mask_for(normalized_sel, image.size)

    studio_dir = user_studio_dir(settings, uid, create=True)
    selections_root = studio_dir / "selections"
    selections_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    if selections_root.is_symlink():
        raise ValueError("User selections directory must not be a symlink")

    nonce_lock_path: Path | None = None
    if editor_nonce is not None:
        cleaned_nonce = editor_nonce.strip() if isinstance(editor_nonce, str) else ""
        if not EDITOR_NONCE_RE.fullmatch(cleaned_nonce):
            raise ValueError("Invalid editor token nonce")
        nonces_dir = selections_root / "_used_nonces"
        nonces_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        if nonces_dir.is_symlink():
            raise ValueError("Editor nonce directory must not be a symlink")
        nonce_lock_path = nonces_dir / f"{cleaned_nonce}.lock"
        try:
            fd = os.open(nonce_lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        except FileExistsError as exc:
            raise EditorTokenReplayError(
                "Selection editor token has already been used to confirm a selection"
            ) from exc

    try:
        for _ in range(8):
            sel_id = f"sel_{secrets.token_hex(16)}"
            sel_dir = selections_root / sel_id
            try:
                os.mkdir(sel_dir, 0o700)
                break
            except FileExistsError:
                continue
        else:
            raise RuntimeError("Unable to allocate unique selection directory")

        save_exact(mask, sel_dir / "mask.png")
        record: dict[str, Any] = {
            "selection_id": sel_id,
            "user_id": uid,
            "image_id": image_id,
            "created": time.time(),
            "selection": normalized_sel,
            "proof": proof,
        }
        target = sel_dir / "selection.json"
        temporary = sel_dir / "selection.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        return record
    except BaseException:
        if nonce_lock_path is not None:
            with contextlib.suppress(OSError):
                nonce_lock_path.unlink(missing_ok=True)
        raise


# 读取并校验指定用户的已确认选区记录（若提供底图路径则强制核对底图与掩码SHA256凭据）
def load_confirmed_selection(
    settings: StudioSettings,
    user_id: str,
    selection_id: str,
    *,
    base_image_id: str | None = None,
    base_path: str | Path | None = None,
) -> dict[str, Any]:
    uid = validate_user_id(user_id)
    studio_dir = user_studio_dir(settings, uid, create=False)
    selections_root = studio_dir / "selections"
    if (
        not studio_dir.exists()
        or studio_dir.is_symlink()
        or not selections_root.exists()
        or selections_root.is_symlink()
        or not selections_root.is_dir()
    ):
        raise ValueError(
            f"Confirmed selection not found for current user: {selection_id}"
        )

    cleaned_id = selection_id.strip() if isinstance(selection_id, str) else ""
    if cleaned_id == "@latest":
        candidates: list[tuple[float, str, dict[str, Any]]] = []
        for entry in selections_root.iterdir():
            if entry.is_symlink() or not entry.is_dir():
                continue
            if not SELECTION_ID_RE.fullmatch(entry.name):
                continue
            sel_json = entry / "selection.json"
            if not sel_json.is_file() or sel_json.is_symlink():
                continue
            try:
                raw = read_bytes_no_follow(sel_json, studio_dir, max_bytes=1024 * 1024)
                parsed = json.loads(raw.decode("utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if not isinstance(parsed, dict) or parsed.get("user_id") != uid:
                continue
            if base_image_id is not None and parsed.get("image_id") != base_image_id:
                continue
            created = float(parsed.get("created") or 0.0)
            candidates.append((created, entry.name, parsed))
        if not candidates:
            raise ValueError("No confirmed selection found for the target image")
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        record = candidates[0][2]
    else:
        sid = validate_selection_id(cleaned_id)
        sel_dir = selections_root / sid
        if not sel_dir.exists() or sel_dir.is_symlink() or not sel_dir.is_dir():
            raise ValueError(f"Confirmed selection not found for current user: {sid}")
        sel_json = sel_dir / "selection.json"
        if not sel_json.exists() or sel_json.is_symlink() or not sel_json.is_file():
            raise ValueError(f"Confirmed selection not found for current user: {sid}")
        ensure_safe_subpath(sel_json, studio_dir)
        raw = read_bytes_no_follow(sel_json, studio_dir, max_bytes=1024 * 1024)
        record = json.loads(raw.decode("utf-8"))
        if not isinstance(record, dict) or record.get("user_id") != uid:
            raise ValueError(f"Confirmed selection not found for current user: {sid}")

    normalized_sel = selection_value(record.get("selection"))
    stored_proof = record.get("proof")
    if type(stored_proof) is not dict:
        raise ValueError("Confirmed selection record is missing proof metadata")

    if (
        base_image_id is not None
        and record.get("image_id")
        and record.get("image_id") != base_image_id
    ):
        raise ValueError(
            "Selection proof mismatch: the confirmed selection belongs to a different base image"
        )

    if base_path is not None:
        current_proof = selection_proof(base_path, normalized_sel)
        for key in (
            "image_sha256",
            "mask_sha256",
            "width",
            "height",
            "selected_pixels",
        ):
            if stored_proof.get(key) != current_proof.get(key):
                raise ValueError(
                    f"Selection proof mismatch on {key}: the base image or selection mask does not match the confirmed proof"
                )

    record["selection"] = normalized_sel
    return record


# 校验图片编号在签名令牌中的格式合法性
def _validate_token_image_id(image_id: str | None) -> str:
    cleaned_img = image_id.strip() if isinstance(image_id, str) else ""
    if (
        not cleaned_img
        or len(cleaned_img) > 128
        or ":" in cleaned_img
        or "/" in cleaned_img
        or "\\" in cleaned_img
        or ".." in cleaned_img
        or "\x00" in cleaned_img
    ):
        raise ValueError("Invalid image ID for signed token")
    return cleaned_img


# 使用HMAC-SHA256对用户编号、图片编号与过期时间生成只读图片预览签名访问令牌（img:前缀，禁止POST写操作）
def sign_image_token(
    settings: StudioSettings,
    user_id: str,
    image_id: str,
    *,
    expires_at: int | None = None,
    ttl_seconds: int | None = None,
) -> tuple[str, int]:
    uid = validate_user_id(user_id)
    cleaned_img = _validate_token_image_id(image_id)
    if expires_at is not None:
        exp = int(expires_at)
    else:
        fields_set = getattr(settings, "model_fields_set", set())
        if ttl_seconds is not None:
            raw_ttl = int(ttl_seconds)
        elif "preview_token_ttl_seconds" in fields_set:
            raw_ttl = int(settings.preview_token_ttl_seconds)
        elif "file_token_ttl_seconds" in fields_set:
            raw_ttl = int(settings.file_token_ttl_seconds)
        else:
            raw_ttl = max(
                int(settings.file_token_ttl_seconds),
                int(
                    getattr(
                        settings,
                        "preview_token_ttl_seconds",
                        settings.file_token_ttl_seconds,
                    )
                ),
            )
        exp = int(time.time()) + max(60, raw_ttl)
    payload_raw = f"img:{uid}:{exp}:{cleaned_img}".encode()
    payload_b64 = base64.urlsafe_b64encode(payload_raw).decode("ascii").rstrip("=")
    key_bytes = bytes.fromhex(settings.file_signing_key)
    signature = hmac.new(
        key_bytes, payload_b64.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"{payload_b64}.{signature}", exp


# 判断令牌载荷是否为只读图片预览令牌（以img:前缀区分）
def is_image_token(token: str) -> bool:
    if not isinstance(token, str):
        return False
    match = REGION_TOKEN_RE.fullmatch(token)
    if match is None:
        return False
    payload_b64 = match.group(1)
    padding = "=" * (-len(payload_b64) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload_b64 + padding).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    return decoded.startswith("img:")


# 校验HMAC-SHA256只读图片预览令牌并返回（user_id, image_id, expires_at）
def verify_image_token(
    settings: StudioSettings,
    token: str,
    *,
    now: float | None = None,
    allow_expired: bool = False,
    max_expired_age_seconds: int | None = None,
) -> tuple[str, str, int]:
    cleaned_token = token if isinstance(token, str) else ""
    match = REGION_TOKEN_RE.fullmatch(cleaned_token)
    if match is None:
        raise ValueError("Malformed image token")
    payload_b64, provided_sig = match.groups()
    key_bytes = bytes.fromhex(settings.file_signing_key)
    expected_sig = hmac.new(
        key_bytes, payload_b64.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(
        provided_sig.encode("ascii"), expected_sig.encode("ascii")
    ):
        raise ValueError("Image token signature mismatch")
    padding = "=" * (-len(payload_b64) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload_b64 + padding).decode("utf-8")
    except Exception as exc:
        raise ValueError("Invalid image token payload encoding") from exc
    parts = decoded.split(":", 3)
    if len(parts) != 4 or parts[0] != "img":
        raise ValueError("Invalid image token payload structure")
    _, raw_uid, raw_exp, raw_image_id = parts
    uid = validate_user_id(raw_uid)
    if not raw_exp.isdigit() or len(raw_exp) > 12:
        raise ValueError("Invalid image token expiration")
    exp = int(raw_exp)
    current_ts = time.time() if now is None else float(now)
    if current_ts >= exp:
        if not allow_expired:
            raise ValueError("Image token has expired")
        if (
            max_expired_age_seconds is not None
            and (current_ts - exp) > max_expired_age_seconds
        ):
            raise ValueError("Image token has expired beyond renewal window")
    cleaned_img = _validate_token_image_id(raw_image_id)
    return uid, cleaned_img, exp


# 使用HMAC-SHA256签发专用、短效、绑定（user_id, image_id, image_sha256, nonce）的选区编辑器令牌（edt:前缀）
def sign_editor_token(
    settings: StudioSettings,
    user_id: str,
    image_id: str,
    image_sha256: str,
    *,
    expires_at: int | None = None,
    ttl_seconds: int | None = None,
    nonce: str | None = None,
    issued_at: int | None = None,
) -> tuple[str, int]:
    uid = validate_user_id(user_id)
    cleaned_img = _validate_token_image_id(image_id)
    cleaned_sha = image_sha256.strip().lower() if isinstance(image_sha256, str) else ""
    if not SHA256_HEX_RE.fullmatch(cleaned_sha):
        raise ValueError("Invalid base image SHA-256 for editor token")
    token_nonce = (
        nonce.strip()
        if isinstance(nonce, str) and nonce.strip()
        else secrets.token_urlsafe(16)
    )
    if not EDITOR_NONCE_RE.fullmatch(token_nonce):
        raise ValueError("Invalid editor token nonce")
    issued = int(time.time()) if issued_at is None else int(issued_at)
    max_expires_at = issued + settings.editor_capability_max_lifetime_seconds
    if int(time.time()) >= max_expires_at:
        raise ValueError("Editor capability has reached its maximum lifetime")
    if expires_at is not None:
        exp = int(expires_at)
    else:
        fields_set = getattr(settings, "model_fields_set", set())
        if ttl_seconds is not None:
            raw_ttl = int(ttl_seconds)
        elif "editor_token_ttl_seconds" in fields_set:
            raw_ttl = int(settings.editor_token_ttl_seconds)
        else:
            raw_ttl = min(
                int(settings.file_token_ttl_seconds),
                int(
                    getattr(
                        settings,
                        "editor_token_ttl_seconds",
                        EDITOR_TOKEN_MAX_TTL_SECONDS,
                    )
                ),
            )
        effective_ttl = max(1, min(raw_ttl, EDITOR_TOKEN_MAX_TTL_SECONDS))
        exp = int(time.time()) + effective_ttl
    exp = min(exp, max_expires_at)
    payload_raw = f"edt:{uid}:{exp}:{issued}:{cleaned_sha}:{token_nonce}:{cleaned_img}".encode()
    payload_b64 = base64.urlsafe_b64encode(payload_raw).decode("ascii").rstrip("=")
    key_bytes = bytes.fromhex(settings.file_signing_key)
    signature = hmac.new(
        key_bytes, payload_b64.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"{payload_b64}.{signature}", exp


# 判断令牌载荷是否为专用选区编辑器令牌（以edt:前缀区分）
def is_editor_token(token: str) -> bool:
    if not isinstance(token, str):
        return False
    match = REGION_TOKEN_RE.fullmatch(token)
    if match is None:
        return False
    payload_b64 = match.group(1)
    padding = "=" * (-len(payload_b64) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload_b64 + padding).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    return decoded.startswith("edt:")


# 检查指定用户的选区编辑器单次随机数是否已被确认操作消耗
def is_editor_nonce_consumed(
    settings: StudioSettings, user_id: str, editor_nonce: str
) -> bool:
    uid = validate_user_id(user_id)
    cleaned_nonce = editor_nonce.strip() if isinstance(editor_nonce, str) else ""
    if not EDITOR_NONCE_RE.fullmatch(cleaned_nonce):
        raise ValueError("Invalid editor token nonce")
    studio_dir = user_studio_dir(settings, uid, create=False)
    nonce_lock_path = studio_dir / "selections" / "_used_nonces" / f"{cleaned_nonce}.lock"
    return nonce_lock_path.exists() or nonce_lock_path.is_symlink()


# 校验HMAC-SHA256选区编辑器令牌并返回绑定声明字典（user_id, image_id, image_sha256, nonce, expires_at）
def verify_editor_token(
    settings: StudioSettings,
    token: str,
    *,
    now: float | None = None,
    allow_expired: bool = False,
    max_expired_age_seconds: int | None = None,
) -> dict[str, Any]:
    cleaned_token = token if isinstance(token, str) else ""
    match = REGION_TOKEN_RE.fullmatch(cleaned_token)
    if match is None:
        raise ValueError("Malformed editor token")
    payload_b64, provided_sig = match.groups()
    key_bytes = bytes.fromhex(settings.file_signing_key)
    expected_sig = hmac.new(
        key_bytes, payload_b64.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(
        provided_sig.encode("ascii"), expected_sig.encode("ascii")
    ):
        raise ValueError("Editor token signature mismatch")
    padding = "=" * (-len(payload_b64) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload_b64 + padding).decode("utf-8")
    except Exception as exc:
        raise ValueError("Invalid editor token payload encoding") from exc
    parts = decoded.split(":", 6)
    if len(parts) not in (6, 7) or parts[0] != "edt":
        raise ValueError("Invalid editor token payload structure")
    if len(parts) == 7:
        _, raw_uid, raw_exp, raw_issued, raw_sha, raw_nonce, raw_image_id = parts
        if not raw_issued.isdigit() or len(raw_issued) > 12:
            raise ValueError("Invalid editor token issue time")
        issued = int(raw_issued)
    else:
        _, raw_uid, raw_exp, raw_sha, raw_nonce, raw_image_id = parts
        issued = int(raw_exp) - EDITOR_TOKEN_MAX_TTL_SECONDS
    uid = validate_user_id(raw_uid)
    if not raw_exp.isdigit() or len(raw_exp) > 12:
        raise ValueError("Invalid editor token expiration")
    exp = int(raw_exp)
    current_ts = time.time() if now is None else float(now)
    if issued > current_ts + 60 or exp > issued + settings.editor_capability_max_lifetime_seconds:
        raise ValueError("Invalid editor token lifetime")
    if current_ts >= exp:
        if not allow_expired:
            raise ValueError("Editor token has expired")
        if (
            max_expired_age_seconds is not None
            and (current_ts - exp) > max_expired_age_seconds
        ):
            raise ValueError("Editor token has expired beyond renewal window")
    if not SHA256_HEX_RE.fullmatch(raw_sha):
        raise ValueError("Invalid image SHA-256 in editor token")
    if not EDITOR_NONCE_RE.fullmatch(raw_nonce):
        raise ValueError("Invalid nonce in editor token")
    cleaned_img = _validate_token_image_id(raw_image_id)
    return {
        "user_id": uid,
        "image_id": cleaned_img,
        "image_sha256": raw_sha,
        "nonce": raw_nonce,
        "expires_at": exp,
        "issued_at": issued,
    }


# 生成内联于聊天窗口的局部选区编辑器HTML（支持点选分割、框选、多边形套索、画笔加减选、撤销、清空、适应窗口与缩放）
def build_selection_editor_html(
    *,
    image_id: str,
    signed_url: str,
    expires_at: str,
    width: int,
    height: int,
    segment_ready: bool = False,
    preview_url: str | None = None,
    preview_expires_at: str | None = None,
) -> str:
    safe_image_id = html.escape(image_id, quote=True)
    effective_preview_url = preview_url if preview_url else signed_url
    effective_preview_expires = (
        preview_expires_at if preview_expires_at else expires_at
    )
    safe_expires = html.escape(expires_at, quote=True)
    safe_preview_expires = html.escape(effective_preview_expires, quote=True)
    human_expires = html.escape(
        format_display_timestamp(effective_preview_expires), quote=True
    )
    safe_url_js = json.dumps(signed_url)
    safe_preview_url_js = json.dumps(effective_preview_url)
    safe_expires_js = json.dumps(expires_at)
    safe_preview_expires_js = json.dumps(effective_preview_expires)
    safe_image_id_js = json.dumps(image_id)
    segment_ready_js = "true" if segment_ready else "false"
    default_tool = "point" if segment_ready else "rect"
    default_tool_js = json.dumps(default_tool)
    point_btn_disabled = (
        ""
        if segment_ready
        else 'disabled title="当前环境未挂载离线分割模型，请使用框选、套索或画笔"'
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1" />
<title>局部选区编辑器（{width}×{height}）</title>
<style>
  :root {{
    color-scheme: light dark;
    --bg: #f6f4ef;
    --panel: #ffffff;
    --border: rgba(36, 32, 26, 0.15);
    --text: #231f1a;
    --muted: #655e54;
    --accent: #2563eb;
    --accent-hover: #1d4ed8;
    --ok: #15803d;
    --danger: #b42318;
    --stage-bg: #1e1e22;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #171614;
      --panel: #22201c;
      --border: rgba(245, 242, 235, 0.15);
      --text: #f5f2eb;
      --muted: #a69f93;
      --accent: #3b82f6;
      --accent-hover: #2563eb;
      --ok: #22c55e;
      --danger: #f87171;
      --stage-bg: #111114;
    }}
  }}
  * {{ box-sizing: border-box; }}
  html, body {{
    margin: 0;
    padding: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    background: var(--bg);
    color: var(--text);
  }}
  .editor-wrap {{
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-radius: 12px;
    background: var(--panel);
  }}
  .header-row, .toolbar-row, .footer-row {{
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }}
  .title-badge {{
    font-size: 13px;
    font-weight: 600;
  }}
  .meta-badge {{
    font-size: 12px;
    color: var(--muted);
  }}
  .btn-group {{
    display: inline-flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px;
  }}
  button {{
    appearance: none;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--text);
    border-radius: 6px;
    padding: 5px 10px;
    font-size: 12px;
    cursor: pointer;
    transition: background 0.15s, border-color 0.15s;
  }}
  button:hover:not(:disabled) {{
    border-color: var(--accent);
  }}
  button[aria-pressed="true"] {{
    background: var(--accent);
    color: #fff;
    border-color: var(--accent);
  }}
  button:disabled {{
    opacity: 0.45;
    cursor: not-allowed;
  }}
  button.primary-btn {{
    background: var(--accent);
    color: #fff;
    border-color: var(--accent);
    font-weight: 600;
    padding: 6px 14px;
  }}
  button.primary-btn:hover:not(:disabled) {{
    background: var(--accent-hover);
  }}
  .brush-ctrl {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    color: var(--muted);
  }}
  .stage-viewport {{
    position: relative;
    width: 100%;
    height: 400px;
    overflow: auto;
    background: var(--stage-bg);
    border-radius: 8px;
    border: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: center;
    user-select: none;
  }}
  .stage-inner {{
    position: relative;
    display: inline-block;
    line-height: 0;
  }}
  .stage-inner img {{
    display: block;
    width: 100%;
    height: 100%;
    pointer-events: none;
    user-select: none;
  }}
  .stage-inner canvas {{
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    cursor: crosshair;
    touch-action: none;
  }}
  .status-line {{
    font-size: 12px;
    color: var(--muted);
    flex: 1;
    min-width: 200px;
  }}
  .status-line.ok {{
    color: var(--ok);
    font-weight: 600;
  }}
  .status-line.err {{
    color: var(--danger);
  }}
  .instruction-input {{
    flex: 1;
    min-width: 200px;
    padding: 6px 10px;
    font-size: 12px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--bg);
    color: var(--text);
  }}
</style>
</head>
<body>
<div class="editor-wrap" id="editorRoot" data-image-id="{safe_image_id}">
  <div class="header-row">
    <span class="title-badge">只改局部 · 选区编辑器（{width}×{height}）</span>
    <span class="meta-badge">蓝色半透明区域将被修改，选区外像素 100% 原样保留 · <span id="expiresBadge" data-expires="{safe_preview_expires}" data-editor-expires="{safe_expires}" data-preview-expires="{safe_preview_expires}">有效期至 {human_expires}</span></span>
  </div>
  <div class="toolbar-row">
    <div class="btn-group" role="group" aria-label="选区工具">
      <button type="button" data-tool="point" {point_btn_disabled}>点选识别</button>
      <button type="button" data-tool="rect">框选</button>
      <button type="button" data-tool="polygon">多边形套索</button>
      <button type="button" data-tool="stroke">画笔涂抹</button>
    </div>
    <div class="btn-group" role="group" aria-label="布尔模式">
      <button type="button" data-op="add" aria-pressed="true">加选区域</button>
      <button type="button" data-op="subtract" aria-pressed="false">减选擦除</button>
    </div>
    <label class="brush-ctrl" id="brushWrap">
      <span>画笔粗细</span>
      <input type="range" id="brushRadius" min="4" max="100" value="22" />
      <span id="brushVal">22</span>
    </label>
    <div class="btn-group" role="group" aria-label="视图与历史">
      <button type="button" id="closePolyBtn" hidden>闭合多边形</button>
      <button type="button" id="undoBtn" disabled>撤销</button>
      <button type="button" id="clearBtn" disabled>清空</button>
      <button type="button" id="fitBtn">适应窗口</button>
      <button type="button" data-zoom="1">100%</button>
      <button type="button" data-zoom="1.5">150%</button>
      <button type="button" data-zoom="2">200%</button>
    </div>
  </div>
  <div class="stage-viewport" id="viewport">
    <div class="stage-inner" id="stageInner">
      <img id="baseImg" alt="待选区底图" crossorigin="anonymous" />
      <canvas id="overlayCanvas"></canvas>
    </div>
  </div>
  <div class="footer-row">
    <div class="status-line" id="statusText">正在加载底图...</div>
    <input type="text" class="instruction-input" id="instructionInput" placeholder="选填：输入局部修改要求（如：把选区内的台面换成白玉兰大理石）" />
    <button type="button" id="renewBtn" hidden>重新获取链接</button>
    <button type="button" class="primary-btn" id="confirmBtn" disabled>确认选区</button>
  </div>
</div>
<script>
(function() {{
  const SIGNED_URL = {safe_url_js};
  const PREVIEW_URL = {safe_preview_url_js};
  const IMAGE_ID = {safe_image_id_js};
  const RESOURCE_URI = 'ui://zyb_studio/selection/' + IMAGE_ID;
  const EDITOR_EXPIRES_AT = {safe_expires_js};
  const PREVIEW_EXPIRES_AT = {safe_preview_expires_js};
  const SEGMENT_READY = {segment_ready_js};

  function extractEditorCapability(signedUrl) {{
    if (typeof signedUrl !== 'string' || !signedUrl) return 'default';
    const match = signedUrl.match(/\\/(?:studio-files|files)\\/([A-Za-z0-9_-]+)\\.([0-9a-f]{{16,64}})/);
    if (match && match[2]) return match[2].slice(0, 24);
    const clean = signedUrl.split('?')[0].replace(/[^A-Za-z0-9_-]/g, '');
    return clean.slice(-24) || 'default';
  }}

  const EDITOR_CAPABILITY = extractEditorCapability(SIGNED_URL);
  const DRAFT_KEY = 'zyb_studio:selection:' + IMAGE_ID + ':' + EDITOR_CAPABILITY;
  const SESSION_NONCE = (() => {{
    const buf = new Uint32Array(4);
    if (window.crypto && window.crypto.getRandomValues) {{
      window.crypto.getRandomValues(buf);
      return Array.from(buf, (n) => n.toString(16).padStart(8, '0')).join('');
    }}
    return String(Date.now()) + '-' + Math.random().toString(16).slice(2);
  }})();

  let currentEditorUrl = SIGNED_URL;
  let currentPreviewUrl = PREVIEW_URL || SIGNED_URL;
  let editorExpiresIso = EDITOR_EXPIRES_AT;
  let previewExpiresIso = PREVIEW_EXPIRES_AT || EDITOR_EXPIRES_AT;
  let reqCounter = 0;

  const state = {{
    tool: {default_tool_js},
    op: 'add',
    brushRadius: 22,
    zoom: 1,
    shapes: [],
    baseShapes: [],
    points: [],
    pointOp: 'add',
    draft: null,
    activeMaskPixels: 0,
    confirmedId: null,
    busy: false,
    triedEditorFallback: false,
    autoRenewSent: false,
    lastDraftUpdatedAt: 0,
    pendingDraftRequestId: null,
    pendingRenewRequestId: null,
    renderDataReceived: false
  }};

  function nextRequestId(prefix) {{
    reqCounter += 1;
    return prefix + '-' + SESSION_NONCE + '-' + reqCounter;
  }}

  function isValidRenewedStudioUrl(candidateUrl, referenceUrl, expectedPrefix) {{
    if (typeof candidateUrl !== 'string' || !candidateUrl) return false;
    try {{
      const cand = new URL(candidateUrl, window.location.href);
      if (cand.protocol !== 'http:' && cand.protocol !== 'https:') return false;
      const ref = new URL(referenceUrl || SIGNED_URL, window.location.href);
      if (cand.origin !== ref.origin) return false;
      const match = cand.pathname.match(/\\/(?:studio-files|files)\\/([A-Za-z0-9_-]+)\\.([0-9a-f]{{64}})$/);
      if (!match) return false;
      if (expectedPrefix) {{
        const b64 = match[1].replace(/-/g, '+').replace(/_/g, '/');
        const pad = '='.repeat((4 - (b64.length % 4)) % 4);
        const decoded = atob(b64 + pad);
        if (!decoded.startsWith(expectedPrefix)) return false;
      }}
      return true;
    }} catch (_e) {{
      return false;
    }}
  }}

  const root = document.getElementById('editorRoot');
  const viewport = document.getElementById('viewport');
  const stageInner = document.getElementById('stageInner');
  const baseImg = document.getElementById('baseImg');
  const canvas = document.getElementById('overlayCanvas');
  const statusText = document.getElementById('statusText');
  const confirmBtn = document.getElementById('confirmBtn');
  const renewBtn = document.getElementById('renewBtn');
  const undoBtn = document.getElementById('undoBtn');
  const clearBtn = document.getElementById('clearBtn');
  const fitBtn = document.getElementById('fitBtn');
  const closePolyBtn = document.getElementById('closePolyBtn');
  const brushRadius = document.getElementById('brushRadius');
  const brushVal = document.getElementById('brushVal');
  const instructionInput = document.getElementById('instructionInput');
  const expiresBadge = document.getElementById('expiresBadge');

  function formatLocalExpiry(isoStr) {{
    if (!isoStr) return '';
    const d = new Date(isoStr);
    if (Number.isNaN(d.getTime())) return String(isoStr).replace('T', ' ').replace(/Z$/, '');
    const pad = (n) => String(n).padStart(2, '0');
    return (
      d.getFullYear() +
      '-' +
      pad(d.getMonth() + 1) +
      '-' +
      pad(d.getDate()) +
      ' ' +
      pad(d.getHours()) +
      ':' +
      pad(d.getMinutes())
    );
  }}

  function refreshExpiryBadge() {{
    if (!expiresBadge) return;
    const targetIso = previewExpiresIso || editorExpiresIso;
    const formatted = formatLocalExpiry(targetIso);
    if (formatted) {{
      expiresBadge.textContent = '有效期至 ' + formatted;
    }}
  }}
  refreshExpiryBadge();

  let lastNotifiedHeight = 0;
  function notifyHostSize(onlyIfChanged) {{
    const h = Math.max(540, Math.ceil(root.getBoundingClientRect().height + 16));
    if (onlyIfChanged === true && h === lastNotifiedHeight) return;
    lastNotifiedHeight = h;
    if (window.parent && window.parent !== window) {{
      window.parent.postMessage({{ type: 'ui-size-change', payload: {{ height: h }} }}, '*');
    }}
  }}

  function setStatus(text, kind) {{
    statusText.textContent = text;
    statusText.className = 'status-line' + (kind ? ' ' + kind : '');
    notifyHostSize(true);
  }}

  function buildDraftSnapshot() {{
    return {{
      version: 1,
      imageId: IMAGE_ID,
      resourceUri: RESOURCE_URI,
      editorCapability: EDITOR_CAPABILITY,
      tool: state.tool,
      op: state.op,
      brushRadius: state.brushRadius,
      zoom: state.zoom,
      shapes: state.shapes.slice(),
      baseShapes: state.baseShapes ? state.baseShapes.slice() : [],
      points: state.points.slice(),
      pointOp: state.pointOp,
      instruction: instructionInput ? instructionInput.value : '',
      confirmedId: state.confirmedId,
      updatedAt: state.lastDraftUpdatedAt || Date.now()
    }};
  }}

  function persistDraftState() {{
    state.lastDraftUpdatedAt = Date.now();
    const snapshot = buildDraftSnapshot();
    try {{
      window.name = JSON.stringify({{
        draftKey: DRAFT_KEY,
        editorCapability: EDITOR_CAPABILITY,
        draft: snapshot
      }});
    }} catch (_e) {{}}
    if (window.parent && window.parent !== window) {{
      window.parent.postMessage({{
        type: 'ui-draft-save',
        messageId: nextRequestId('draft-save'),
        payload: {{
          draftKey: DRAFT_KEY,
          imageId: IMAGE_ID,
          resourceUri: RESOURCE_URI,
          editorCapability: EDITOR_CAPABILITY,
          sessionNonce: SESSION_NONCE,
          draft: snapshot
        }}
      }}, '*');
    }}
  }}

  function applyDraftSnapshot(draft) {{
    if (!draft || typeof draft !== 'object' || draft.imageId !== IMAGE_ID) return false;
    if (draft.editorCapability && draft.editorCapability !== EDITOR_CAPABILITY) return false;
    const incomingTs = Number(draft.updatedAt) || 0;
    if (state.lastDraftUpdatedAt > 0 && incomingTs > 0 && incomingTs < state.lastDraftUpdatedAt) {{
      return false;
    }}
    if (typeof draft.tool === 'string' && (draft.tool !== 'point' || SEGMENT_READY)) {{
      state.tool = draft.tool;
    }}
    if (draft.op === 'add' || draft.op === 'subtract') {{
      state.op = draft.op;
    }}
    if (Number.isFinite(Number(draft.brushRadius))) {{
      state.brushRadius = Math.max(4, Math.min(100, Number(draft.brushRadius)));
      brushRadius.value = String(state.brushRadius);
      brushVal.textContent = String(state.brushRadius);
    }}
    if (Number.isFinite(Number(draft.zoom)) && Number(draft.zoom) > 0) {{
      state.zoom = Number(draft.zoom);
    }}
    if (Array.isArray(draft.shapes)) {{
      state.shapes = draft.shapes.slice();
    }}
    if (Array.isArray(draft.baseShapes)) {{
      state.baseShapes = draft.baseShapes.slice();
    }}
    if (Array.isArray(draft.points)) {{
      state.points = draft.points.slice();
    }}
    if (draft.pointOp === 'add' || draft.pointOp === 'subtract') {{
      state.pointOp = draft.pointOp;
    }}
    if (typeof draft.instruction === 'string' && instructionInput) {{
      instructionInput.value = draft.instruction;
    }}
    state.confirmedId = typeof draft.confirmedId === 'string' ? draft.confirmedId : null;
    state.lastDraftUpdatedAt = incomingTs || Date.now();
    try {{
      window.name = JSON.stringify({{
        draftKey: DRAFT_KEY,
        editorCapability: EDITOR_CAPABILITY,
        draft: buildDraftSnapshot()
      }});
    }} catch (_e) {{}}
    syncStageLayout();
    if (state.shapes.length > 0 && !state.confirmedId) {{
      setStatus('已恢复未确认的选区草稿，可继续调整或点击“确认选区”。', '');
    }}
    return true;
  }}

  function applyRenewedLinks(links) {{
    if (!links || typeof links !== 'object') return false;
    const targetImgId = links.imageId || links.image_id;
    if (targetImgId && targetImgId !== IMAGE_ID) return false;
    let updated = false;
    const nextEditor = links.editorUrl || links.editor_url || links.signedUrl || links.signed_url;
    const nextPreview = links.previewUrl || links.preview_url;
    if (typeof nextEditor === 'string' && nextEditor) {{
      if (!isValidRenewedStudioUrl(nextEditor, SIGNED_URL, 'edt:')) {{
        return false;
      }}
      currentEditorUrl = nextEditor;
      updated = true;
    }}
    if (typeof nextPreview === 'string' && nextPreview) {{
      if (!isValidRenewedStudioUrl(nextPreview, PREVIEW_URL || SIGNED_URL, 'img:')) {{
        return false;
      }}
      currentPreviewUrl = nextPreview;
      if (!baseImg.complete || baseImg.naturalWidth === 0) {{
        baseImg.src = currentPreviewUrl;
      }}
      updated = true;
    }} else if (updated && (!baseImg.complete || baseImg.naturalWidth === 0)) {{
      baseImg.src = currentEditorUrl;
    }}
    const nextExpires = links.expiresAt || links.expires_at;
    if (typeof nextExpires === 'string' && nextExpires) {{
      editorExpiresIso = nextExpires;
    }}
    const nextPreviewExpires = links.previewExpiresAt || links.preview_expires_at;
    if (typeof nextPreviewExpires === 'string' && nextPreviewExpires) {{
      previewExpiresIso = nextPreviewExpires;
    }}
    if (updated) {{
      state.linksRenewed = true;
      refreshExpiryBadge();
      if (renewBtn) renewBtn.hidden = true;
      setStatus('已更新选区链接，可继续框选或点击“确认选区”。', '');
    }}
    return updated;
  }}

  async function renewDirectlyFromStudio() {{
    try {{
      const res = await fetch(currentEditorUrl, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{
          action: 'renew',
          resource_uri: RESOURCE_URI,
          image_id: IMAGE_ID,
          editor_url: currentEditorUrl,
          preview_url: currentPreviewUrl
        }})
      }});
      const rawText = await res.text();
      let data = {{}};
      if (rawText) {{
        try {{
          const parsed = JSON.parse(rawText);
          if (parsed && typeof parsed === 'object') data = parsed;
        }} catch (_e) {{}}
      }}
      if (res.ok && applyRenewedLinks(data)) {{
        return true;
      }}
    }} catch (_err) {{}}
    return false;
  }}

  async function requestLinkRenewal(mode) {{
    persistDraftState();
    if (renewBtn) renewBtn.hidden = false;
    if (mode === 'manual' || mode === 'tool') {{
      setStatus('正在刷新当前选区编辑链接（已保留未确认草稿）…', '');
    }}
    if (window.parent && window.parent !== window) {{
      const reqId = nextRequestId('renew');
      state.pendingRenewRequestId = reqId;
      window.parent.postMessage({{
        type: 'ui-link-renew',
        messageId: reqId,
        payload: {{
          resourceUri: RESOURCE_URI,
          imageId: IMAGE_ID,
          editorCapability: EDITOR_CAPABILITY,
          sessionNonce: SESSION_NONCE,
          reason: mode || 'manual',
          editorUrl: currentEditorUrl,
          previewUrl: currentPreviewUrl
        }}
      }}, '*');
    }}
    const ok = await renewDirectlyFromStudio();
    if (!ok && (mode === 'manual' || mode === 'tool') && (!window.parent || window.parent === window)) {{
      setStatus('选区链接刷新失败，请稍后重试。', 'err');
    }}
  }}

  function clampCoord(v) {{
    return Math.max(0, Math.min(999, Math.round(v)));
  }}

  function eventPoint(ev) {{
    const rect = canvas.getBoundingClientRect();
    return {{
      x: clampCoord((ev.clientX - rect.left) / Math.max(1, rect.width) * 999),
      y: clampCoord((ev.clientY - rect.top) / Math.max(1, rect.height) * 999)
    }};
  }}

  function toPx(pt, w, h) {{
    return [pt.x / 999 * w, pt.y / 999 * h];
  }}

  function traceShape(ctx, shape, w, h) {{
    if (!shape || !shape.points || !shape.points.length) return;
    if (shape.type === 'rect' && shape.points.length === 2) {{
      const [x1, y1] = toPx(shape.points[0], w, h);
      const [x2, y2] = toPx(shape.points[1], w, h);
      ctx.beginPath();
      ctx.rect(Math.min(x1, x2), Math.min(y1, y2), Math.abs(x2 - x1), Math.abs(y2 - y1));
      ctx.fill();
    }} else if (shape.type === 'polygon' && shape.points.length >= 3) {{
      ctx.beginPath();
      shape.points.forEach((pt, idx) => {{
        const [x, y] = toPx(pt, w, h);
        if (idx === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }});
      ctx.closePath();
      ctx.fill();
    }} else if (shape.type === 'stroke') {{
      const r = Math.max(2, (shape.radius || 22) / 999 * Math.min(w, h));
      ctx.save();
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      ctx.lineWidth = r * 2;
      ctx.beginPath();
      shape.points.forEach((pt, idx) => {{
        const [x, y] = toPx(pt, w, h);
        if (idx === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }});
      if (shape.points.length === 1) {{
        const [x, y] = toPx(shape.points[0], w, h);
        ctx.arc(x, y, r, 0, Math.PI * 2);
        ctx.fill();
      }} else {{
        ctx.stroke();
      }}
      ctx.restore();
    }}
  }}

  function syncStageLayout() {{
    const natW = baseImg.naturalWidth || {width};
    const natH = baseImg.naturalHeight || {height};
    const maxW = Math.max(280, viewport.clientWidth - 16);
    const maxH = Math.max(240, viewport.clientHeight - 16);
    const fitScale = Math.min(maxW / natW, maxH / natH, 1);
    const dispW = Math.max(120, Math.round(natW * fitScale * state.zoom));
    const dispH = Math.max(90, Math.round(natH * fitScale * state.zoom));
    stageInner.style.width = dispW + 'px';
    stageInner.style.height = dispH + 'px';
    if (canvas.width !== dispW || canvas.height !== dispH) {{
      canvas.width = dispW;
      canvas.height = dispH;
    }}
    renderOverlay();
    notifyHostSize();
  }}

  function hasAdditiveShape() {{
    return state.shapes.some(s => s.op === 'add') && state.activeMaskPixels > 0;
  }}

  function updateToolbarUI() {{
    document.querySelectorAll('[data-tool]').forEach(btn => {{
      btn.setAttribute('aria-pressed', btn.dataset.tool === state.tool ? 'true' : 'false');
    }});
    document.querySelectorAll('[data-op]').forEach(btn => {{
      btn.setAttribute('aria-pressed', btn.dataset.op === state.op ? 'true' : 'false');
    }});
    document.querySelectorAll('[data-zoom]').forEach(btn => {{
      btn.setAttribute('aria-pressed', Number(btn.dataset.zoom) === state.zoom ? 'true' : 'false');
    }});
    const hasHistory = state.shapes.length > 0 || state.points.length > 0 || Boolean(state.draft);
    undoBtn.disabled = !hasHistory || state.busy;
    clearBtn.disabled = !hasHistory || state.busy;
    closePolyBtn.hidden = !(state.tool === 'polygon' && state.draft && state.draft.points.length >= 3);
    confirmBtn.disabled = !hasAdditiveShape() || state.busy;
  }}

  function renderOverlay() {{
    const w = canvas.width || 1;
    const h = canvas.height || 1;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, w, h);

    const maskCanvas = document.createElement('canvas');
    maskCanvas.width = w;
    maskCanvas.height = h;
    const mctx = maskCanvas.getContext('2d');
    const allShapes = state.draft && state.draft.type !== 'polygon'
      ? state.shapes.concat([state.draft])
      : state.shapes;

    allShapes.forEach(shape => {{
      const isSubtract = shape.op === 'subtract';
      mctx.globalCompositeOperation = isSubtract ? 'destination-out' : 'source-over';
      mctx.fillStyle = isSubtract ? 'rgba(0, 0, 0, 1)' : 'rgba(37, 99, 235, 1)';
      mctx.strokeStyle = isSubtract ? 'rgba(0, 0, 0, 1)' : 'rgba(37, 99, 235, 1)';
      traceShape(mctx, shape, w, h);
    }});

    const maskData = mctx.getImageData(0, 0, w, h).data;
    let activePixels = 0;
    for (let i = 3; i < maskData.length; i += 4) {{
      if (maskData[i] > 0) activePixels += 1;
    }}
    state.activeMaskPixels = activePixels;

    ctx.save();
    ctx.globalAlpha = 0.36;
    ctx.drawImage(maskCanvas, 0, 0);
    ctx.restore();

    ctx.save();
    ctx.strokeStyle = '#60a5fa';
    ctx.lineWidth = 2;
    if (state.draft && state.draft.type === 'rect' && state.draft.points.length === 2) {{
      const [x1, y1] = toPx(state.draft.points[0], w, h);
      const [x2, y2] = toPx(state.draft.points[1], w, h);
      ctx.strokeRect(Math.min(x1, x2), Math.min(y1, y2), Math.abs(x2 - x1), Math.abs(y2 - y1));
    }}

    if (state.draft && state.draft.type === 'polygon') {{
      ctx.setLineDash([5, 4]);
      ctx.beginPath();
      state.draft.points.forEach((pt, idx) => {{
        const [x, y] = toPx(pt, w, h);
        if (idx === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }});
      if (state.draft.cursor) {{
        const [cx, cy] = toPx(state.draft.cursor, w, h);
        ctx.lineTo(cx, cy);
      }}
      ctx.stroke();
      ctx.setLineDash([]);
      state.draft.points.forEach((pt, idx) => {{
        const [x, y] = toPx(pt, w, h);
        ctx.fillStyle = idx === 0 ? '#2563eb' : '#ffffff';
        ctx.beginPath();
        ctx.arc(x, y, idx === 0 ? 5 : 4, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
      }});
    }}

    state.points.forEach(pt => {{
      const [x, y] = toPx(pt, w, h);
      ctx.fillStyle = pt.label === 0 ? '#dc2626' : '#2563eb';
      ctx.strokeStyle = '#ffffff';
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(x, y, 6, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
    }});
    ctx.restore();
    updateToolbarUI();
  }}

  async function runPointSegment() {{
    if (!state.points.length) return;
    state.busy = true;
    setStatus('正在识别点选区域...', '');
    updateToolbarUI();
    try {{
      const res = await fetch(currentEditorUrl, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{
          action: 'segment',
          points: state.points,
          operation: state.pointOp || 'add',
          existing: state.baseShapes && state.baseShapes.length
            ? {{ version: 1, shapes: state.baseShapes }}
            : null
        }})
      }});
      const rawText = await res.text();
      let data = {{}};
      if (rawText) {{
        try {{
          const parsed = JSON.parse(rawText);
          if (parsed && typeof parsed === 'object') data = parsed;
        }} catch (_parseErr) {{}}
      }}
      if (!res.ok) {{
        if (
          res.status === 401 ||
          res.status === 403 ||
          res.status === 404 ||
          res.status === 409 ||
          res.status === 410
        ) {{
          if (renewBtn) renewBtn.hidden = false;
          persistDraftState();
          throw new Error(
            (data.error || '当前选区编辑凭证已过期或已使用') +
              '，请点击“重新获取链接”刷新后继续（选区草稿已保留）'
          );
        }}
        throw new Error(data.error || '点选识别未成功，请改用框选、套索或画笔');
      }}
      state.shapes = data.shapes || [];
      state.confirmedId = null;
      persistDraftState();
      setStatus('已识别选区，可继续点选微调，或切换到画笔/框选修边，完成后点击“确认选区”。', '');
    }} catch (err) {{
      state.points.pop();
      setStatus(err.message || '点选识别失败，请使用框选、套索或画笔', 'err');
    }} finally {{
      state.busy = false;
      renderOverlay();
    }}
  }}

  function finishPolygon() {{
    if (!state.draft || state.draft.type !== 'polygon' || state.draft.points.length < 3) return;
    state.shapes.push({{
      type: 'polygon',
      op: state.draft.op,
      points: state.draft.points.slice(0, 1024)
    }});
    state.draft = null;
    state.points = [];
    state.confirmedId = null;
    persistDraftState();
    setStatus('已添加多边形选区，可继续加选/减选或点击“确认选区”。', '');
    renderOverlay();
  }}

  canvas.addEventListener('pointerdown', ev => {{
    if (state.busy || ev.button > 0) return;
    const pt = eventPoint(ev);
    if (state.tool === 'point') {{
      if (!SEGMENT_READY) {{
        setStatus('当前环境使用手动选区工具，请使用框选、多边形套索或画笔涂抹。', 'err');
        return;
      }}
      if (state.points.length === 0) {{
        if (ev.altKey) {{
          setStatus('请先点击要修改的目标区域（加选），再按住 Alt 点击不需要的部分。', 'err');
          return;
        }}
        if (state.op === 'subtract' && !state.shapes.some(s => s.op === 'add')) {{
          setStatus('请先点击要修改的目标区域（加选），再点不需要的部分（减选）。', 'err');
          return;
        }}
        state.baseShapes = state.shapes.slice();
        state.pointOp = state.op;
        state.points.push({{ x: pt.x, y: pt.y, label: 1 }});
      }} else {{
        const label = (ev.altKey || (state.op === 'subtract' && state.pointOp === 'add')) ? 0 : 1;
        state.points.push({{ x: pt.x, y: pt.y, label }});
      }}
      renderOverlay();
      runPointSegment();
      return;
    }}
    if (state.tool === 'polygon') {{
      if (!state.draft || state.draft.type !== 'polygon') {{
        state.draft = {{ type: 'polygon', op: state.op, points: [pt], cursor: pt }};
      }} else {{
        const first = state.draft.points[0];
        const [fx, fy] = toPx(first, canvas.width, canvas.height);
        const [cx, cy] = toPx(pt, canvas.width, canvas.height);
        if (state.draft.points.length >= 3 && Math.hypot(cx - fx, cy - fy) <= 12) {{
          finishPolygon();
          return;
        }}
        state.draft.points.push(pt);
      }}
      renderOverlay();
      return;
    }}
    canvas.setPointerCapture(ev.pointerId);
    if (state.tool === 'rect') {{
      state.draft = {{ type: 'rect', op: state.op, points: [pt, pt] }};
    }} else if (state.tool === 'stroke') {{
      state.draft = {{ type: 'stroke', op: state.op, radius: state.brushRadius, points: [pt] }};
    }}
    renderOverlay();
  }});

  canvas.addEventListener('pointermove', ev => {{
    if (!state.draft) return;
    const pt = eventPoint(ev);
    if (state.draft.type === 'polygon') {{
      state.draft.cursor = pt;
      renderOverlay();
      return;
    }}
    if (state.draft.type === 'rect') {{
      state.draft.points[1] = pt;
      renderOverlay();
    }} else if (state.draft.type === 'stroke') {{
      const last = state.draft.points[state.draft.points.length - 1];
      if (!last || Math.hypot(pt.x - last.x, pt.y - last.y) >= 3) {{
        state.draft.points.push(pt);
        renderOverlay();
      }}
    }}
  }});

  function finishDrag(ev) {{
    if (!state.draft || state.draft.type === 'polygon') return;
    const draft = state.draft;
    state.draft = null;
    if (draft.type === 'rect') {{
      const [a, b] = draft.points;
      const x1 = Math.min(a.x, b.x), y1 = Math.min(a.y, b.y);
      const x2 = Math.max(a.x, b.x), y2 = Math.max(a.y, b.y);
      if (x2 - x1 >= 6 && y2 - y1 >= 6) {{
        state.shapes.push({{ type: 'rect', op: draft.op, points: [{{ x: x1, y: y1 }}, {{ x: x2, y: y2 }}] }});
        state.points = [];
        state.confirmedId = null;
        persistDraftState();
        setStatus('已更新选区，完成后请点击“确认选区”。', '');
      }}
    }} else if (draft.type === 'stroke' && draft.points.length >= 1) {{
      state.shapes.push({{
        type: 'stroke',
        op: draft.op,
        radius: draft.radius,
        points: draft.points.slice(0, 1024)
      }});
      state.points = [];
      state.confirmedId = null;
      persistDraftState();
      setStatus('已更新画笔选区，完成后请点击“确认选区”。', '');
    }}
    renderOverlay();
  }}

  canvas.addEventListener('pointerup', finishDrag);
  canvas.addEventListener('pointercancel', finishDrag);
  canvas.addEventListener('dblclick', () => {{
    if (state.tool === 'polygon') finishPolygon();
  }});

  document.querySelectorAll('[data-tool]').forEach(btn => {{
    btn.addEventListener('click', () => {{
      if (btn.disabled) return;
      state.tool = btn.dataset.tool;
      state.draft = null;
      state.points = [];
      persistDraftState();
      renderOverlay();
    }});
  }});

  document.querySelectorAll('[data-op]').forEach(btn => {{
    btn.addEventListener('click', () => {{
      state.op = btn.dataset.op;
      persistDraftState();
      renderOverlay();
    }});
  }});

  document.querySelectorAll('[data-zoom]').forEach(btn => {{
    btn.addEventListener('click', () => {{
      state.zoom = Number(btn.dataset.zoom) || 1;
      persistDraftState();
      syncStageLayout();
    }});
  }});

  fitBtn.addEventListener('click', () => {{
    state.zoom = 1;
    persistDraftState();
    syncStageLayout();
  }});

  closePolyBtn.addEventListener('click', finishPolygon);

  brushRadius.addEventListener('input', () => {{
    state.brushRadius = Number(brushRadius.value) || 22;
    brushVal.textContent = String(state.brushRadius);
    persistDraftState();
  }});

  if (instructionInput) {{
    instructionInput.addEventListener('input', () => {{
      persistDraftState();
    }});
  }}

  undoBtn.addEventListener('click', () => {{
    if (state.draft) {{
      state.draft = null;
    }} else if (state.tool === 'point' && state.points.length) {{
      state.points.pop();
      if (state.points.some(p => p.label === 1)) {{
        runPointSegment();
        return;
      }}
      state.shapes = state.baseShapes ? state.baseShapes.slice() : [];
    }} else {{
      state.shapes.pop();
    }}
    state.confirmedId = null;
    persistDraftState();
    renderOverlay();
  }});

  clearBtn.addEventListener('click', () => {{
    state.shapes = [];
    state.baseShapes = [];
    state.points = [];
    state.draft = null;
    state.confirmedId = null;
    persistDraftState();
    setStatus('已清空选区，请重新框选、套索或涂抹需要修改的局部区域。', '');
    renderOverlay();
  }});

  if (renewBtn) {{
    renewBtn.addEventListener('click', () => {{
      requestLinkRenewal('manual');
    }});
  }}

  confirmBtn.addEventListener('click', async () => {{
    if (!hasAdditiveShape() || state.busy) return;
    state.busy = true;
    setStatus('正在保存并校验选区凭据...', '');
    updateToolbarUI();
    try {{
      const res = await fetch(currentEditorUrl, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{
          action: 'confirm',
          selection: {{ version: 1, shapes: state.shapes }}
        }})
      }});
      const rawText = await res.text();
      let data = {{}};
      if (rawText) {{
        try {{
          const parsed = JSON.parse(rawText);
          if (parsed && typeof parsed === 'object') data = parsed;
        }} catch (_parseErr) {{}}
      }}
      if (!res.ok) {{
        if (
          res.status === 401 ||
          res.status === 403 ||
          res.status === 404 ||
          res.status === 409 ||
          res.status === 410
        ) {{
          if (renewBtn) renewBtn.hidden = false;
          persistDraftState();
          throw new Error(
            (data.error || '当前选区编辑凭证已过期或已使用') +
              '，请点击“重新获取链接”刷新后继续（选区草稿已保留）'
          );
        }}
        throw new Error(data.error || '选区确认失败，请检查选区范围后重试');
      }}
      state.confirmedId = data.selection_id;
      statusText.dataset.selectionId = data.selection_id || '';
      persistDraftState();
      const note = (instructionInput.value || '').trim();
      const pxCount = (data.proof && data.proof.selected_pixels) || 0;
      setStatus(
        '已确认选区（选中 ' + pxCount.toLocaleString('zh-CN') + ' 像素），已向设计助手发送局部修改请求',
        'ok'
      );
      const promptMsg = note
        ? '已确认选区 ' + data.selection_id + '（底图 ' + IMAGE_ID + '），请执行局部修改：' + note
        : '已确认选区 ' + data.selection_id + '（底图 ' + IMAGE_ID + '）。';
      window.parent.postMessage({{
        type: 'prompt',
        payload: {{ prompt: promptMsg }}
      }}, '*');
    }} catch (err) {{
      setStatus(err.message || '选区确认失败，请重试', 'err');
    }} finally {{
      state.busy = false;
      updateToolbarUI();
      notifyHostSize();
    }}
  }});

  window.addEventListener('message', (ev) => {{
    if (!window.parent || window.parent === window || ev.source !== window.parent) return;
    if (!ev.data || typeof ev.data !== 'object') return;
    const msgType = ev.data.type;
    const payload = ev.data.payload || {{}};
    if (msgType === 'ui-lifecycle-iframe-render-data' && !state.renderDataReceived) {{
      state.renderDataReceived = true;
      const renderData = payload.renderData || payload;
      if (renderData && typeof renderData === 'object') {{
        window.iframeRenderData = renderData;
        const draftMap = renderData.drafts || {{}};
        const candidateDraft = renderData.selectionDraft || draftMap[DRAFT_KEY] || renderData.draft;
        if (candidateDraft) applyDraftSnapshot(candidateDraft);
        if (renderData.renewedLinks) {{
          applyRenewedLinks(renderData.renewedLinks[IMAGE_ID] || renderData.renewedLinks);
        }} else if (renderData.previewUrl || renderData.editorUrl) {{
          applyRenewedLinks(renderData);
        }}
      }}
    }} else if (msgType === 'ui-message-response') {{
      const messageId = ev.data.messageId;
      if (!messageId || typeof messageId !== 'string') return;
      const resp = payload.response || payload;
      if (!resp || typeof resp !== 'object') return;
      if (resp.sessionNonce && resp.sessionNonce !== SESSION_NONCE) return;
      if (messageId === state.pendingDraftRequestId) {{
        state.pendingDraftRequestId = null;
        if (resp.editorCapability && resp.editorCapability !== EDITOR_CAPABILITY) return;
        if (resp.draft) applyDraftSnapshot(resp.draft);
      }} else if (messageId === state.pendingRenewRequestId) {{
        state.pendingRenewRequestId = null;
        if (resp.previewUrl || resp.preview_url || resp.editorUrl || resp.editor_url || resp.signedUrl || resp.signed_url) {{
          applyRenewedLinks(resp);
        }}
      }}
    }}
  }});

  baseImg.addEventListener('load', () => {{
    const editorExpired = Date.now() >= new Date(editorExpiresIso).getTime();
    if (renewBtn) {{
      renewBtn.hidden = !editorExpired;
    }}
    syncStageLayout();
    if (state.linksRenewed && !state.confirmedId) {{
      setStatus('已更新选区链接，可继续框选或点击“确认选区”。', '');
    }} else if (state.shapes.length > 0 && !state.confirmedId) {{
      setStatus('已恢复未确认的选区草稿，可继续调整或点击“确认选区”。', '');
    }} else if (!state.confirmedId) {{
      setStatus('请在图上框选、套索或画笔涂抹要修改的局部区域，蓝色区域外将保持原样，完成后点击“确认选区”。', '');
    }}
    notifyHostSize();
  }});
  baseImg.addEventListener('error', () => {{
    if (!state.triedEditorFallback && currentEditorUrl && baseImg.src !== currentEditorUrl) {{
      state.triedEditorFallback = true;
      baseImg.src = currentEditorUrl;
      return;
    }}
    if (renewBtn) renewBtn.hidden = false;
    setStatus('底图预览链接已过期，请点击“重新获取链接”刷新后继续操作（当前选区草稿已保留）。', 'err');
    if (!state.autoRenewSent) {{
      state.autoRenewSent = true;
      requestLinkRenewal('check');
    }}
    notifyHostSize();
  }});

  try {{
    if (window.iframeRenderData && typeof window.iframeRenderData === 'object') {{
      if (window.iframeRenderData.selectionDraft) {{
        applyDraftSnapshot(window.iframeRenderData.selectionDraft);
      }}
      if (window.iframeRenderData.renewedLinks) {{
        applyRenewedLinks(window.iframeRenderData.renewedLinks);
      }}
    }}
    if (window.name && window.name.startsWith('{{')) {{
      const parsedName = JSON.parse(window.name);
      if (
        parsedName &&
        parsedName.draftKey === DRAFT_KEY &&
        (!parsedName.editorCapability || parsedName.editorCapability === EDITOR_CAPABILITY) &&
        parsedName.draft
      ) {{
        applyDraftSnapshot(parsedName.draft);
      }}
    }}
  }} catch (_e) {{}}

  if (window.parent && window.parent !== window) {{
    window.parent.postMessage({{ type: 'ui-lifecycle-iframe-ready' }}, '*');
    const loadReqId = nextRequestId('draft-load');
    state.pendingDraftRequestId = loadReqId;
    window.parent.postMessage({{
      type: 'ui-draft-load',
      messageId: loadReqId,
      payload: {{
        draftKey: DRAFT_KEY,
        imageId: IMAGE_ID,
        resourceUri: RESOURCE_URI,
        editorCapability: EDITOR_CAPABILITY,
        sessionNonce: SESSION_NONCE
      }}
    }}, '*');
  }}

  window.addEventListener('resize', syncStageLayout);
  if (typeof ResizeObserver !== 'undefined' && root) {{
    new ResizeObserver(() => {{
      notifyHostSize(true);
    }}).observe(root);
  }}
  baseImg.src = currentPreviewUrl;
  updateToolbarUI();
  notifyHostSize();
}})();
</script>
</body>
</html>"""


# 生成内联于聊天窗口的局部重绘前后对比与明显色差标注HTML（含滑块对比、切换原图与红底色差叠加）
def build_compare_viewer_html(
    *,
    image_id: str,
    base_id: str,
    selection_id: str,
    before_url: str,
    after_url: str,
    expires_at: str,
    width: int,
    height: int,
    selected_pixels: int,
) -> str:
    safe_image_id = html.escape(image_id, quote=True)
    safe_base_id = html.escape(base_id, quote=True)
    safe_sel_id = html.escape(selection_id, quote=True)
    safe_expires = html.escape(expires_at, quote=True)
    human_expires = html.escape(format_display_timestamp(expires_at), quote=True)
    before_url_js = json.dumps(before_url)
    after_url_js = json.dumps(after_url)
    image_id_js = json.dumps(image_id)
    base_id_js = json.dumps(base_id)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1" />
<title>局部修改前后对比（{width}×{height}）</title>
<style>
  :root {{
    color-scheme: light dark;
    --bg: #f6f4ef;
    --panel: #ffffff;
    --border: rgba(36, 32, 26, 0.15);
    --text: #231f1a;
    --muted: #655e54;
    --accent: #2563eb;
    --ok: #15803d;
    --stage-bg: #18181b;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #171614;
      --panel: #22201c;
      --border: rgba(245, 242, 235, 0.15);
      --text: #f5f2eb;
      --muted: #a69f93;
      --accent: #3b82f6;
      --ok: #22c55e;
      --stage-bg: #101013;
    }}
  }}
  * {{ box-sizing: border-box; }}
  html, body {{
    margin: 0;
    padding: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    background: var(--bg);
    color: var(--text);
  }}
  .compare-wrap {{
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-radius: 12px;
    background: var(--panel);
  }}
  .top-row, .ctrl-row {{
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }}
  .title-text {{
    font-size: 13px;
    font-weight: 600;
  }}
  .verify-badge {{
    font-size: 12px;
    color: var(--ok);
    font-weight: 600;
  }}
  .controls {{
    display: inline-flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }}
  button {{
    appearance: none;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--text);
    border-radius: 6px;
    padding: 5px 10px;
    font-size: 12px;
    cursor: pointer;
  }}
  button[aria-pressed="true"] {{
    background: var(--accent);
    color: #fff;
    border-color: var(--accent);
  }}
  label.diff-toggle {{
    display: inline-flex;
    align-items: center;
    gap: 5px;
    font-size: 12px;
    cursor: pointer;
  }}
  .slider-wrap {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    color: var(--muted);
  }}
  .stage-box {{
    position: relative;
    width: 100%;
    height: 380px;
    background: var(--stage-bg);
    border-radius: 8px;
    border: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    user-select: none;
  }}
  .stage-box canvas {{
    max-width: 100%;
    max-height: 100%;
    display: block;
  }}
  .meta-line {{
    font-size: 12px;
    color: var(--muted);
  }}
</style>
</head>
<body>
<div class="compare-wrap" id="compareRoot" data-image-id="{safe_image_id}" data-base-id="{safe_base_id}" data-selection-id="{safe_sel_id}">
  <div class="top-row">
    <span class="title-text">局部重绘效果对比（{width}×{height}）</span>
    <span class="verify-badge">✓ 选区外像素 100% 原样保留（0 像素改动 · 选区 {selected_pixels:,} 像素）</span>
  </div>
  <div class="ctrl-row">
    <div class="controls">
      <button type="button" id="toggleBeforeBtn" aria-pressed="false">对比修改前</button>
      <label class="diff-toggle">
        <input type="checkbox" id="diffCheck" />
        <span>标出明显色差</span>
      </label>
      <label class="slider-wrap" id="sliderWrap">
        <span>修改前</span>
        <input type="range" id="splitRange" min="0" max="100" value="50" />
        <span>修改后</span>
      </label>
      <button type="button" id="compareRenewBtn" hidden>刷新预览链接</button>
    </div>
    <span class="meta-line">画幅 {width}×{height} · <span id="compareExpiresBadge" data-expires="{safe_expires}">有效期至 {human_expires}</span></span>
  </div>
  <div class="stage-box" id="stageBox">
    <canvas id="compareCanvas"></canvas>
  </div>
</div>
<script>
(function() {{
  const INITIAL_BEFORE_URL = {before_url_js};
  const INITIAL_AFTER_URL = {after_url_js};
  let currentBeforeUrl = INITIAL_BEFORE_URL;
  let currentAfterUrl = INITIAL_AFTER_URL;
  const IMAGE_ID = {image_id_js};
  const BASE_ID = {base_id_js};
  const RESOURCE_URI = 'ui://zyb_studio/compare/' + IMAGE_ID;
  const SESSION_NONCE = (() => {{
    const buf = new Uint32Array(4);
    if (window.crypto && window.crypto.getRandomValues) {{
      window.crypto.getRandomValues(buf);
      return Array.from(buf, (n) => n.toString(16).padStart(8, '0')).join('');
    }}
    return String(Date.now()) + '-' + Math.random().toString(16).slice(2);
  }})();
  let reqCounter = 0;
  let pendingRenewRequestId = null;
  let renderDataReceived = false;

  const root = document.getElementById('compareRoot');
  const canvas = document.getElementById('compareCanvas');
  const toggleBeforeBtn = document.getElementById('toggleBeforeBtn');
  const diffCheck = document.getElementById('diffCheck');
  const splitRange = document.getElementById('splitRange');
  const sliderWrap = document.getElementById('sliderWrap');
  const compareRenewBtn = document.getElementById('compareRenewBtn');
  const compareExpiresBadge = document.getElementById('compareExpiresBadge');

  function nextRequestId(prefix) {{
    reqCounter += 1;
    return prefix + '-' + SESSION_NONCE + '-' + reqCounter;
  }}

  function isValidRenewedCompareUrl(candidateUrl, referenceUrl) {{
    if (typeof candidateUrl !== 'string' || !candidateUrl) return false;
    try {{
      const cand = new URL(candidateUrl, window.location.href);
      if (cand.protocol !== 'http:' && cand.protocol !== 'https:') return false;
      const ref = new URL(referenceUrl || INITIAL_AFTER_URL, window.location.href);
      if (cand.origin !== ref.origin) return false;
      const match = cand.pathname.match(/\\/(?:studio-files|files)\\/([A-Za-z0-9_-]+)\\.([0-9a-f]{{64}})$/);
      if (!match) return false;
      const b64 = match[1].replace(/-/g, '+').replace(/_/g, '/');
      const pad = '='.repeat((4 - (b64.length % 4)) % 4);
      const decoded = atob(b64 + pad);
      return decoded.startsWith('img:');
    }} catch (_e) {{
      return false;
    }}
  }}

  function formatLocalExpiry(isoStr) {{
    if (!isoStr) return '';
    const d = new Date(isoStr);
    if (Number.isNaN(d.getTime())) return String(isoStr).replace('T', ' ').replace(/Z$/, '');
    const pad = (n) => String(n).padStart(2, '0');
    return (
      d.getFullYear() +
      '-' +
      pad(d.getMonth() + 1) +
      '-' +
      pad(d.getDate()) +
      ' ' +
      pad(d.getHours()) +
      ':' +
      pad(d.getMinutes())
    );
  }}

  if (compareExpiresBadge && compareExpiresBadge.dataset.expires) {{
    const formatted = formatLocalExpiry(compareExpiresBadge.dataset.expires);
    if (formatted) {{
      compareExpiresBadge.textContent = '有效期至 ' + formatted;
    }}
  }}

  let showBeforeOnly = false;
  let showDiff = false;
  let splitRatio = 0.5;
  let diffImageData = null;

  const beforeImg = new Image();
  const afterImg = new Image();
  beforeImg.crossOrigin = 'anonymous';
  afterImg.crossOrigin = 'anonymous';
  let loadedCount = 0;

  function notifyHostSize() {{
    const h = Math.max(480, Math.ceil(root.getBoundingClientRect().height + 16));
    if (window.parent && window.parent !== window) {{
      window.parent.postMessage({{ type: 'ui-size-change', payload: {{ height: h }} }}, '*');
    }}
  }}

  function buildDiffOverlay(w, h) {{
    const a = document.createElement('canvas');
    const b = document.createElement('canvas');
    a.width = b.width = w;
    a.height = b.height = h;
    const actx = a.getContext('2d');
    const bctx = b.getContext('2d');
    actx.filter = bctx.filter = 'blur(1.5px)';
    actx.drawImage(beforeImg, 0, 0, w, h);
    bctx.drawImage(afterImg, 0, 0, w, h);
    const beforeData = actx.getImageData(0, 0, w, h).data;
    const afterFrame = bctx.getImageData(0, 0, w, h);
    const out = afterFrame.data;
    const changed = new Uint8Array(w * h);

    for (let p = 0, i = 0; p < changed.length; p += 1, i += 4) {{
      const d = Math.max(
        Math.abs(beforeData[i] - out[i]),
        Math.abs(beforeData[i + 1] - out[i + 1]),
        Math.abs(beforeData[i + 2] - out[i + 2])
      );
      if (d > 36) changed[p] = 1;
    }}

    const cleanFrame = canvas.getContext('2d');
    cleanFrame.drawImage(afterImg, 0, 0, w, h);
    const clean = cleanFrame.getImageData(0, 0, w, h);
    const dst = clean.data;

    for (let y = 1; y < h - 1; y += 1) {{
      for (let x = 1; x < w - 1; x += 1) {{
        const p = y * w + x;
        if (!changed[p]) continue;
        const neighbors = changed[p - 1] + changed[p + 1] + changed[p - w] + changed[p + w];
        if (neighbors < 3) continue;
        const i = p * 4;
        dst[i] = Math.round(dst[i] * 0.3 + 255 * 0.7);
        dst[i + 1] = Math.round(dst[i + 1] * 0.3);
        dst[i + 2] = Math.round(dst[i + 2] * 0.3);
      }}
    }}
    return clean;
  }}

  function render() {{
    if (loadedCount < 2) return;
    const natW = afterImg.naturalWidth || {width};
    const natH = afterImg.naturalHeight || {height};
    const scale = Math.min(1, 1400 / Math.max(natW, natH));
    const w = Math.max(1, Math.round(natW * scale));
    const h = Math.max(1, Math.round(natH * scale));
    if (canvas.width !== w || canvas.height !== h) {{
      canvas.width = w;
      canvas.height = h;
      diffImageData = null;
    }}
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, w, h);

    if (showBeforeOnly) {{
      ctx.drawImage(beforeImg, 0, 0, w, h);
      return;
    }}
    if (showDiff) {{
      if (!diffImageData) {{
        diffImageData = buildDiffOverlay(w, h);
      }}
      ctx.putImageData(diffImageData, 0, 0);
      return;
    }}

    ctx.drawImage(afterImg, 0, 0, w, h);
    const splitX = Math.round(w * splitRatio);
    if (splitX > 0) {{
      ctx.save();
      ctx.beginPath();
      ctx.rect(0, 0, splitX, h);
      ctx.clip();
      ctx.drawImage(beforeImg, 0, 0, w, h);
      ctx.restore();
    }}
    ctx.save();
    ctx.strokeStyle = '#ffffff';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(splitX, 0);
    ctx.lineTo(splitX, h);
    ctx.stroke();
    ctx.restore();
  }}

  function onImageLoaded() {{
    loadedCount += 1;
    if (loadedCount >= 2) {{
      if (compareRenewBtn) compareRenewBtn.hidden = true;
      render();
      notifyHostSize();
    }}
  }}

  function onImageError() {{
    if (compareRenewBtn) compareRenewBtn.hidden = false;
    notifyHostSize();
  }}

  beforeImg.addEventListener('load', onImageLoaded);
  afterImg.addEventListener('load', onImageLoaded);
  beforeImg.addEventListener('error', onImageError);
  afterImg.addEventListener('error', onImageError);

  function applyRenewedCompareLinks(links) {{
    if (!links || typeof links !== 'object') return false;
    const nextBefore = links.beforeUrl || links.before_url;
    const nextAfter = links.afterUrl || links.after_url;
    if (!isValidRenewedCompareUrl(nextBefore, INITIAL_BEFORE_URL)) return false;
    if (!isValidRenewedCompareUrl(nextAfter, INITIAL_AFTER_URL)) return false;
    currentBeforeUrl = nextBefore;
    currentAfterUrl = nextAfter;
    loadedCount = 0;
    diffImageData = null;
    beforeImg.src = currentBeforeUrl;
    afterImg.src = currentAfterUrl;
    const nextExpires = links.expiresAt || links.expires_at;
    if (nextExpires && compareExpiresBadge) {{
      compareExpiresBadge.dataset.expires = nextExpires;
      compareExpiresBadge.textContent = '有效期至 ' + formatLocalExpiry(nextExpires);
    }}
    if (compareRenewBtn) compareRenewBtn.hidden = true;
    return true;
  }}

  async function renewCompareDirectly() {{
    try {{
      const res = await fetch(currentAfterUrl, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{
          action: 'renew',
          resource_uri: RESOURCE_URI,
          image_id: IMAGE_ID,
          base_id: BASE_ID,
          before_url: currentBeforeUrl,
          after_url: currentAfterUrl
        }})
      }});
      const rawText = await res.text();
      let data = {{}};
      if (rawText) {{
        try {{
          const parsed = JSON.parse(rawText);
          if (parsed && typeof parsed === 'object') data = parsed;
        }} catch (_e) {{}}
      }}
      if (res.ok && applyRenewedCompareLinks(data)) {{
        return true;
      }}
    }} catch (_err) {{}}
    return false;
  }}

  if (compareRenewBtn) {{
    compareRenewBtn.addEventListener('click', async () => {{
      if (window.parent && window.parent !== window) {{
        pendingRenewRequestId = nextRequestId('renew-compare');
        window.parent.postMessage({{
          type: 'ui-link-renew',
          messageId: pendingRenewRequestId,
          payload: {{
            resourceUri: RESOURCE_URI,
            imageId: IMAGE_ID,
            baseId: BASE_ID,
            sessionNonce: SESSION_NONCE,
            beforeUrl: currentBeforeUrl,
            afterUrl: currentAfterUrl
          }}
        }}, '*');
      }}
      await renewCompareDirectly();
    }});
  }}

  window.addEventListener('message', (ev) => {{
    if (!window.parent || window.parent === window || ev.source !== window.parent) return;
    if (!ev.data || typeof ev.data !== 'object') return;
    const msgType = ev.data.type;
    const payload = ev.data.payload || {{}};
    if (msgType === 'ui-lifecycle-iframe-render-data' && !renderDataReceived) {{
      renderDataReceived = true;
      const renderData = payload.renderData || payload;
      if (renderData && typeof renderData === 'object') {{
        applyRenewedCompareLinks(renderData.renewedLinks || renderData);
      }}
      return;
    }}
    if (msgType === 'ui-message-response' && ev.data.messageId && ev.data.messageId === pendingRenewRequestId) {{
      pendingRenewRequestId = null;
      const resp = payload.response || payload;
      if (!resp || typeof resp !== 'object') return;
      if (resp.sessionNonce && resp.sessionNonce !== SESSION_NONCE) return;
      applyRenewedCompareLinks(resp);
    }}
  }});

  toggleBeforeBtn.addEventListener('click', () => {{
    showBeforeOnly = !showBeforeOnly;
    toggleBeforeBtn.setAttribute('aria-pressed', showBeforeOnly ? 'true' : 'false');
    toggleBeforeBtn.textContent = showBeforeOnly ? '返回修改后' : '对比修改前';
    if (showBeforeOnly) {{
      showDiff = false;
      diffCheck.checked = false;
    }}
    sliderWrap.style.opacity = (showBeforeOnly || showDiff) ? '0.45' : '1';
    render();
  }});

  diffCheck.addEventListener('change', () => {{
    showDiff = diffCheck.checked;
    if (showDiff) {{
      showBeforeOnly = false;
      toggleBeforeBtn.setAttribute('aria-pressed', 'false');
      toggleBeforeBtn.textContent = '对比修改前';
    }}
    sliderWrap.style.opacity = (showBeforeOnly || showDiff) ? '0.45' : '1';
    render();
  }});

  splitRange.addEventListener('input', () => {{
    splitRatio = (Number(splitRange.value) || 50) / 100;
    showBeforeOnly = false;
    showDiff = false;
    toggleBeforeBtn.setAttribute('aria-pressed', 'false');
    toggleBeforeBtn.textContent = '对比修改前';
    diffCheck.checked = false;
    sliderWrap.style.opacity = '1';
    render();
  }});

  window.addEventListener('resize', notifyHostSize);
  if (typeof ResizeObserver !== 'undefined' && root) {{
    new ResizeObserver(() => {{
      notifyHostSize();
    }}).observe(root);
  }}
  beforeImg.src = currentBeforeUrl;
  afterImg.src = currentAfterUrl;
  notifyHostSize();
}})();
</script>
</body>
</html>"""
