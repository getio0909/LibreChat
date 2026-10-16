import asyncio
import base64
import binascii
import hashlib
import io
import json
import math
import os
import re
import shutil
import time
import warnings
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageCms, ImageOps

from .config import StudioRules, StudioSettings
from .store import ensure_safe_subpath, read_bytes_no_follow

# 默认生图接口地址、模型编号与档位像素基准
BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
MODEL = "doubao-seedream-5-0-pro-260628"
SIZES = {"1K": 1024, "2K": 2048}
NATIVE: dict[str, dict[str, tuple[int, int]]] = {
    "1K": {
        "1:1": (1024, 1024),
        "4:3": (1152, 864),
        "3:4": (864, 1152),
        "16:9": (1280, 720),
        "9:16": (720, 1280),
    },
    "2K": {
        "1:1": (2048, 2048),
        "4:3": (2304, 1728),
        "3:4": (1728, 2304),
        "16:9": (2560, 1440),
        "9:16": (1440, 2560),
    },
}
MAX_REFERENCES = 10
MAX_EDIT_REFERENCES = 9
MAX_INPUT_PIXELS = 40_000_000
MAX_OUTPUT_PIXELS = 24_000_000
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_DISPLAY_BYTES = 3 * 1024 * 1024
REFERENCE_USES = ("参考", "物件", "造型", "材质", "纹样", "风格")


# 请求发出后结果状态不确定或非saved终态的异常类型（附带job_id与计费警告且不自动重试）
class GenerationUncertain(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        job_id: str | None = None,
        state: str = "uncertain",
    ) -> None:
        super().__init__(message)
        self.job_id = job_id
        self.state = state


# 以0600权限和fsync原子写入receipt.json状态文件
def write_receipt(directory: Path, values: dict[str, Any]) -> None:
    target = directory / "receipt.json"
    temporary = directory / "receipt.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(values, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)


# 尽力将receipt状态更新为uncertain，忽略底层存储异常以始终保留计费警告
def _mark_uncertain(directory: Path, values: dict[str, Any], **extra: Any) -> None:
    values.update(state="uncertain", finished=time.time(), **extra)
    try:
        write_receipt(directory, values)
    except OSError:
        pass


# 按原图宽高比计算1K或2K档位下符合16倍数、面积范围与2.5%宽高比误差上限的目标尺寸
def source_dimensions(
    size: str, source_size: tuple[int, int] | list[int]
) -> tuple[int, int]:
    if (
        size not in NATIVE
        or not isinstance(source_size, (tuple, list))
        or len(source_size) != 2
    ):
        raise ValueError("Invalid source image dimensions")
    w, h = source_size
    if (
        any(not isinstance(v, int) or isinstance(v, bool) or v < 1 for v in (w, h))
        or not 1 / 16 <= w / h <= 16
    ):
        raise ValueError("Source image aspect ratio is out of supported range")
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
        raise ValueError(
            "Cannot generate at source aspect ratio; please choose a standard aspect ratio"
        )
    return best_pair


# 规范化参考图片并返回Data URI与EXIF方向校正后的像素尺寸
def normalize_reference_with_size(
    path: str | Path, allowed_roots: tuple[Path, ...], *, linework: bool = False
) -> tuple[str, tuple[int, int]]:
    candidate = Path(path)
    matched_root: Path | None = None
    for root in allowed_roots:
        if not root.exists():
            continue
        try:
            ensure_safe_subpath(candidate, root)
            matched_root = root
            break
        except (OSError, ValueError):
            continue
    if matched_root is None:
        raise ValueError("Reference image is outside allowed directories")

    raw_bytes = read_bytes_no_follow(candidate, matched_root)
    previous_max = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = MAX_INPUT_PIXELS
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw_bytes)) as source:
                if source.format not in ("JPEG", "PNG", "WEBP"):
                    raise ValueError("Unsupported reference image format")
                if source.width * source.height > MAX_INPUT_PIXELS:
                    raise ValueError(
                        "Reference image exceeds 40 MP maximum resolution limit"
                    )
                im = ImageOps.exif_transpose(source)
                oriented_size = (im.width, im.height)
                has_alpha = "A" in im.getbands() or "transparency" in im.info
                alpha = im.convert("RGBA").getchannel("A") if has_alpha else None
                icc_bytes = source.info.get("icc_profile")
                if icc_bytes:
                    profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_bytes))
                    space = profile.profile.xcolor_space.strip()
                    mode = {
                        "RGB": "RGB",
                        "CMYK": "CMYK",
                        "GRAY": "L",
                        "LAB": "LAB",
                    }.get(space, im.mode)
                    color = im if im.mode == mode else im.convert(mode)
                    im = ImageCms.profileToProfile(
                        color,
                        profile,
                        ImageCms.createProfile("sRGB"),
                        outputMode="RGB",
                    )
                elif im.mode != "RGB":
                    im = im.convert("RGB")
                im = im.convert("RGBA")
                if alpha is not None:
                    im.putalpha(alpha)
                im = Image.alpha_composite(
                    Image.new("RGBA", im.size, "white"), im
                ).convert("RGB")
            im.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
            im.info.clear()
            output = io.BytesIO()
            if linework:
                im.save(output, format="PNG")
                prefix = "data:image/png;base64,"
            else:
                im.save(output, format="JPEG", quality=92)
                prefix = "data:image/jpeg;base64,"
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError(
            "Reference image exceeds 40 MP maximum resolution limit"
        ) from None
    except ValueError:
        raise
    except (OSError, ImageCms.PyCMSError):
        raise ValueError("Reference image cannot be safely decoded") from None
    finally:
        Image.MAX_IMAGE_PIXELS = previous_max
    data_uri = prefix + base64.b64encode(output.getvalue()).decode("ascii")
    return data_uri, oriented_size


# 规范化参考图片：校验目录、限制40MP、EXIF方向转置并剥离、ICC/CMYK转sRGB、长边缩放至2048以内
def normalize_reference(
    path: str | Path, allowed_roots: tuple[Path, ...], *, linework: bool = False
) -> str:
    data_uri, _ = normalize_reference_with_size(path, allowed_roots, linework=linework)
    return data_uri


# 火山方舟创建3D生成任务API输入图片限制（官方文档：https://docs.volcengine.com/docs/ark/create-3d-generation-task-api?lang=zh 与 https://www.volcengine.com/docs/82379/1593703）
SEED3D_MIN_ASPECT_RATIO = 0.4
SEED3D_MAX_ASPECT_RATIO = 2.5
SEED3D_MIN_EDGE_PX = 300
SEED3D_MAX_EDGE_PX = 6000
SEED3D_MAX_NORMALIZED_EDGE_PX = 2048
SEED3D_MAX_IMAGE_BYTES = 10 * 1024 * 1024
SEED3D_PAD_COLOR = (240, 238, 233)


# 为3D模型生成准备输入图片：拒绝任一边小于300px（禁止放大），以中性背景填充使宽高比落入0.4–2.5并限制长边不超过2048px
def prepare_seed3d_image_with_size(
    path: str | Path, allowed_roots: tuple[Path, ...]
) -> tuple[str, tuple[int, int]]:
    candidate = Path(path)
    matched_root: Path | None = None
    for root in allowed_roots:
        if not root.exists():
            continue
        try:
            ensure_safe_subpath(candidate, root)
            matched_root = root
            break
        except (OSError, ValueError):
            continue
    if matched_root is None:
        raise ValueError("Reference image is outside allowed directories")

    raw_bytes = read_bytes_no_follow(
        candidate, matched_root, max_bytes=SEED3D_MAX_IMAGE_BYTES
    )
    previous_max = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = MAX_INPUT_PIXELS
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw_bytes)) as source:
                if source.format not in ("JPEG", "PNG", "WEBP"):
                    raise ValueError("Unsupported reference image format")
                if source.width * source.height > MAX_INPUT_PIXELS:
                    raise ValueError(
                        "Reference image exceeds 40 MP maximum resolution limit"
                    )
                im = ImageOps.exif_transpose(source)
                src_w, src_h = im.size
                if src_w < SEED3D_MIN_EDGE_PX or src_h < SEED3D_MIN_EDGE_PX:
                    raise ValueError(
                        f"Source image dimensions ({src_w}x{src_h} px) are too small for 3D model generation; "
                        f"both width and height must be at least {SEED3D_MIN_EDGE_PX} px (upscaling is not permitted). "
                        "Please select or generate a higher-resolution image (>= 300x300 px)."
                    )

                has_alpha = "A" in im.getbands() or "transparency" in im.info
                alpha = im.convert("RGBA").getchannel("A") if has_alpha else None
                icc_bytes = source.info.get("icc_profile")
                if icc_bytes:
                    profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_bytes))
                    space = profile.profile.xcolor_space.strip()
                    mode = {
                        "RGB": "RGB",
                        "CMYK": "CMYK",
                        "GRAY": "L",
                        "LAB": "LAB",
                    }.get(space, im.mode)
                    color = im if im.mode == mode else im.convert(mode)
                    im = ImageCms.profileToProfile(
                        color,
                        profile,
                        ImageCms.createProfile("sRGB"),
                        outputMode="RGB",
                    )
                elif im.mode != "RGB":
                    im = im.convert("RGB")
                im = im.convert("RGBA")
                if alpha is not None:
                    im.putalpha(alpha)
                im = Image.alpha_composite(
                    Image.new("RGBA", im.size, (*SEED3D_PAD_COLOR, 255)), im
                ).convert("RGB")

            # 若宽高比超出[0.4, 2.5]，使用中性石材浅灰背景居中填充至合法宽高比范围
            aspect = src_w / src_h
            if aspect < SEED3D_MIN_ASPECT_RATIO:
                padded_w = max(src_w, math.ceil(src_h * SEED3D_MIN_ASPECT_RATIO))
                padded_h = src_h
                canvas = Image.new("RGB", (padded_w, padded_h), SEED3D_PAD_COLOR)
                canvas.paste(im, ((padded_w - src_w) // 2, 0))
                im = canvas
            elif aspect > SEED3D_MAX_ASPECT_RATIO:
                padded_w = src_w
                padded_h = max(src_h, math.ceil(src_w / SEED3D_MAX_ASPECT_RATIO))
                canvas = Image.new("RGB", (padded_w, padded_h), SEED3D_PAD_COLOR)
                canvas.paste(im, (0, (padded_h - src_h) // 2))
                im = canvas

            # 将长边限制在2048px以内，并确保整数舍入后宽高比仍在[0.4, 2.5]且短边>=300px
            cur_w, cur_h = im.size
            max_edge = max(cur_w, cur_h)
            if max_edge > SEED3D_MAX_NORMALIZED_EDGE_PX:
                scale = SEED3D_MAX_NORMALIZED_EDGE_PX / max_edge
                new_w = max(SEED3D_MIN_EDGE_PX, round(cur_w * scale))
                new_h = max(SEED3D_MIN_EDGE_PX, round(cur_h * scale))
                if new_w / new_h < SEED3D_MIN_ASPECT_RATIO:
                    new_w = max(
                        SEED3D_MIN_EDGE_PX, math.ceil(new_h * SEED3D_MIN_ASPECT_RATIO)
                    )
                elif new_w / new_h > SEED3D_MAX_ASPECT_RATIO:
                    new_h = max(
                        SEED3D_MIN_EDGE_PX, math.ceil(new_w / SEED3D_MAX_ASPECT_RATIO)
                    )
                im = im.resize((new_w, new_h), Image.Resampling.LANCZOS)

            im.info.clear()
            final_size = (im.width, im.height)
            output = io.BytesIO()
            im.save(output, format="JPEG", quality=92)
            encoded = output.getvalue()
            if len(encoded) > SEED3D_MAX_IMAGE_BYTES:
                output = io.BytesIO()
                im.save(output, format="JPEG", quality=82, optimize=True)
                encoded = output.getvalue()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError(
            "Reference image exceeds 40 MP maximum resolution limit"
        ) from None
    except ValueError:
        raise
    except (OSError, ImageCms.PyCMSError):
        raise ValueError("Reference image cannot be safely decoded") from None
    finally:
        Image.MAX_IMAGE_PIXELS = previous_max
    data_uri = "data:image/jpeg;base64," + base64.b64encode(encoded).decode("ascii")
    return data_uri, final_size


# 为3D模型生成准备输入图片并返回Data URI
def prepare_seed3d_image(path: str | Path, allowed_roots: tuple[Path, ...]) -> str:
    data_uri, _ = prepare_seed3d_image_with_size(path, allowed_roots)
    return data_uri


# 校验材质对比部位名称：限制40字符以内，拒绝石材名称/别名/检索词以及附加的增删改否定指令
def comparison_target(value: Any, catalog: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 40
        or "\x00" in value
        or not value.strip()
    ):
        raise ValueError("target must be a non-empty string of at most 40 characters")
    cleaned = value.strip()
    if hasattr(catalog, "materials"):
        products = catalog.materials
    elif isinstance(catalog, dict):
        products = catalog.values()
    else:
        products = catalog or ()

    names: set[str] = set()
    for product in products:
        if isinstance(product, dict):
            candidates = (
                product.get("name", ""),
                *(product.get("aliases") or ()),
                *(product.get("search_terms") or ()),
            )
        else:
            candidates = (
                getattr(product, "name", ""),
                *(getattr(product, "aliases", ()) or ()),
                *(getattr(product, "search_terms", ()) or ()),
            )
        for name in candidates:
            if name:
                names.add(str(name))

    if any(name in cleaned for name in names) or re.search(
        r"不|别|除了|去掉|移除|取消|换成|改成", cleaned
    ):
        raise ValueError(
            "target must only name the surface or part to replace (e.g. 背景墙, 佛像背后的背光墙) without material names or extra editing instructions"
        )
    return cleaned


# 将商业石材名称与别名替换为图N所示石材
def neutralize_stone_names(text: str, replacements: dict[str, str]) -> str:
    result = text
    for alias in sorted(replacements, key=len, reverse=True):
        if not alias:
            continue
        pattern = re.escape(alias) + ("(?!画)" if alias == "敦煌壁" else "")
        result = re.sub(pattern, lambda _, target=replacements[alias]: target, result)
    return result


# 仅当提供了匹配目录石材的参考图时，将该石材名称与别名映射为图N所示石材
def build_stone_replacements(
    rules: StudioRules,
    unique_paths: list[Path],
    references: list[dict[str, Any]],
    *,
    has_base: bool,
) -> dict[str, str]:
    offset = 2 if has_base else 1
    product_samples: dict[str, list[str]] = {}

    for ref in references:
        ref_path = Path(ref["path"])
        idx = unique_paths.index(ref_path) + offset
        sample = f"图{idx}"
        extra_mat_ids = " ".join(ref.get("material_ids") or ())
        note_text = (
            f"{ref.get('note') or ''} {ref.get('id') or ''} "
            f"{ref.get('material_id') or ''} {extra_mat_ids} {ref_path.stem}"
        )
        for product in rules.materials:
            match_keys = [
                product.id,
                product.name,
                *product.aliases,
                *product.sample_asset_ids,
                *product.all_slab_asset_ids,
            ]
            if any(
                key
                and re.search(
                    re.escape(key) + ("(?!画)" if key == "敦煌壁" else ""),
                    note_text,
                )
                is not None
                for key in match_keys
            ):
                samples = product_samples.setdefault(product.id, [])
                if sample not in samples:
                    samples.append(sample)

    replacements: dict[str, str] = {}
    for product in rules.materials:
        samples = product_samples.get(product.id)
        if not samples:
            continue
        target = "、".join(samples) + "所示石材"
        for name in [product.name, *product.aliases]:
            if name:
                replacements[name] = target
    return replacements


# 组装概念生图提示词、参考规则与参考图路径列表
def build_generate_prompt(
    prompt: str,
    references: list[dict[str, Any]],
    rules: StudioRules,
) -> tuple[str, list[Path]]:
    if len(references) > MAX_REFERENCES:
        raise ValueError(f"At most {MAX_REFERENCES} reference images are allowed")
    unique_paths: list[Path] = []
    for ref in references:
        use = ref.get("use", "参考")
        if use not in REFERENCE_USES:
            raise ValueError(f"Invalid reference use: {use}")
        p = Path(ref["path"])
        if p not in unique_paths:
            unique_paths.append(p)

    replacements = build_stone_replacements(
        rules, unique_paths, references, has_base=False
    )
    clean_prompt = neutralize_stone_names(prompt.strip(), replacements)
    if not unique_paths:
        return rules.render_prefix + clean_prompt, []

    lines: list[str] = []
    for ref in references:
        p = Path(ref["path"])
        sample = f"图{unique_paths.index(p) + 1}"
        use = ref.get("use", "参考")
        raw_note = (ref.get("note") or "").strip()
        part = (
            neutralize_stone_names(raw_note, replacements) if raw_note else use
        ).rstrip("。；，.!?;:,！？")
        lines.append(f"从{sample}取用{part}，用于新方案的相应部分。")
        if use == "材质":
            lines.append(
                f"{sample}只提供颜色与纹理，用于新设计的指定部位，不取用样图背景、地面、支架、标签与其他物件。"
            )

    compose_tail = (
        "按说明组合成新的完整设计，各参考图地位相同，不默认第一张是底图。"
        "只取指定内容，统一比例、透视、光线与构图，不拼成对照图或分栏图。"
    )
    body = "\n".join([clean_prompt, *lines, compose_tail])
    full_prompt = f"{rules.render_prefix}{rules.reference_rules}\n{body}"
    return full_prompt, unique_paths


# 组装整图修改提示词、底图保护规则与参考图路径列表
def build_edit_prompt(
    instruction: str,
    references: list[dict[str, Any]],
    rules: StudioRules,
) -> tuple[str, list[Path]]:
    if len(references) > MAX_EDIT_REFERENCES:
        raise ValueError(
            f"At most {MAX_EDIT_REFERENCES} additional reference images are allowed for edits"
        )
    unique_paths: list[Path] = []
    for ref in references:
        use = ref.get("use", "参考")
        if use not in REFERENCE_USES:
            raise ValueError(f"Invalid reference use: {use}")
        p = Path(ref["path"])
        if p not in unique_paths:
            unique_paths.append(p)

    replacements = build_stone_replacements(
        rules, unique_paths, references, has_base=True
    )
    clean_instruction = neutralize_stone_names(instruction.strip(), replacements)

    lines: list[str] = []
    for ref in references:
        p = Path(ref["path"])
        sample = f"图{unique_paths.index(p) + 2}"
        use = ref.get("use", "参考")
        raw_note = (ref.get("note") or "").strip()
        part = (
            neutralize_stone_names(raw_note, replacements) if raw_note else use
        ).rstrip("。；，.!?;:,！？")
        lines.append(f"{sample}提供本次修改的{part}，应用部位以修改说明为准。")
        if use == "材质":
            lines.append(
                f"{sample}只提供颜色与纹理，保留修改部位原有造型和细节，不取用样图背景、地面、支架、标签与其他物件。"
            )

    preserve_tail = (
        "图1是唯一的修改底图。保留底图的构图、视角和前景遮挡。"
        "未要求修改的物体、面部表情、五官、姿态、手势、小像和装饰细节须与图1一致。"
        "修改范围以说明为准；说明未写整面或全部时，只改点名的部位，保留相邻的边框、线脚和板块。"
    )
    body_parts = [clean_instruction, *lines, preserve_tail]
    body = "\n".join(part for part in body_parts if part)
    full_prompt = f"{rules.edit_prefix}{rules.edit_rules}\n{body}"
    return full_prompt, unique_paths


# 组装局部选区修改提示词（附加局部裁切框内的归一化<bbox>约束及可选原样贴入说明）
def build_local_edit_prompt(
    instruction: str,
    references: list[dict[str, Any]],
    rules: StudioRules,
    *,
    region_bbox: tuple[int, int, int, int],
    placement_instruction: str | None = None,
) -> tuple[str, list[Path]]:
    base_prompt, unique_paths = build_edit_prompt(instruction, references, rules)
    rx1, ry1, rx2, ry2 = region_bbox
    region_line = (
        f"图1是原场景的局部裁图。仅修改 <bbox>{rx1} {ry1} {rx2} {ry2}</bbox> 内的指定内容，"
        "保持构图位置、边缘结构与周围光线一致，不添加选区标记。"
    )
    extra_parts = [
        part for part in (placement_instruction, region_line) if part and part.strip()
    ]
    return "\n".join([base_prompt, *extra_parts]), unique_paths


# 生成用于MCP结果展示的压缩图片（JPEG或WebP，严格不超过3MB），不修改原图字节
def encode_display_image(
    raw_bytes: bytes, max_bytes: int = MAX_DISPLAY_BYTES
) -> tuple[bytes, str]:
    with Image.open(io.BytesIO(raw_bytes)) as img:
        fmt = img.format
        if fmt in ("JPEG", "WEBP") and len(raw_bytes) <= max_bytes:
            return raw_bytes, ("image/jpeg" if fmt == "JPEG" else "image/webp")
        rgb = ImageOps.exif_transpose(img).convert("RGB")

    for quality in (90, 82, 74, 65, 55):
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        if len(data) <= max_bytes:
            return data, "image/jpeg"

    working = rgb.copy()
    while True:
        working = working.resize(
            (max(64, int(working.width * 0.8)), max(64, int(working.height * 0.8))),
            Image.Resampling.LANCZOS,
        )
        buf = io.BytesIO()
        working.save(buf, format="JPEG", quality=82, optimize=True)
        data = buf.getvalue()
        if len(data) <= max_bytes or (working.width <= 64 and working.height <= 64):
            return data, "image/jpeg"


# 调用云端图像生成接口并原子保存原始字节与receipt.json（支持局部选区回贴与选区外像素零改动校验）
async def generate_seedream(
    settings: StudioSettings,
    prompt: str,
    references: list[str],
    size: str,
    aspect: str,
    destination: Path,
    *,
    source_size: tuple[int, int] | None = None,
    local_composite: dict[str, Any] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    if size not in NATIVE or (aspect != "source" and aspect not in NATIVE[size]):
        raise ValueError("Invalid image size or aspect ratio")
    if len(references) > MAX_REFERENCES:
        raise ValueError(f"Reference image count exceeds limit of {MAX_REFERENCES}")

    destination = Path(destination)
    destination.mkdir(parents=True, mode=0o700, exist_ok=True)
    if destination.is_symlink() or (destination / "receipt.json").exists():
        raise ValueError("Job directory already contains a generation receipt")
    if shutil.disk_usage(destination).free < 2 * MAX_RESPONSE_BYTES:
        raise ValueError("Insufficient disk space to submit image generation")

    key = settings.ark_api_key.strip()
    if not key or any(ch.isspace() for ch in key):
        raise ValueError("Invalid image API key format")

    width, height = (
        source_dimensions(size, source_size)
        if aspect == "source"
        else NATIVE[size][aspect]
    )
    body: dict[str, Any] = {
        "model": settings.seedream_model,
        "prompt": prompt,
        "size": f"{width}x{height}",
        "response_format": "b64_json",
        "watermark": False,
        "stream": False,
    }
    if references:
        body["image"] = references[0] if len(references) == 1 else list(references)

    record: dict[str, Any] = {
        "state": "pending",
        "started": time.time(),
        "model": settings.seedream_model,
        "requested_size": body["size"],
        "reference_count": len(references),
        "reference_sha256": [
            hashlib.sha256(ref.encode("utf-8")).hexdigest() for ref in references
        ],
    }
    write_receipt(destination, record)

    uncertain_msg = (
        "Image generation status is uncertain after request dispatch "
        "(the request may have been billed); no automatic retry was performed. "
        "Please check existing images before retrying."
    )

    url = settings.ark_base_url.rstrip("/") + "/images/generations"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(float(settings.seedream_timeout_seconds)),
            follow_redirects=False,
        ) as client:
            response = await client.post(url, json=body, headers=headers)
            raw_content = response.content
    except asyncio.CancelledError:
        _mark_uncertain(destination, record)
        raise
    except (httpx.TimeoutException, TimeoutError, httpx.HTTPError, OSError):
        _mark_uncertain(destination, record)
        raise GenerationUncertain(uncertain_msg) from None

    if response.status_code >= 500:
        _mark_uncertain(destination, record, http_status=response.status_code)
        raise GenerationUncertain(uncertain_msg)

    if response.status_code >= 400:
        record.update(
            state="rejected", http_status=response.status_code, finished=time.time()
        )
        try:
            write_receipt(destination, record)
        except OSError:
            pass
        status_messages = {
            400: "Image generation parameters or content were rejected",
            401: "Image service authentication failed",
            402: "Image service account balance is insufficient",
            403: "Image service access was denied",
            429: "Image service rate limit exceeded",
        }
        msg = status_messages.get(
            response.status_code, "Image service rejected the request"
        )
        raise RuntimeError(f"{msg}; no automatic retry was performed")

    if len(raw_content) > MAX_RESPONSE_BYTES:
        _mark_uncertain(destination, record)
        raise GenerationUncertain(uncertain_msg)

    try:
        parsed = json.loads(raw_content)
        rows = parsed["data"]
        if not isinstance(rows, list) or len(rows) != 1 or rows[0].get("error"):
            raise ValueError("Invalid response data array")
        image_bytes = base64.b64decode(rows[0]["b64_json"], validate=True)
        with Image.open(io.BytesIO(image_bytes)) as im:
            suffix = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}[im.format]
            actual_width, actual_height = im.size
            if actual_width * actual_height > MAX_OUTPUT_PIXELS:
                raise ValueError("Output image exceeds maximum pixel count")
            if any(
                k.casefold() in ("prompt", "workflow", "parameters", "description")
                for k in im.info
            ):
                raise ValueError("Output image contains disallowed metadata")
            im.verify()
    except (
        KeyError,
        TypeError,
        ValueError,
        OSError,
        binascii.Error,
        Image.DecompressionBombError,
    ):
        _mark_uncertain(destination, record)
        raise GenerationUncertain(uncertain_msg) from None

    if local_composite is not None:
        from .regions import composite_local

        crop_path = destination / f"crop{suffix}"
        try:
            temp_crop = destination / "crop.part"
            desc = os.open(temp_crop, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(desc, "wb") as stream:
                stream.write(image_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_crop, crop_path)

            original_path = destination / "original.png"
            comp = composite_local(
                local_composite["base_path"],
                crop_path,
                local_composite["selection"],
                local_composite["crop"],
                original_path,
            )
            final_bytes = original_path.read_bytes()
            record.update(
                state="saved",
                finished=time.time(),
                width=comp["width"],
                height=comp["height"],
                file=original_path.name,
                sha256=comp["sha256"],
                selected_pixels=comp["selected_pixels"],
                outside_changed_pixels=0,
            )
            write_receipt(destination, record)
        except (ValueError, RuntimeError) as exc:
            _mark_uncertain(destination, record)
            raise RuntimeError(str(exc)) from exc
        except OSError:
            _mark_uncertain(destination, record)
            raise GenerationUncertain(uncertain_msg) from None

        return {
            "path": original_path,
            "raw_bytes": final_bytes,
            "width": comp["width"],
            "height": comp["height"],
            "sha256": comp["sha256"],
            "selected_pixels": comp["selected_pixels"],
            "outside_changed_pixels": 0,
            "requested_size": body["size"],
            "reference_count": len(references),
            "watermark": False,
        }

    original_path = destination / f"original{suffix}"
    try:
        temporary = destination / "original.part"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(image_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, original_path)
        record.update(
            state="saved",
            finished=time.time(),
            width=actual_width,
            height=actual_height,
            file=original_path.name,
            sha256=hashlib.sha256(image_bytes).hexdigest(),
        )
        write_receipt(destination, record)
    except OSError:
        _mark_uncertain(destination, record)
        raise GenerationUncertain(uncertain_msg) from None

    return {
        "path": original_path,
        "raw_bytes": image_bytes,
        "width": actual_width,
        "height": actual_height,
        "requested_size": body["size"],
        "reference_count": len(references),
        "watermark": False,
    }
