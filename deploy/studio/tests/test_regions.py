import asyncio
import base64
import hashlib
import io
import json
import os
import random
import re
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
from PIL import Image, ImageDraw

from deploy.studio.app import create_app
from deploy.studio.config import StudioSettings, load_rules, load_settings
from deploy.studio.images import GEMINI_NATIVE
from deploy.studio.regions import (
    DEFAULT_SAM2_MODEL_DIR,
    MODEL_REPO,
    MODEL_REVISION,
    MODEL_SHA256,
    Segmenter,
    build_selection_editor_html,
    composite_local,
    count_outside_changes,
    is_editor_token,
    is_image_token,
    load_confirmed_selection,
    local_context,
    mask_for,
    object_placement,
    open_image,
    save_confirmed_selection,
    save_exact,
    selection_proof,
    selection_value,
    sign_editor_token,
    sign_image_token,
    source_dimensions,
    verify_editor_token,
    verify_onnx_model_dir,
    verify_sam2_model_dir,
)
from deploy.studio.store import resolve_image

# 测试固定令牌、签名密钥与隔离用户编号
VALID_TOKEN = "a" * 64
VALID_SIGNING_KEY = "f" * 64
USER_A = "65f00000000000000000000a"
USER_B = "65f00000000000000000000b"
FIXTURE_KB_TEMPLATE = Path(__file__).resolve().parent / "fixtures" / "kb"
VENDOR_PATTERN = re.compile(
    r"gemini|argon|claude|haiku|sonnet|opus|openrouter|seedream|seed3d|volcengine|google|anthropic|deepseek|doubao|\bark\b|openai|gpt|豆包|火山|字节",
    re.IGNORECASE,
)


# 构建带记录与样图的测试知识库目录
def _populate_fixture_kb(kb_dir: Path) -> Path:
    records_dir = kb_dir / "records"
    catalog_dir = kb_dir / "catalog"
    ref_dir = kb_dir / "assets" / "ref2048"
    thumb_dir = kb_dir / "assets" / "thumb512"
    shutil.copytree(FIXTURE_KB_TEMPLATE / "records", records_dir)
    shutil.copytree(FIXTURE_KB_TEMPLATE / "catalog", catalog_dir)
    ref_dir.mkdir(parents=True, mode=0o700)
    thumb_dir.mkdir(parents=True, mode=0o700)
    for idx, yaml_file in enumerate(sorted(records_dir.glob("*.yaml"))):
        asset_id = yaml_file.stem
        color = ((40 + idx * 20) % 255, (100 + idx * 15) % 255, (160 - idx * 10) % 255)
        img = Image.new("RGB", (160, 120), color)
        buf_jpg = io.BytesIO()
        img.save(buf_jpg, format="JPEG")
        (ref_dir / f"{asset_id}.jpg").write_bytes(buf_jpg.getvalue())
        thumb = Image.new("RGB", (64, 48), color)
        buf_webp = io.BytesIO()
        thumb.save(buf_webp, format="WEBP")
        (thumb_dir / f"{asset_id}.webp").write_bytes(buf_webp.getvalue())
    return kb_dir


# 构建测试用StudioSettings实例
@pytest.fixture
def studio_env(tmp_path: Path) -> StudioSettings:
    lc_dir = tmp_path / "librechat_images"
    data_dir = tmp_path / "data"
    kb_dir = _populate_fixture_kb(tmp_path / "kb")
    lc_dir.mkdir(parents=True, mode=0o700)
    data_dir.mkdir(parents=True, mode=0o700)
    secret_file = tmp_path / "studio.json"
    secret_data = {
        "shared_token": VALID_TOKEN,
        "file_signing_key": VALID_SIGNING_KEY,
        "public_base_url": "https://dev.xuqian.io",
        "gemini_api_key": "test-gemini-secret-key",
        "ark_api_key": "test-ark-secret-key",
        "librechat_images_dir": str(lc_dir),
        "data_dir": str(data_dir),
        "kb_root": str(kb_dir),
    }
    secret_file.write_text(json.dumps(secret_data), encoding="utf-8")
    secret_file.chmod(0o600)
    return load_settings(secret_file)


# 校验矩形、多边形、画笔涂抹选区的布尔加减光栅化及非法选区拒绝逻辑
def test_mask_rasterization_of_shapes_and_validation() -> None:
    selection = {
        "version": 1,
        "shapes": [
            {
                "type": "rect",
                "op": "add",
                "points": [{"x": 100, "y": 100}, {"x": 500, "y": 500}],
            },
            {
                "type": "polygon",
                "op": "add",
                "points": [
                    {"x": 600, "y": 150},
                    {"x": 850, "y": 150},
                    {"x": 750, "y": 450},
                ],
            },
            {
                "type": "stroke",
                "op": "add",
                "radius": 35,
                "points": [{"x": 200, "y": 750}, {"x": 700, "y": 750}],
            },
            {
                "type": "rect",
                "op": "subtract",
                "points": [{"x": 200, "y": 200}, {"x": 350, "y": 350}],
            },
        ],
    }
    normalized = selection_value(selection)
    assert len(normalized["shapes"]) == 4

    mask = mask_for(normalized, (200, 200))
    assert mask.mode == "L"
    assert mask.size == (200, 200)
    # 矩形加选区边缘内部应为255，而被subtract挖空的中心点应为0
    assert mask.getpixel((30, 30)) == 255
    assert mask.getpixel((55, 55)) == 0
    # 画笔涂抹轨迹中心应为255
    assert mask.getpixel((90, 150)) == 255
    # 远离所有形状的左上角像素应为0
    assert mask.getpixel((2, 2)) == 0

    # 校验仅有subtract无add形状、越界坐标、布尔坐标、过小区域与过多顶点均被拒绝
    with pytest.raises(ValueError, match="additive"):
        selection_value(
            {
                "version": 1,
                "shapes": [
                    {
                        "type": "rect",
                        "op": "subtract",
                        "points": [{"x": 10, "y": 10}, {"x": 200, "y": 200}],
                    }
                ],
            }
        )
    with pytest.raises(ValueError, match="out of range"):
        selection_value(
            {
                "version": 1,
                "shapes": [
                    {
                        "type": "rect",
                        "op": "add",
                        "points": [{"x": -1, "y": 10}, {"x": 200, "y": 200}],
                    }
                ],
            }
        )
    with pytest.raises(ValueError, match="out of range"):
        selection_value(
            {
                "version": 1,
                "shapes": [
                    {
                        "type": "rect",
                        "op": "add",
                        "points": [{"x": True, "y": 10}, {"x": 200, "y": 200}],
                    }
                ],
            }
        )
    with pytest.raises(ValueError, match="too small"):
        mask_for(
            {
                "version": 1,
                "shapes": [
                    {
                        "type": "rect",
                        "op": "add",
                        "points": [{"x": 10, "y": 10}, {"x": 11, "y": 11}],
                    }
                ],
            },
            (64, 64),
        )


# 属性测试：在随机底图与随机多形状布尔掩码上验证composite_local合回后选区外像素100%字节级零改动
def test_outside_selection_pixels_identical_after_composite_property(
    tmp_path: Path,
) -> None:
    rng = random.Random(20261016)
    for trial in range(25):
        w = rng.randint(96, 320)
        h = rng.randint(96, 320)
        base_arr = np.frombuffer(rng.randbytes(w * h * 3), dtype=np.uint8).reshape(
            (h, w, 3)
        )
        base_img = Image.fromarray(base_arr, mode="RGB")
        base_path = tmp_path / f"base_{trial}.png"
        save_exact(base_img, base_path)

        shapes: list[dict[str, Any]] = []
        num_add = rng.randint(1, 3)
        for _ in range(num_add):
            kind = rng.choice(["rect", "polygon", "stroke"])
            if kind == "rect":
                x1 = rng.randint(50, 650)
                y1 = rng.randint(50, 650)
                x2 = min(980, x1 + rng.randint(120, 320))
                y2 = min(980, y1 + rng.randint(120, 320))
                shapes.append(
                    {
                        "type": "rect",
                        "op": "add",
                        "points": [{"x": x1, "y": y1}, {"x": x2, "y": y2}],
                    }
                )
            elif kind == "polygon":
                cx = rng.randint(200, 800)
                cy = rng.randint(200, 800)
                pts = [
                    {
                        "x": max(0, min(999, cx + rng.randint(-180, 180))),
                        "y": max(0, min(999, cy + rng.randint(-180, 180))),
                    }
                    for _ in range(rng.randint(3, 7))
                ]
                shapes.append({"type": "polygon", "op": "add", "points": pts})
            else:
                pts = [
                    {"x": rng.randint(80, 920), "y": rng.randint(80, 920)}
                    for _ in range(rng.randint(2, 6))
                ]
                shapes.append(
                    {
                        "type": "stroke",
                        "op": "add",
                        "radius": rng.randint(25, 80),
                        "points": pts,
                    }
                )

        if rng.random() < 0.5:
            sx1 = rng.randint(100, 400)
            sy1 = rng.randint(100, 400)
            shapes.append(
                {
                    "type": "rect",
                    "op": "subtract",
                    "points": [
                        {"x": sx1, "y": sy1},
                        {"x": sx1 + 90, "y": sy1 + 90},
                    ],
                }
            )

        selection = {"version": 1, "shapes": shapes}
        try:
            mask = mask_for(selection, (w, h))
        except ValueError:
            continue

        ctx = local_context(base_path, selection)
        crop_w, crop_h = ctx["size"]
        gen_arr = np.frombuffer(
            rng.randbytes(crop_w * crop_h * 3), dtype=np.uint8
        ).reshape((crop_h, crop_w, 3))
        gen_img = Image.fromarray(gen_arr, mode="RGB")
        gen_path = tmp_path / f"gen_{trial}.png"
        save_exact(gen_img, gen_path)

        dest_path = tmp_path / f"out_{trial}.png"
        comp = composite_local(base_path, gen_path, selection, ctx["crop"], dest_path)
        assert comp["width"] == w
        assert comp["height"] == h
        assert comp["outside_changed_pixels"] == 0

        reloaded = open_image(dest_path)
        assert reloaded.size == (w, h)
        assert count_outside_changes(base_img, reloaded, mask) == 0

        # 使用numpy逐字节核对mask==0处所有通道完全一致
        outside = np.asarray(mask) == 0
        assert np.array_equal(
            np.asarray(base_img)[outside],
            np.asarray(reloaded)[outside],
        )

    # 验证若生成图比例与裁切框比例偏差超过2.5%，composite_local拒绝变形回贴
    bad_gen = Image.new("RGB", (300, 100), (255, 0, 0))
    bad_gen_path = tmp_path / "bad_ratio.png"
    save_exact(bad_gen, bad_gen_path)
    base_sq = tmp_path / "base_sq.png"
    save_exact(Image.new("RGB", (200, 200), (20, 40, 60)), base_sq)
    sel_sq = {
        "version": 1,
        "shapes": [
            {
                "type": "rect",
                "op": "add",
                "points": [{"x": 200, "y": 200}, {"x": 800, "y": 800}],
            }
        ],
    }
    with pytest.raises(ValueError, match="aspect ratio"):
        composite_local(
            base_sq,
            bad_gen_path,
            sel_sq,
            (0, 0, 200, 200),
            tmp_path / "should_not_exist.png",
        )


# 校验含EXIF旋转方向（Orientation=6顺时针90度）底图的裁切、凭据计算、回贴几何及原样贴入
def test_crop_and_composite_geometry_including_exif_rotated_bases(
    tmp_path: Path,
) -> None:
    # 原始存储尺寸为320x200，设置EXIF Orientation=6（顺时针旋转90度后视觉尺寸为200x320）
    raw_img = Image.new("RGB", (320, 200), (30, 60, 90))
    draw = ImageDraw.Draw(raw_img)
    draw.rectangle((0, 0, 159, 99), fill=(220, 40, 40))
    exif = raw_img.getexif()
    exif[0x0112] = 6
    exif_base_path = tmp_path / "exif_rotated.jpg"
    raw_img.save(exif_base_path, format="JPEG", exif=exif.tobytes(), quality=95)

    oriented = open_image(exif_base_path)
    assert oriented.size == (200, 320)

    selection = {
        "version": 1,
        "shapes": [
            {
                "type": "rect",
                "op": "add",
                "points": [{"x": 200, "y": 250}, {"x": 750, "y": 700}],
            }
        ],
    }
    proof = selection_proof(exif_base_path, selection)
    assert proof["width"] == 200
    assert proof["height"] == 320
    assert proof["selected_pixels"] > 100

    ctx = local_context(exif_base_path, selection)
    assert ctx["base_size"] == (200, 320)
    crop_w, crop_h = ctx["size"]
    req_w, req_h = source_dimensions("1K", (crop_w, crop_h))
    assert req_w % 16 == 0 and req_h % 16 == 0

    # 模拟按1K目标尺寸生成的局部重绘结果并合回EXIF旋转底图
    gen_crop = Image.new("RGB", (req_w, req_h), (10, 210, 90))
    gen_crop_path = tmp_path / "gen_exif_crop.png"
    save_exact(gen_crop, gen_crop_path)

    dest_path = tmp_path / "exif_composited.png"
    comp = composite_local(
        exif_base_path, gen_crop_path, selection, ctx["crop"], dest_path
    )
    assert comp["width"] == 200
    assert comp["height"] == 320
    assert comp["outside_changed_pixels"] == 0

    mask = mask_for(selection, (200, 320))
    saved = open_image(dest_path)
    assert saved.size == (200, 320)
    assert count_outside_changes(oriented, saved, mask) == 0
    # 选区中心点像素应已更新为生成的绿色调
    center_px = saved.getpixel((95, 150))
    assert center_px[1] > 180

    # 验证object_placement（原样贴入）功能
    ref_rgba = Image.new("RGBA", (80, 80), (0, 0, 0, 0))
    ref_draw = ImageDraw.Draw(ref_rgba)
    ref_draw.ellipse((10, 10, 70, 70), fill=(200, 160, 80, 255))
    ref_path = tmp_path / "statue_ref.png"
    ref_rgba.save(ref_path, format="PNG")

    placed = object_placement(exif_base_path, selection, ref_path)
    assert placed["data_uri"].startswith("data:image/png;base64,")
    assert placed["crop"] == ctx["crop"]
    assert "原样放入选区" in placed["instruction"]


# 校验选区proof不匹配拒绝（跨底图、底图字节篡改、掩码篡改）以及跨用户选区隔离
def test_selection_proof_mismatch_rejection_and_per_user_isolation(
    studio_env: StudioSettings,
) -> None:
    user_a_lc = studio_env.librechat_images_dir / USER_A
    user_b_lc = studio_env.librechat_images_dir / USER_B
    user_a_lc.mkdir(parents=True, mode=0o700)
    user_b_lc.mkdir(parents=True, mode=0o700)

    uid_a1 = "11111111-2222-3333-4444-555555555555"
    uid_a2 = "22222222-3333-4444-5555-666666666666"
    path_a1 = user_a_lc / f"{uid_a1}__hall_a1.png"
    path_a2 = user_a_lc / f"{uid_a2}__hall_a2.png"
    save_exact(Image.new("RGB", (240, 180), (120, 110, 100)), path_a1)
    save_exact(Image.new("RGB", (240, 180), (60, 70, 80)), path_a2)

    selection = {
        "version": 1,
        "shapes": [
            {
                "type": "rect",
                "op": "add",
                "points": [{"x": 150, "y": 150}, {"x": 700, "y": 700}],
            }
        ],
    }
    rec_a = save_confirmed_selection(studio_env, USER_A, uid_a1, path_a1, selection)
    sel_id = rec_a["selection_id"]
    assert sel_id.startswith("sel_")

    # 1. User B 无法读取或使用 User A 的 selection_id
    with pytest.raises(ValueError, match="not found for current user"):
        load_confirmed_selection(
            studio_env, USER_B, sel_id, base_image_id=uid_a1, base_path=path_a1
        )

    # 2. User A 将该选区用于另一张底图 uid_a2 时，触发 proof mismatch 拒绝
    with pytest.raises(ValueError, match="Selection proof mismatch"):
        load_confirmed_selection(
            studio_env, USER_A, sel_id, base_image_id=uid_a2, base_path=path_a2
        )

    # 3. 底图文件字节在确认选区后发生改变（即使尺寸相同），触发 image_sha256 proof mismatch 拒绝
    tampered_path = user_a_lc / f"{uid_a1}__hall_a1.png"
    save_exact(Image.new("RGB", (240, 180), (121, 110, 100)), tampered_path)
    with pytest.raises(ValueError, match="Selection proof mismatch on image_sha256"):
        load_confirmed_selection(
            studio_env,
            USER_A,
            sel_id,
            base_image_id=uid_a1,
            base_path=tampered_path,
        )


# 校验select_region与edit_region端到端MCP工具流、签名URL回调及UI资源无敏感信息泄露
def test_select_region_and_edit_region_mcp_end_to_end_and_signed_urls_only(
    studio_env: StudioSettings,
) -> None:
    user_a_lc = studio_env.librechat_images_dir / USER_A
    user_a_lc.mkdir(parents=True, mode=0o700)
    upload_id = "11111111-2222-3333-4444-555555555555"
    base_file = user_a_lc / f"{upload_id}__temple_wall.png"
    base_img = Image.new("RGB", (320, 240), (150, 130, 110))
    save_exact(base_img, base_file)

    captured_gemini_requests: list[dict[str, Any]] = []

    async def _mock_gemini_handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-nano-banana-2.1:generateContent"
        )
        assert request.headers["x-goog-api-key"] == studio_env.gemini_api_key
        body = json.loads(request.content.decode("utf-8"))
        captured_gemini_requests.append(body)
        img_cfg = body["generationConfig"]["imageConfig"]
        req_w, req_h = GEMINI_NATIVE[img_cfg["imageSize"]][img_cfg["aspectRatio"]]
        gen_img = Image.new("RGB", (req_w, req_h), (210, 195, 175))
        buf = io.BytesIO()
        gen_img.save(buf, format="PNG")
        b64_png = base64.b64encode(buf.getvalue()).decode("ascii")
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "inlineData": {
                                        "mimeType": "image/png",
                                        "data": b64_png,
                                    }
                                }
                            ]
                        }
                    }
                ]
            },
        )

    app = create_app(
        settings=studio_env,
        rules=load_rules(),
        gemini_transport=httpx.MockTransport(_mock_gemini_handler),
    )

    async def _run() -> None:
        headers_a = {
            "X-Studio-Token": VALID_TOKEN,
            "X-LibreChat-User-ID": USER_A,
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://studio:8000",
        ) as client:
            # 1. 调用 select_region 获取内联选区编辑器 MCP UI
            sel_res = await client.post(
                "/mcp",
                headers=headers_a,
                json={
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/call",
                    "params": {
                        "name": "select_region",
                        "arguments": {"image_id": upload_id},
                    },
                },
            )
            assert sel_res.status_code == 200
            sel_result = sel_res.json()["result"]
            assert sel_result.get("isError") is False
            content = sel_result["content"]
            assert len(content) == 2
            summary = json.loads(content[0]["text"])
            assert summary["image_id"] == upload_id
            assert summary["width"] == 320
            assert summary["height"] == 240
            assert "editor_url" not in summary
            assert summary["preview_url"].startswith(
                "https://dev.xuqian.io/studio-files/"
            )
            preview_token = summary["preview_url"].rsplit("/", 1)[-1]
            assert is_image_token(preview_token) is True
            assert is_editor_token(preview_token) is False

            ui_res = content[1]["resource"]
            assert ui_res["uri"] == f"ui://zyb_studio/selection/{upload_id}"
            assert ui_res["mimeType"] == "text/html"
            editor_html = ui_res["text"]

            # 验证TextContent中的preview_url不具备编辑器权限，而EmbeddedResource内嵌专用edt:令牌
            assert summary["preview_url"] not in editor_html
            url_match = re.search(
                r"https://dev\.xuqian\.io/studio-files/([A-Za-z0-9_.-]+)",
                editor_html,
            )
            assert url_match is not None
            token = url_match.group(1)
            assert is_editor_token(token) is True
            assert is_image_token(token) is False
            assert token not in content[0]["text"]
            assert "确认选区" in editor_html
            for secret_val in (
                VALID_TOKEN,
                VALID_SIGNING_KEY,
                studio_env.ark_api_key,
                studio_env.gemini_api_key,
                str(studio_env.data_dir),
                str(studio_env.librechat_images_dir),
            ):
                assert secret_val not in editor_html
            assert VENDOR_PATTERN.search(editor_html) is None

            # 2. 通过签名URL测试OPTIONS预检、GET底图读取与POST选区确认
            opt_res = await client.options(f"/files/{token}")
            assert opt_res.status_code == 204
            assert opt_res.headers.get("access-control-allow-origin") == "*"
            assert "POST" in opt_res.headers.get("access-control-allow-methods", "")

            get_res = await client.get(f"/files/{token}")
            assert get_res.status_code == 200
            assert get_res.headers.get("access-control-allow-origin") == "*"
            assert get_res.content == base_file.read_bytes()

            confirm_selection = {
                "version": 1,
                "shapes": [
                    {
                        "type": "rect",
                        "op": "add",
                        "points": [{"x": 200, "y": 200}, {"x": 750, "y": 750}],
                    }
                ],
            }
            post_res = await client.post(
                f"/files/{token}",
                json={"action": "confirm", "selection": confirm_selection},
            )
            assert post_res.status_code == 200
            assert post_res.headers.get("access-control-allow-origin") == "*"
            confirmed_data = post_res.json()
            selection_id = confirmed_data["selection_id"]
            assert selection_id.startswith("sel_")
            assert confirmed_data["image_id"] == upload_id
            assert confirmed_data["proof"]["width"] == 320
            assert confirmed_data["proof"]["height"] == 240

            # 3. 调用 edit_region 执行局部重绘并验证返回对比UI与输出图
            edit_res = await client.post(
                "/mcp",
                headers=headers_a,
                json={
                    "jsonrpc": "2.0",
                    "id": 11,
                    "method": "tools/call",
                    "params": {
                        "name": "edit_region",
                        "arguments": {
                            "base_id": upload_id,
                            "selection_id": selection_id,
                            "instruction": "将选区内的台面更换为白玉兰大理石纹理",
                            "size": "1K",
                        },
                    },
                },
            )
            assert edit_res.status_code == 200
            edit_result = edit_res.json()["result"]
            assert edit_result.get("isError") is False
            edit_blocks = edit_result["content"]
            assert len(edit_blocks) == 3

            edit_summary = json.loads(edit_blocks[0]["text"])
            assert edit_summary["base_id"] == upload_id
            assert edit_summary["selection_id"] == selection_id
            assert edit_summary["width"] == 320
            assert edit_summary["height"] == 240
            assert edit_summary["outside_changed_pixels"] == 0
            assert edit_summary["before_url"].startswith(
                "https://dev.xuqian.io/studio-files/"
            )
            assert edit_summary["after_url"].startswith(
                "https://dev.xuqian.io/studio-files/"
            )

            compare_ui = edit_blocks[1]["resource"]
            assert compare_ui["uri"] == f"ui://zyb_studio/compare/{edit_summary['id']}"
            assert compare_ui["mimeType"] == "text/html"
            compare_html = compare_ui["text"]
            assert "对比修改前" in compare_html
            assert "标出明显色差" in compare_html
            assert edit_summary["before_url"] in compare_html
            assert edit_summary["after_url"] in compare_html
            for secret_val in (
                VALID_TOKEN,
                VALID_SIGNING_KEY,
                studio_env.ark_api_key,
                studio_env.gemini_api_key,
                str(studio_env.data_dir),
                str(studio_env.librechat_images_dir),
            ):
                assert secret_val not in compare_html
            assert VENDOR_PATTERN.search(compare_html) is None

            assert edit_blocks[2]["type"] == "image"
            assert edit_blocks[2]["mimeType"] in ("image/jpeg", "image/webp")

            # 验证发往Gemini生图服务的提示词与多模态结构包含局部裁切<bbox>约束与底图裁切inlineData
            assert len(captured_gemini_requests) == 1
            sent_parts = captured_gemini_requests[0]["contents"][0]["parts"]
            sent_prompt = sent_parts[0]["text"]
            assert "<bbox>" in sent_prompt and "</bbox>" in sent_prompt
            assert "inlineData" in sent_parts[1]
            assert captured_gemini_requests[0]["generationConfig"][
                "responseModalities"
            ] == ["TEXT", "IMAGE"]

            # 验证落盘后的结果图在选区外与底图100%字节一致
            saved_resolved = resolve_image(studio_env, USER_A, edit_summary["id"])
            saved_img = open_image(saved_resolved.path)
            mask = mask_for(confirm_selection, base_img.size)
            assert count_outside_changes(base_img, saved_img, mask) == 0

            # 验证 resolve_image(@latest) 能识别 edit_region 产出的新图片
            latest_resolved = resolve_image(studio_env, USER_A, "@latest")
            assert latest_resolved.id == edit_summary["id"]

    asyncio.run(_run())


# 校验Segmenter（SAM 2.1 hiera-tiny PyTorch CPU + transformers Sam2Model）的SHA256校验、safetensors限制、懒加载、单图embedding缓存、串行推理与点选分割路由
def test_segmentation_adapter_mocked_and_endpoint(
    studio_env: StudioSettings, tmp_path: Path
) -> None:
    import torch

    assert MODEL_REPO == "facebook/sam2.1-hiera-tiny"
    assert MODEL_REVISION == "de431c4043854a71d8101e17995dfe596bf101a5"
    assert (
        MODEL_SHA256
        == "48c14467e5cf9e51870511feb72c89688e82dd74523142c0538b663e193ac2a7"
    )

    model_dir = tmp_path / "sam2_safetensors"
    model_dir.mkdir(parents=True, mode=0o700)
    weights_bytes = b"mock-sam2.1-hiera-tiny-safetensors-v1"
    cfg_bytes = b'{"model_type": "sam2"}'
    pre_bytes = b'{"do_resize": true}'
    proc_bytes = b'{"processor_class": "Sam2Processor"}'
    (model_dir / "model.safetensors").write_bytes(weights_bytes)
    (model_dir / "config.json").write_bytes(cfg_bytes)
    (model_dir / "preprocessor_config.json").write_bytes(pre_bytes)
    (model_dir / "processor_config.json").write_bytes(proc_bytes)
    manifest = {
        "source": MODEL_REPO,
        "revision": MODEL_REVISION,
        "files": {
            "model.safetensors": {"sha256": hashlib.sha256(weights_bytes).hexdigest()},
            "config.json": {"sha256": hashlib.sha256(cfg_bytes).hexdigest()},
            "preprocessor_config.json": {
                "sha256": hashlib.sha256(pre_bytes).hexdigest()
            },
            "processor_config.json": {"sha256": hashlib.sha256(proc_bytes).hexdigest()},
        },
    }
    (model_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    # 1. 验证严禁目录中存在任何非safetensors的pickle权重文件（.bin/.pt/.pth/.ckpt/.pkl）
    bad_pickle_weight = model_dir / "pytorch_model.bin"
    bad_pickle_weight.write_bytes(b"disallow-pickle")
    with pytest.raises(ValueError, match="Disallowed non-safetensors weight"):
        verify_sam2_model_dir(model_dir)
    bad_pickle_weight.unlink()

    stats = {"loaded": 0, "encoder_runs": 0, "decoder_runs": 0}

    class _MockOutputs:
        def __init__(
            self,
            pred_masks: torch.Tensor,
            iou_scores: torch.Tensor,
            image_embeddings: torch.Tensor,
        ) -> None:
            self.pred_masks = pred_masks
            self.iou_scores = iou_scores
            self.image_embeddings = image_embeddings

    class _MockProcessor:
        def __call__(
            self,
            *,
            images: Image.Image,
            input_points: Any | None = None,
            input_labels: Any | None = None,
            return_tensors: str = "pt",
        ) -> dict[str, Any]:
            assert return_tensors == "pt"
            w, h = images.size
            return {
                "pixel_values": torch.ones((1, 3, 64, 64), dtype=torch.float32),
                "original_sizes": torch.tensor([[h, w]], dtype=torch.int64),
                "input_points": torch.tensor(input_points, dtype=torch.float32),
                "input_labels": torch.tensor(input_labels, dtype=torch.int64),
            }

        def post_process_masks(
            self,
            pred_masks: torch.Tensor,
            original_sizes: torch.Tensor,
        ) -> list[torch.Tensor]:
            del pred_masks
            h = int(original_sizes[0, 0].item())
            w = int(original_sizes[0, 1].item())
            # 构造一个带中心镂空孔洞的环形掩码，以验证RETR_TREE同时提取外轮廓add与内孔subtract
            mask = torch.zeros((1, 3, h, w), dtype=torch.bool)
            mask[0, 1, h // 6 : h * 5 // 6, w // 6 : w * 5 // 6] = True
            mask[0, 1, h * 2 // 5 : h * 3 // 5, w * 2 // 5 : w * 3 // 5] = False
            return [mask]

    class _MockSam2Model:
        device = "cpu"

        def __call__(
            self,
            *,
            pixel_values: torch.Tensor | None = None,
            image_embeddings: torch.Tensor | None = None,
            input_points: torch.Tensor,
            input_labels: torch.Tensor,
            original_sizes: torch.Tensor | None = None,
            multimask_output: bool = True,
        ) -> _MockOutputs:
            assert multimask_output is True
            del input_points, input_labels, original_sizes
            if image_embeddings is None:
                assert pixel_values is not None
                stats["encoder_runs"] += 1
                emb = pixel_values[:, :1, :16, :16].clone()
            else:
                emb = image_embeddings
            stats["decoder_runs"] += 1
            pred = torch.zeros((1, 1, 3, 64, 64), dtype=torch.float32)
            scores = torch.tensor([[[0.42, 0.94, 0.71]]], dtype=torch.float32)
            return _MockOutputs(pred, scores, emb)

    def _mock_loader(verified_dir: Path) -> tuple[Any, Any]:
        assert verified_dir == model_dir
        stats["loaded"] += 1
        return _MockProcessor(), _MockSam2Model()

    segmenter = Segmenter(model_dir=model_dir, model_loader=_mock_loader)
    assert segmenter.ready() is True
    # 首次调用前不应提前加载模型（懒加载）
    assert stats["loaded"] == 0

    img1_path = tmp_path / "img1.png"
    img2_path = tmp_path / "img2.png"
    save_exact(Image.new("RGB", (200, 160), (100, 120, 140)), img1_path)
    save_exact(Image.new("RGB", (200, 160), (140, 120, 100)), img2_path)

    res1 = segmenter.segment(img1_path, [{"x": 250, "y": 250, "label": 1}])
    assert stats["loaded"] == 1
    assert stats["encoder_runs"] == 1
    assert stats["decoder_runs"] == 1
    assert res1["score"] == pytest.approx(0.94, rel=1e-2)
    assert len(res1["shapes"]) >= 2
    assert any(s["op"] == "add" for s in res1["shapes"])
    assert any(s["op"] == "subtract" for s in res1["shapes"])
    assert res1["selected_pixels"] > 100

    # 对同一张图再次发起点选分割，应复用单图embedding缓存（encoder_runs保持为1）
    res2 = segmenter.segment(
        img1_path,
        [{"x": 250, "y": 250, "label": 1}, {"x": 500, "y": 500, "label": 0}],
    )
    assert stats["loaded"] == 1
    assert stats["encoder_runs"] == 1
    assert stats["decoder_runs"] == 2
    assert res2["selected_pixels"] > 100

    # 切换到第二张图片时，重新编码并替换单图缓存（仅缓存1张图的embedding）
    segmenter.segment(img2_path, [{"x": 300, "y": 300, "label": 1}])
    assert stats["encoder_runs"] == 2
    assert stats["decoder_runs"] == 3
    assert segmenter._cache_key is not None
    assert segmenter._cache_key[0] == str(img2_path.resolve())

    # 验证通过 /files/<token> POST action=segment 端到端调用（含existing与operation参数）
    user_a_lc = studio_env.librechat_images_dir / USER_A
    user_a_lc.mkdir(parents=True, mode=0o700, exist_ok=True)
    upload_id = "33333333-4444-5555-6666-777777777777"
    shutil.copyfile(img1_path, user_a_lc / f"{upload_id}__seg.png")

    app = create_app(
        settings=studio_env,
        rules=load_rules(),
        segmenter=segmenter,
    )

    async def _run_http() -> None:
        img1_sha = hashlib.sha256(img1_path.read_bytes()).hexdigest()
        token, _ = sign_editor_token(studio_env, USER_A, upload_id, img1_sha)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://studio:8000",
        ) as client:
            seg_http = await client.post(
                f"/files/{token}",
                json={
                    "action": "segment",
                    "points": [{"x": 250, "y": 250, "label": 1}],
                    "operation": "add",
                    "existing": {
                        "version": 1,
                        "shapes": [
                            {
                                "type": "rect",
                                "op": "add",
                                "points": [
                                    {"x": 10, "y": 10},
                                    {"x": 80, "y": 80},
                                ],
                            }
                        ],
                    },
                },
            )
            assert seg_http.status_code == 200
            data = seg_http.json()
            assert all(s["type"] == "polygon" for s in data["shapes"])
            assert len(data["shapes"]) >= 2
            assert data["selected_pixels"] > 100

    asyncio.run(_run_http())

    # 验证若manifest中的SHA256与实际模型文件不匹配，verify_sam2_model_dir拒绝加载
    bad_manifest = {
        "source": "tampered",
        "files": {
            "model.safetensors": {"sha256": "0" * 64},
            "config.json": {"sha256": hashlib.sha256(cfg_bytes).hexdigest()},
        },
    }
    (model_dir / "manifest.json").write_text(
        json.dumps(manifest | bad_manifest), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        verify_onnx_model_dir(model_dir)


# 若本地已存在通过SHA256校验的真实facebook/sam2.1-hiera-tiny safetensors权重，则执行真实PyTorch CPU冒烟推理（绝不触发下载）
def test_real_sam2_hiera_tiny_smoke_if_available(tmp_path: Path) -> None:
    candidate_dirs = [
        Path(p)
        for p in (
            os.environ.get("STUDIO_SAM2_MODEL_DIR"),
            os.environ.get("STUDIO_SAM2_ONNX_DIR"),
            str(DEFAULT_SAM2_MODEL_DIR),
            "/home/wangtinghu/.local/share/Trash/files/zyb-multiimage-20260928.69hb9m/sam2.1-hiera-tiny",
        )
        if p
    ]
    verified_dir: Path | None = None
    for cand in candidate_dirs:
        if cand.is_dir():
            try:
                verify_sam2_model_dir(cand, expected_sha256=MODEL_SHA256)
                verified_dir = cand
                break
            except ValueError:
                continue

    if verified_dir is None:
        pytest.skip(
            "Verified local facebook/sam2.1-hiera-tiny safetensors weights not present; skipping real-model smoke test"
        )

    segmenter = Segmenter(model_dir=verified_dir)
    assert segmenter.ready() is True

    sample_path = tmp_path / "real_sam2_smoke.png"
    img = Image.new("RGB", (256, 256), (35, 38, 42))
    draw = ImageDraw.Draw(img)
    draw.rectangle((64, 64, 192, 192), fill=(235, 220, 190))
    save_exact(img, sample_path)

    out = segmenter.segment(sample_path, [{"x": 500, "y": 500, "label": 1}])
    assert out["version"] == 1
    assert len(out["shapes"]) >= 1
    assert out["score"] > 0.5
    assert out["selected_pixels"] >= 1000
    mask = mask_for({"version": 1, "shapes": out["shapes"]}, (256, 256))
    # 中心亮块像素被选中，四角背景像素未被选中
    assert mask.getpixel((128, 128)) == 255
    assert mask.getpixel((8, 8)) == 0


# 回归测试（Finding #1）：调色板P模式含tRNS透明像素的PNG底图经局部合回后，选区外RGBA四通道（含Alpha）100%一致
def test_palette_png_with_trns_preserves_alpha_outside_selection(
    tmp_path: Path,
) -> None:
    w, h = 160, 120
    pal_img = Image.new("P", (w, h), 0)
    palette: list[int] = [
        10,
        20,
        30,  # 索引0：完全透明背景
        180,
        140,
        90,  # 索引1：半透明边框
        60,
        130,
        190,  # 索引2：不透明主体
    ] + [0] * (768 - 9)
    pal_img.putpalette(palette)
    draw = ImageDraw.Draw(pal_img)
    draw.rectangle((16, 16, 144, 104), fill=1)
    draw.rectangle((40, 32, 120, 88), fill=2)
    pal_path = tmp_path / "palette_trns_base.png"
    pal_img.save(pal_path, format="PNG", transparency=bytes([0, 128, 255]))

    selection = {
        "version": 1,
        "shapes": [
            {
                "type": "rect",
                "op": "add",
                "points": [{"x": 350, "y": 350}, {"x": 650, "y": 650}],
            }
        ],
    }
    ctx = local_context(pal_path, selection)
    crop_w, crop_h = ctx["size"]
    gen_img = Image.new("RGBA", (crop_w, crop_h), (240, 200, 120, 255))
    gen_path = tmp_path / "palette_gen.png"
    save_exact(gen_img, gen_path)

    dest_path = tmp_path / "palette_out.png"
    comp = composite_local(
        pal_path,
        gen_path,
        selection,
        ctx["crop"],
        dest_path,
        expected_proof=selection_proof(pal_path, selection),
    )
    assert comp["outside_changed_pixels"] == 0

    mask = mask_for(selection, (w, h))
    outside = np.asarray(mask) == 0
    with Image.open(pal_path) as raw_base:
        base_rgba = np.asarray(raw_base.convert("RGBA"))
    with Image.open(dest_path) as raw_out:
        out_rgba = np.asarray(raw_out.convert("RGBA"))

    # 选区外包含透明（alpha=0）与半透明（alpha=128）像素，合回后RGBA全部通道必须逐像素完全相等
    assert int(np.sum(base_rgba[:, :, 3][outside] == 0)) > 100
    assert int(np.sum(base_rgba[:, :, 3][outside] == 128)) > 100
    assert np.array_equal(base_rgba[outside], out_rgba[outside])


# 回归测试（Finding #6）：约12:1极端宽高比（如8000x670与670x8000）在1K与2K档位下均满足2.5%合成比例校验
def test_source_dimensions_and_local_crop_extreme_12_to_1_aspect_at_1k(
    tmp_path: Path,
) -> None:
    for dims in ((8000, 670), (670, 8000), (3200, 200), (200, 3200)):
        for tier in ("1K", "2K"):
            req_w, req_h = source_dimensions(tier, dims)
            assert req_w % 16 == 0 and req_h % 16 == 0
            assert 921600 <= req_w * req_h <= 4624220
            crop_ratio = dims[0] / dims[1]
            gen_ratio = req_w / req_h
            assert abs(gen_ratio / crop_ratio - 1.0) <= 0.025

    # 端到端验证8000x670底图在整图回退裁切框与1K档位下完成prepare_local_crop与composite_local
    banner_path = tmp_path / "banner_8000x670.png"
    banner_img = Image.new("RGB", (8000, 670), (140, 120, 100))
    save_exact(banner_img, banner_path)
    selection = {
        "version": 1,
        "shapes": [
            {
                "type": "rect",
                "op": "add",
                "points": [{"x": 50, "y": 100}, {"x": 950, "y": 900}],
            }
        ],
    }
    ctx = local_context(banner_path, selection)
    req_w, req_h = source_dimensions("1K", ctx["size"])
    gen_banner = Image.new("RGB", (req_w, req_h), (200, 170, 90))
    gen_banner_path = tmp_path / "gen_banner_1k.png"
    save_exact(gen_banner, gen_banner_path)

    out_banner_path = tmp_path / "out_banner_1k.png"
    comp = composite_local(
        banner_path,
        gen_banner_path,
        selection,
        ctx["crop"],
        out_banner_path,
    )
    assert comp["width"] == 8000
    assert comp["height"] == 670
    assert comp["outside_changed_pixels"] == 0


# 回归测试（Finding #8）：原样贴入对象若越出L形凹多边形或带孔选区的非选中区域，必须在生图前抛出ValueError拒绝
def test_object_placement_rejects_concave_or_holed_selection_clipping(
    tmp_path: Path,
) -> None:
    base_path = tmp_path / "concave_base.png"
    save_exact(Image.new("RGB", (300, 300), (120, 120, 120)), base_path)

    # 构造不透明方形参考对象（贴入包围盒中心时会覆盖中心区域）
    ref_path = tmp_path / "solid_ plaque.png"
    save_exact(Image.new("RGBA", (120, 120), (210, 160, 60, 255)), ref_path)

    # 1. 带中心镂空孔洞的选区：外框[100,100]-[900,900]，中心[300,300]-[700,700]被subtract挖空
    holed_selection = {
        "version": 1,
        "shapes": [
            {
                "type": "rect",
                "op": "add",
                "points": [{"x": 100, "y": 100}, {"x": 900, "y": 900}],
            },
            {
                "type": "rect",
                "op": "subtract",
                "points": [{"x": 300, "y": 300}, {"x": 700, "y": 700}],
            },
        ],
    }
    with pytest.raises(
        ValueError,
        match="Original object placement extends outside the confirmed selection",
    ):
        object_placement(base_path, holed_selection, ref_path)

    # 2. L形凹多边形选区：右上象限[350,100]-[900,650]不在选区内，但包围盒仍为[100,100]-[900,900]
    l_shape_selection = {
        "version": 1,
        "shapes": [
            {
                "type": "polygon",
                "op": "add",
                "points": [
                    {"x": 100, "y": 100},
                    {"x": 300, "y": 100},
                    {"x": 300, "y": 700},
                    {"x": 900, "y": 700},
                    {"x": 900, "y": 900},
                    {"x": 100, "y": 900},
                ],
            }
        ],
    }
    with pytest.raises(
        ValueError,
        match="Original object placement extends outside the confirmed selection",
    ):
        object_placement(base_path, l_shape_selection, ref_path)


# 回归测试（Finding #2）：只读预览令牌（img:）与编辑器专用短效单次令牌（edt:）能力隔离，且confirm严禁重放覆盖@latest
def test_token_capability_separation_and_single_use_editor_confirm(
    studio_env: StudioSettings,
) -> None:
    user_a_lc = studio_env.librechat_images_dir / USER_A
    user_a_lc.mkdir(parents=True, mode=0o700, exist_ok=True)
    upload_id = "44444444-5555-6666-7777-888888888888"
    base_file = user_a_lc / f"{upload_id}__altar_cap.png"
    save_exact(Image.new("RGB", (240, 180), (140, 120, 100)), base_file)
    base_sha = hashlib.sha256(base_file.read_bytes()).hexdigest()

    # 1. 校验编辑器令牌TTL被强制限制在900秒以内并绑定user_id + image_id + image_sha256 + nonce
    edt_token, edt_exp = sign_editor_token(
        studio_env, USER_A, upload_id, base_sha, ttl_seconds=86400
    )
    claims = verify_editor_token(studio_env, edt_token)
    assert claims["user_id"] == USER_A
    assert claims["image_id"] == upload_id
    assert claims["image_sha256"] == base_sha
    assert edt_exp - int(os.path.getmtime(base_file)) <= 905

    app = create_app(settings=studio_env, rules=load_rules())

    async def _run() -> None:
        headers_a = {
            "X-Studio-Token": VALID_TOKEN,
            "X-LibreChat-User-ID": USER_A,
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://studio:8000",
        ) as client:
            sel_res = await client.post(
                "/mcp",
                headers=headers_a,
                json={
                    "jsonrpc": "2.0",
                    "id": 21,
                    "method": "tools/call",
                    "params": {
                        "name": "select_region",
                        "arguments": {"image_id": upload_id},
                    },
                },
            )
            assert sel_res.status_code == 200
            sel_content = sel_res.json()["result"]["content"]
            text_payload = json.loads(sel_content[0]["text"])
            editor_html = sel_content[1]["resource"]["text"]

            # 2. 工具文本中的preview_url只能携带只读img:令牌，绝不携带edt:写能力令牌
            preview_tok = text_payload["preview_url"].rsplit("/", 1)[-1]
            assert is_image_token(preview_tok) is True
            assert is_editor_token(preview_tok) is False

            # 用只读preview_url或before_url/after_url发起POST必须返回HTTP 403
            sel_first = {
                "version": 1,
                "shapes": [
                    {
                        "type": "rect",
                        "op": "add",
                        "points": [{"x": 100, "y": 100}, {"x": 500, "y": 500}],
                    }
                ],
            }
            ro_post = await client.post(
                f"/files/{preview_tok}",
                json={"action": "confirm", "selection": sel_first},
            )
            assert ro_post.status_code == 403

            before_tok, _ = sign_image_token(studio_env, USER_A, upload_id)
            before_post = await client.post(
                f"/files/{before_tok}",
                json={"action": "confirm", "selection": sel_first},
            )
            assert before_post.status_code == 403

            # 其他用户（USER_B）对 USER_A 的底图签发的 edt: 令牌在解析底图时因跨用户隔离返回404
            cross_user_edt, _ = sign_editor_token(
                studio_env, USER_B, upload_id, base_sha
            )
            cross_post = await client.post(
                f"/files/{cross_user_edt}",
                json={"action": "confirm", "selection": sel_first},
            )
            assert cross_post.status_code == 404

            # 3. 提取EmbeddedResource内嵌的edt:令牌，首次confirm成功（200）
            match = re.search(
                r"https://dev\.xuqian\.io/studio-files/([A-Za-z0-9_.-]+)",
                editor_html,
            )
            assert match is not None
            embedded_edt_tok = match.group(1)
            assert is_editor_token(embedded_edt_tok) is True
            assert embedded_edt_tok not in sel_content[0]["text"]

            first_confirm = await client.post(
                f"/files/{embedded_edt_tok}",
                json={"action": "confirm", "selection": sel_first},
            )
            assert first_confirm.status_code == 200
            first_sel_id = first_confirm.json()["selection_id"]

            # 4. 使用同一个edt:令牌重放第二次confirm必须返回409，且绝不覆盖@latest
            sel_second = {
                "version": 1,
                "shapes": [
                    {
                        "type": "rect",
                        "op": "add",
                        "points": [{"x": 550, "y": 550}, {"x": 900, "y": 900}],
                    }
                ],
            }
            replay_confirm = await client.post(
                f"/files/{embedded_edt_tok}",
                json={"action": "confirm", "selection": sel_second},
            )
            assert replay_confirm.status_code == 409

            latest_rec = load_confirmed_selection(
                studio_env,
                USER_A,
                "@latest",
                base_image_id=upload_id,
                base_path=base_file,
            )
            assert latest_rec["selection_id"] == first_sel_id
            assert latest_rec["selection"] == selection_value(sel_first)

            # 5. 若底图文件在签发edt:令牌后被改动（SHA256变化），旧edt:令牌被409拒绝
            fresh_edt_tok, _ = sign_editor_token(
                studio_env, USER_A, upload_id, base_sha
            )
            save_exact(Image.new("RGB", (240, 180), (141, 120, 100)), base_file)
            stale_sha_res = await client.post(
                f"/files/{fresh_edt_tok}",
                json={"action": "confirm", "selection": sel_second},
            )
            assert stale_sha_res.status_code == 409

    asyncio.run(_run())


# 回归测试（Finding #5）：/files/<token> POST流式请求体超过512KiB时立即中止流读取并返回413，不完整缓冲大请求体
def test_post_files_stream_aborts_immediately_over_512kib(
    studio_env: StudioSettings,
) -> None:
    user_a_lc = studio_env.librechat_images_dir / USER_A
    user_a_lc.mkdir(parents=True, mode=0o700, exist_ok=True)
    upload_id = "55555555-6666-7777-8888-999999999999"
    base_file = user_a_lc / f"{upload_id}__stream_limit.png"
    save_exact(Image.new("RGB", (160, 120), (100, 110, 120)), base_file)
    base_sha = hashlib.sha256(base_file.read_bytes()).hexdigest()
    edt_token, _ = sign_editor_token(studio_env, USER_A, upload_id, base_sha)

    app = create_app(settings=studio_env, rules=load_rules())

    async def _run() -> None:
        chunks_emitted = 0

        async def _oversized_chunk_stream() -> AsyncIterator[bytes]:
            nonlocal chunks_emitted
            # 试图发送32个64KiB分块（共2MiB），服务端应在读到第9块（576KiB > 512KiB）时立即中断
            for _ in range(32):
                chunks_emitted += 1
                yield b"a" * (64 * 1024)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://studio:8000",
        ) as client:
            res_stream = await client.post(
                f"/files/{edt_token}",
                content=_oversized_chunk_stream(),
                headers={"Content-Type": "application/json"},
            )
            assert res_stream.status_code == 413
            assert chunks_emitted == 9

            # 若Content-Length本身超过512KiB，甚至不读取任何分块即直接返回413
            cl_chunks = 0

            async def _counted_stream() -> AsyncIterator[bytes]:
                nonlocal cl_chunks
                cl_chunks += 1
                yield b"{}"

            res_cl = await client.post(
                f"/files/{edt_token}",
                content=_counted_stream(),
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(600 * 1024),
                },
            )
            assert res_cl.status_code == 413
            assert cl_chunks == 0

    asyncio.run(_run())


# 回归测试（Finding #4）：若底图在供应商生图期间被篡改，composite_local必须在落盘original.png前因proof mismatch中止
def test_edit_region_aborts_if_base_image_mutated_during_generation(
    studio_env: StudioSettings,
) -> None:
    user_a_lc = studio_env.librechat_images_dir / USER_A
    user_a_lc.mkdir(parents=True, mode=0o700, exist_ok=True)
    upload_id = "66666666-7777-8888-9999-aaaaaaaaaaaa"
    base_file = user_a_lc / f"{upload_id}__mutated_midflight.png"
    save_exact(Image.new("RGB", (320, 240), (140, 120, 100)), base_file)

    selection = {
        "version": 1,
        "shapes": [
            {
                "type": "rect",
                "op": "add",
                "points": [{"x": 200, "y": 200}, {"x": 800, "y": 800}],
            }
        ],
    }
    rec = save_confirmed_selection(studio_env, USER_A, upload_id, base_file, selection)
    sel_id = rec["selection_id"]

    async def _mutating_gemini_handler(request: httpx.Request) -> httpx.Response:
        # 模拟并发请求在供应商响应返回前修改了底图文件字节
        save_exact(Image.new("RGB", (320, 240), (141, 120, 100)), base_file)
        body = json.loads(request.content.decode("utf-8"))
        img_cfg = body["generationConfig"]["imageConfig"]
        req_w, req_h = GEMINI_NATIVE[img_cfg["imageSize"]][img_cfg["aspectRatio"]]
        gen_img = Image.new("RGB", (req_w, req_h), (210, 190, 160))
        buf = io.BytesIO()
        gen_img.save(buf, format="PNG")
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "inlineData": {
                                        "mimeType": "image/png",
                                        "data": base64.b64encode(buf.getvalue()).decode(
                                            "ascii"
                                        ),
                                    }
                                }
                            ]
                        }
                    }
                ]
            },
        )

    app = create_app(
        settings=studio_env,
        rules=load_rules(),
        gemini_transport=httpx.MockTransport(_mutating_gemini_handler),
    )

    async def _run() -> None:
        headers_a = {
            "X-Studio-Token": VALID_TOKEN,
            "X-LibreChat-User-ID": USER_A,
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://studio:8000",
        ) as client:
            edit_res = await client.post(
                "/mcp",
                headers=headers_a,
                json={
                    "jsonrpc": "2.0",
                    "id": 31,
                    "method": "tools/call",
                    "params": {
                        "name": "edit_region",
                        "arguments": {
                            "base_id": upload_id,
                            "selection_id": sel_id,
                            "instruction": "替换为深色花岗岩",
                            "size": "1K",
                        },
                    },
                },
            )
            assert edit_res.status_code == 200
            result = edit_res.json()["result"]
            assert result["isError"] is True
            err_data = json.loads(result["content"][0]["text"])
            assert "Selection proof mismatch on image_sha256" in err_data["error"]
            job_id = err_data["job_id"]
            job_dir = studio_env.data_dir / "users" / USER_A / "jobs" / job_id
            assert not (job_dir / "original.png").exists()

    asyncio.run(_run())


# 回归测试（Finding #3）：供应商返回后若本地回贴失败，必须告警计费、暴露recoverable job_id，且finish_region_edit无需二次计费即可恢复合成
def test_edit_region_post_provider_failure_persists_recoverable_crop_and_finishes_without_rebilling(
    studio_env: StudioSettings,
) -> None:
    user_a_lc = studio_env.librechat_images_dir / USER_A
    user_a_lc.mkdir(parents=True, mode=0o700, exist_ok=True)
    upload_id = "77777777-8888-9999-aaaa-bbbbbbbbbbbb"
    base_file = user_a_lc / f"{upload_id}__recoverable_base.png"
    original_base_img = Image.new("RGB", (320, 240), (130, 115, 95))
    save_exact(original_base_img, base_file)
    original_base_bytes = base_file.read_bytes()

    selection = {
        "version": 1,
        "shapes": [
            {
                "type": "rect",
                "op": "add",
                "points": [{"x": 250, "y": 250}, {"x": 750, "y": 750}],
            }
        ],
    }
    rec = save_confirmed_selection(studio_env, USER_A, upload_id, base_file, selection)
    sel_id = rec["selection_id"]

    provider_calls = 0

    async def _gemini_handler_then_transient_base_fault(
        request: httpx.Request,
    ) -> httpx.Response:
        nonlocal provider_calls
        provider_calls += 1
        # 模拟供应商已成功生成并计费，但此时底图短暂被占用/替换导致回贴阶段失败
        save_exact(Image.new("RGB", (320, 240), (99, 99, 99)), base_file)
        body = json.loads(request.content.decode("utf-8"))
        img_cfg = body["generationConfig"]["imageConfig"]
        req_w, req_h = GEMINI_NATIVE[img_cfg["imageSize"]][img_cfg["aspectRatio"]]
        gen_img = Image.new("RGB", (req_w, req_h), (215, 185, 125))
        buf = io.BytesIO()
        gen_img.save(buf, format="PNG")
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "inlineData": {
                                        "mimeType": "image/png",
                                        "data": base64.b64encode(buf.getvalue()).decode(
                                            "ascii"
                                        ),
                                    }
                                }
                            ]
                        }
                    }
                ]
            },
        )

    app = create_app(
        settings=studio_env,
        rules=load_rules(),
        gemini_transport=httpx.MockTransport(_gemini_handler_then_transient_base_fault),
    )

    async def _run() -> None:
        headers_a = {
            "X-Studio-Token": VALID_TOKEN,
            "X-LibreChat-User-ID": USER_A,
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://studio:8000",
        ) as client:
            # 1. 首次调用 edit_region：供应商调用成功（provider_calls == 1），回贴阶段报错
            fail_res = await client.post(
                "/mcp",
                headers=headers_a,
                json={
                    "jsonrpc": "2.0",
                    "id": 41,
                    "method": "tools/call",
                    "params": {
                        "name": "edit_region",
                        "arguments": {
                            "base_id": upload_id,
                            "selection_id": sel_id,
                            "instruction": "将中心区域替换为敦煌金浮雕",
                            "size": "1K",
                        },
                    },
                },
            )
            assert fail_res.status_code == 200
            fail_result = fail_res.json()["result"]
            assert fail_result["isError"] is True
            err_payload = json.loads(fail_result["content"][0]["text"])
            assert provider_calls == 1
            assert err_payload["state"] == "recoverable"
            assert err_payload["recoverable"] is True
            assert "may have been billed" in err_payload["error"]
            assert "edit_region" in err_payload["error"]
            assert "finish_region_edit" not in err_payload["error"]
            job_id = err_payload["job_id"]
            assert job_id.startswith("img_")

            # 校验 tools/list 公开的工具面包含 edit_region 且其 schema 声明了 job_id
            tools_res = await client.post(
                "/mcp",
                headers=headers_a,
                json={"jsonrpc": "2.0", "id": 420, "method": "tools/list", "params": {}},
            )
            assert tools_res.status_code == 200
            discovered_tools = {
                t["name"]: t for t in tools_res.json()["result"]["tools"]
            }
            assert "edit_region" in discovered_tools
            assert "job_id" in discovered_tools["edit_region"]["inputSchema"]["properties"]

            job_dir = studio_env.data_dir / "users" / USER_A / "jobs" / job_id
            assert (job_dir / "crop.png").is_file()
            assert (job_dir / "local_recovery.json").is_file()
            assert not (job_dir / "original.png").exists()

            # 2. 校验 receipt.json 记录了 recoverable 状态，且未完成合成的任务不会被 @latest 误选为已完成图片
            receipt_data = json.loads(
                (job_dir / "receipt.json").read_text(encoding="utf-8")
            )
            assert receipt_data["state"] == "recoverable"
            assert resolve_image(studio_env, USER_A, "@latest").id == upload_id

            # 3. 恢复原底图字节后，调用公开工具 edit_region(job_id=...) 直接用已保存的 crop.png 完成回贴，不触发第二次供应商请求
            base_file.write_bytes(original_base_bytes)
            recover_res = await client.post(
                "/mcp",
                headers=headers_a,
                json={
                    "jsonrpc": "2.0",
                    "id": 43,
                    "method": "tools/call",
                    "params": {
                        "name": "edit_region",
                        "arguments": {"job_id": job_id},
                    },
                },
            )
            assert recover_res.status_code == 200
            rec_result = recover_res.json()["result"]
            assert rec_result.get("isError") is False
            assert provider_calls == 1

            rec_summary = json.loads(rec_result["content"][0]["text"])
            assert rec_summary["id"] == job_id
            assert rec_summary["recovered"] is True
            assert rec_summary["outside_changed_pixels"] == 0
            assert (job_dir / "original.png").is_file()

            # 校验选区外像素与原底图逐字节一致
            mask = mask_for(selection, (320, 240))
            saved_img = open_image(job_dir / "original.png")
            assert count_outside_changes(original_base_img, saved_img, mask) == 0
            assert saved_img.getpixel((160, 120)) == (215, 185, 125)

    asyncio.run(_run())


# 回归测试（Finding #7）：选区编辑器中subtract区域在Canvas上的Alpha必须擦除至0，且当subtract完全擦空选区时confirmBtn必须禁用
def test_selection_editor_subtract_renders_zero_alpha_and_disables_confirm_on_full_erase(
    tmp_path: Path,
) -> None:
    from playwright.sync_api import sync_playwright

    base_img = Image.new("RGB", (300, 200), (150, 130, 110))
    buf = io.BytesIO()
    base_img.save(buf, format="PNG")
    data_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(
        "ascii"
    )
    editor_html = build_selection_editor_html(
        image_id="img_subtract_test",
        signed_url=data_uri,
        expires_at="2026-12-31T00:00:00Z",
        width=300,
        height=200,
        segment_ready=False,
    )
    host_html = (
        "<!DOCTYPE html><html><body>"
        '<iframe id="ed" sandbox="allow-scripts" style="width:760px;height:640px;border:0"></iframe>'
        "</body></html>"
    )
    del tmp_path

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path="/usr/bin/chromium",
            headless=True,
        )
        try:
            page = browser.new_page(viewport={"width": 840, "height": 720})
            page.set_content(host_html)
            page.eval_on_selector(
                "#ed",
                "(el, html) => { el.srcdoc = html; }",
                editor_html,
            )
            frame = page.frame_locator("#ed")
            frame.locator("#confirmBtn").wait_for(state="visible")
            page.wait_for_timeout(150)

            canvas_box = frame.locator("#overlayCanvas").bounding_box()
            assert canvas_box is not None
            cx = canvas_box["x"]
            cy = canvas_box["y"]
            cw = canvas_box["width"]
            ch = canvas_box["height"]

            # 1. 绘制加选矩形 [0.20, 0.20] -> [0.80, 0.80]
            page.mouse.move(cx + cw * 0.20, cy + ch * 0.20)
            page.mouse.down()
            page.mouse.move(cx + cw * 0.80, cy + ch * 0.80, steps=6)
            page.mouse.up()

            # 2. 切换“减选擦除”，在右半部分 [0.50, 0.20] -> [0.80, 0.80] 绘制减选矩形
            frame.locator('[data-op="subtract"]').click()
            page.mouse.move(cx + cw * 0.50, cy + ch * 0.20)
            page.mouse.down()
            page.mouse.move(cx + cw * 0.80, cy + ch * 0.80, steps=6)
            page.mouse.up()

            # 读取左半保留区 (0.35, 0.50) 与右半擦除区 (0.65, 0.50) 的Canvas RGBA像素
            alpha_state = frame.locator("#overlayCanvas").evaluate(
                """(canvas) => {
                    const ctx = canvas.getContext('2d');
                    const w = canvas.width;
                    const h = canvas.height;
                    const kept = ctx.getImageData(Math.round(w * 0.35), Math.round(h * 0.50), 1, 1).data;
                    const erased = ctx.getImageData(Math.round(w * 0.65), Math.round(h * 0.50), 1, 1).data;
                    const confirmDisabled = document.getElementById('confirmBtn').disabled;
                    return {
                        keptAlpha: kept[3],
                        erasedAlpha: erased[3],
                        confirmDisabled
                    };
                }"""
            )
            assert alpha_state["keptAlpha"] > 60
            assert alpha_state["erasedAlpha"] == 0
            assert alpha_state["confirmDisabled"] is False

            # 3. 再绘制一个覆盖剩余全部选区的subtract矩形 [0.10, 0.10] -> [0.90, 0.90]，此时confirmBtn必须自动禁用
            page.mouse.move(cx + cw * 0.10, cy + ch * 0.10)
            page.mouse.down()
            page.mouse.move(cx + cw * 0.90, cy + ch * 0.90, steps=6)
            page.mouse.up()

            full_erased_state = frame.locator("#overlayCanvas").evaluate(
                """(canvas) => {
                    const ctx = canvas.getContext('2d');
                    const w = canvas.width;
                    const h = canvas.height;
                    const keptAfter = ctx.getImageData(Math.round(w * 0.35), Math.round(h * 0.50), 1, 1).data;
                    return {
                        keptAlphaAfter: keptAfter[3],
                        confirmDisabled: document.getElementById('confirmBtn').disabled
                    };
                }"""
            )
            assert full_erased_state["keptAlphaAfter"] == 0
            assert full_erased_state["confirmDisabled"] is True
        finally:
            browser.close()


# 回归测试（Finding #9）：在真实uvicorn测试服务器与Playwright Chromium <iframe sandbox="allow-scripts" srcdoc="...">中验证null-origin CORS预检、框选+减选遮罩与mask_for一致性、以及confirm -> postMessage(prompt) -> ask宿主桥接
def test_playwright_sandboxed_selection_editor_null_origin_cors_subtract_and_prompt_handoff(
    studio_env: StudioSettings,
) -> None:
    import html as html_mod
    import socket
    import threading
    import time as _time

    import uvicorn
    from playwright.sync_api import sync_playwright
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, Response
    from starlette.routing import Route

    user_lc = studio_env.librechat_images_dir / USER_A
    user_lc.mkdir(parents=True, mode=0o700, exist_ok=True)
    upload_id = "f9000009-1111-2222-3333-444444444444"
    base_file = user_lc / f"{upload_id}__playwright_base.png"
    base_img = Image.new("RGB", (360, 240), (145, 130, 115))
    save_exact(base_img, base_file)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])

    local_cfg = studio_env.model_copy(
        update={"public_base_url": f"http://127.0.0.1:{port}"}
    )

    async def _mock_gemini_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        img_cfg = body["generationConfig"]["imageConfig"]
        req_w, req_h = GEMINI_NATIVE[img_cfg["imageSize"]][img_cfg["aspectRatio"]]
        gen_img = Image.new("RGB", (req_w, req_h), (225, 190, 140))
        buf = io.BytesIO()
        gen_img.save(buf, format="PNG")
        b64_png = base64.b64encode(buf.getvalue()).decode("ascii")
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "inlineData": {
                                        "mimeType": "image/png",
                                        "data": b64_png,
                                    }
                                }
                            ]
                        }
                    }
                ]
            },
        )

    studio_app = create_app(
        settings=local_cfg,
        rules=load_rules(),
        gemini_transport=httpx.MockTransport(_mock_gemini_handler),
    )

    def _call_mcp_http(
        tool_name: str, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        res = httpx.post(
            f"http://127.0.0.1:{port}/mcp",
            headers={
                "X-Studio-Token": VALID_TOKEN,
                "X-LibreChat-User-ID": USER_A,
            },
            json={
                "jsonrpc": "2.0",
                "id": 901,
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": arguments,
                },
            },
            timeout=10.0,
        )
        assert res.status_code == 200
        result = res.json()["result"]
        assert result.get("isError") is False
        meta = json.loads(result["content"][0]["text"])
        resource_html = result["content"][1]["resource"]["text"]
        return meta, resource_html

    server_seen_requests: list[dict[str, Any]] = []
    current_srcdoc: dict[str, str] = {"html": ""}

    async def _host_page(request: Request) -> HTMLResponse:
        del request
        escaped_srcdoc = html_mod.escape(current_srcdoc["html"], quote=True)
        page_html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8" /></head>
<body>
  <iframe
    id="mcp-sandbox-frame"
    title="MCP HTML Resource (Embedded Content)"
    sandbox="allow-scripts"
    srcdoc="{escaped_srcdoc}"
    style="width:820px;height:680px;border:0;"
  ></iframe>
  <iframe
    id="mcp-compare-frame"
    title="MCP Compare Resource"
    sandbox="allow-scripts"
    style="width:820px;height:680px;border:0;"
  ></iframe>
  <script>
    window.__receivedMessages = [];
    window.__askSubmissions = [];
    function ask(submission) {{
      window.__askSubmissions.push(submission);
    }}
    window.addEventListener('message', (event) => {{
      if (!event.data || typeof event.data !== 'object') return;
      window.__receivedMessages.push({{
        origin: event.origin,
        data: event.data
      }});
      if (event.data.type === 'prompt' && event.data.payload && typeof event.data.payload.prompt === 'string') {{
        const prompt = event.data.payload.prompt;
        const messageText = `The user clicked a button in an embedded UI Resource, and we got a message of type \\`prompt\\`.\\nThe prompt is:\\n\\n\\`\\`\\`\\n${{prompt}}\\n\\`\\`\\`\\n\\nExecute the intention of the prompt that is mentioned in the message using the tools available to you.`;
        ask({{ text: messageText, rawPrompt: prompt }});
      }}
    }});
  </script>
</body>
</html>"""
        return HTMLResponse(page_html)

    async def _proxy_studio_files(request: Request) -> Response:
        tok = str(request.path_params.get("token") or "")
        raw_body = await request.body()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=studio_app),
            base_url=f"http://127.0.0.1:{port}",
        ) as c:
            upstream = await c.request(
                request.method,
                f"/files/{tok}",
                content=raw_body,
                headers={
                    k: v
                    for k, v in request.headers.items()
                    if k.lower()
                    in (
                        "origin",
                        "access-control-request-method",
                        "access-control-request-headers",
                        "content-type",
                        "range",
                        "if-none-match",
                        "if-modified-since",
                    )
                },
            )
            server_seen_requests.append(
                {
                    "method": request.method,
                    "path": request.url.path,
                    "headers": {k.lower(): v for k, v in request.headers.items()},
                    "status": upstream.status_code,
                    "response_headers": {
                        k.lower(): v for k, v in upstream.headers.items()
                    },
                }
            )
            return Response(
                upstream.content,
                status_code=upstream.status_code,
                headers=dict(upstream.headers),
            )

    async def _proxy_mcp(request: Request) -> Response:
        raw_body = await request.body()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=studio_app),
            base_url=f"http://127.0.0.1:{port}",
        ) as c:
            upstream = await c.request(
                request.method,
                "/mcp",
                content=raw_body,
                headers={
                    k: v
                    for k, v in request.headers.items()
                    if k.lower()
                    in (
                        "x-studio-token",
                        "x-librechat-user-id",
                        "x-librechat-conversation-id",
                        "content-type",
                        "accept",
                    )
                },
            )
            return Response(
                upstream.content,
                status_code=upstream.status_code,
                headers=dict(upstream.headers),
            )

    gateway_app = Starlette(
        routes=[
            Route("/host", endpoint=_host_page, methods=["GET"]),
            Route("/mcp", endpoint=_proxy_mcp, methods=["POST", "GET", "DELETE"]),
            Route(
                "/studio-files/{token:path}",
                endpoint=_proxy_studio_files,
                methods=["GET", "HEAD", "POST", "OPTIONS"],
            ),
        ]
    )

    config = uvicorn.Config(
        gateway_app,
        host="127.0.0.1",
        port=port,
        log_level="error",
    )
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    try:
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                _time.sleep(0.05)

        sel_meta, sel_html = _call_mcp_http("select_region", {"image_id": upload_id})
        image_id = sel_meta["image_id"]
        assert image_id == upload_id
        assert not is_editor_token(sel_meta["preview_url"].rsplit("/", 1)[-1])
        current_srcdoc["html"] = sel_html

        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path="/usr/bin/chromium",
                headless=True,
                args=["--no-sandbox", "--disable-gpu"],
            )
            try:
                page = browser.new_page(viewport={"width": 900, "height": 800})
                page.goto(f"http://127.0.0.1:{port}/host", wait_until="networkidle")

                frame = page.frame_locator("#mcp-sandbox-frame")
                frame.locator("#confirmBtn").wait_for(state="visible")
                frame.locator("#baseImg").evaluate(
                    """(img) => new Promise((resolve, reject) => {
                        if (img.complete && img.naturalWidth > 0) return resolve(true);
                        img.addEventListener('load', () => resolve(true), { once: true });
                        img.addEventListener('error', () => reject(new Error('baseImg load error')), { once: true });
                    })"""
                )
                page.wait_for_timeout(100)

                # 1. 验证null-origin下从baseImg读取Canvas像素不会触发CORS污染SecurityError
                img_pixel = frame.locator("#baseImg").evaluate(
                    """(img) => {
                        const c = document.createElement('canvas');
                        c.width = img.naturalWidth;
                        c.height = img.naturalHeight;
                        const ctx = c.getContext('2d');
                        ctx.drawImage(img, 0, 0);
                        return Array.from(ctx.getImageData(10, 10, 1, 1).data);
                    }"""
                )
                assert img_pixel == [145, 130, 115, 255]

                canvas_box = frame.locator("#overlayCanvas").bounding_box()
                assert canvas_box is not None
                cx = canvas_box["x"]
                cy = canvas_box["y"]
                cw = canvas_box["width"]
                ch = canvas_box["height"]

                # 2. 在画布上框选(add) [0.15, 0.15] -> [0.85, 0.85]
                page.mouse.move(cx + cw * 0.15, cy + ch * 0.15)
                page.mouse.down()
                page.mouse.move(cx + cw * 0.85, cy + ch * 0.85, steps=8)
                page.mouse.up()

                # 3. 切换到减选(subtract)，挖除中心右半块 [0.50, 0.25] -> [0.80, 0.75]
                frame.locator('[data-op="subtract"]').click()
                page.mouse.move(cx + cw * 0.50, cy + ch * 0.25)
                page.mouse.down()
                page.mouse.move(cx + cw * 0.80, cy + ch * 0.75, steps=8)
                page.mouse.up()

                # 填写局部修改要求并点击“确认选区”
                frame.locator("#instructionInput").fill("将选区替换为浅色橡木纹理")
                frame.locator("#confirmBtn").click()

                # 等待父页面收到来自null-origin iframe的postMessage(type='prompt')并触发ask
                page.wait_for_function(
                    "() => window.__askSubmissions && window.__askSubmissions.length === 1",
                    timeout=5000,
                )

                # 读取前端overlayCanvas尺寸与二值化Alpha掩码
                overlay_snapshot = frame.locator("#overlayCanvas").evaluate(
                    """(canvas) => {
                        const w = canvas.width;
                        const h = canvas.height;
                        const rgba = canvas.getContext('2d').getImageData(0, 0, w, h).data;
                        const alpha = [];
                        for (let i = 3; i < rgba.length; i += 4) {
                            alpha.push(rgba[i] > 0 ? 1 : 0);
                        }
                        return {
                            width: w,
                            height: h,
                            alpha,
                            keptSample: rgba[((Math.round(h * 0.50) * w + Math.round(w * 0.30)) * 4) + 3],
                            subtractedSample: rgba[((Math.round(h * 0.50) * w + Math.round(w * 0.65)) * 4) + 3],
                            outsideSample: rgba[((Math.round(h * 0.05) * w + Math.round(w * 0.05)) * 4) + 3]
                        };
                    }"""
                )
                assert overlay_snapshot["keptSample"] > 60
                assert overlay_snapshot["subtractedSample"] == 0
                assert overlay_snapshot["outsideSample"] == 0

                # 4. 核对服务端真实收到的null-origin OPTIONS预检请求与POST确认请求
                preflight_reqs = [
                    r
                    for r in server_seen_requests
                    if r["method"] == "OPTIONS" and "/studio-files/" in r["path"]
                ]
                assert len(preflight_reqs) >= 1
                assert preflight_reqs[0]["headers"].get("origin") == "null"
                assert (
                    preflight_reqs[0]["headers"]
                    .get("access-control-request-method", "")
                    .upper()
                    == "POST"
                )
                assert preflight_reqs[0]["status"] == 204
                assert (
                    preflight_reqs[0]["response_headers"].get(
                        "access-control-allow-origin"
                    )
                    == "*"
                )

                post_reqs = [
                    r
                    for r in server_seen_requests
                    if r["method"] == "POST" and "/studio-files/" in r["path"]
                ]
                assert len(post_reqs) == 1
                assert post_reqs[0]["headers"].get("origin") == "null"
                assert post_reqs[0]["status"] == 200
                assert (
                    post_reqs[0]["response_headers"].get("access-control-allow-origin")
                    == "*"
                )

                # 5. 核对confirm -> postMessage({ type: 'prompt', payload: { prompt } }) -> ask宿主交接
                messages = page.evaluate("() => window.__receivedMessages")
                prompt_msgs = [m for m in messages if m["data"].get("type") == "prompt"]
                assert len(prompt_msgs) == 1
                assert prompt_msgs[0]["origin"] == "null"
                prompt_str = prompt_msgs[0]["data"]["payload"]["prompt"]

                ask_calls = page.evaluate("() => window.__askSubmissions")
                assert len(ask_calls) == 1
                assert prompt_str in ask_calls[0]["text"]
                assert "将选区替换为浅色橡木纹理" in prompt_str
                assert image_id in prompt_str

                sel_match = re.search(r"sel_[0-9a-f]{32}", prompt_str)
                assert sel_match is not None
                confirmed_sel_id = sel_match.group(0)

                # 6. 核对前端Canvas渲染遮罩与后端mask_for生成的掩码逐像素高度一致
                confirmed_record = load_confirmed_selection(
                    local_cfg,
                    USER_A,
                    confirmed_sel_id,
                    base_image_id=image_id,
                )
                cw_int = int(overlay_snapshot["width"])
                ch_int = int(overlay_snapshot["height"])
                backend_mask = (
                    np.asarray(
                        mask_for(confirmed_record["selection"], (cw_int, ch_int))
                    )
                    > 0
                ).astype(np.uint8)
                frontend_mask = np.asarray(
                    overlay_snapshot["alpha"], dtype=np.uint8
                ).reshape((ch_int, cw_int))
                agreement = float(np.mean(backend_mask == frontend_mask))
                assert agreement >= 0.985
                assert (
                    int(backend_mask[round(ch_int * 0.50), round(cw_int * 0.30)]) == 1
                )
                assert (
                    int(backend_mask[round(ch_int * 0.50), round(cw_int * 0.65)]) == 0
                )
                assert (
                    int(backend_mask[round(ch_int * 0.05), round(cw_int * 0.05)]) == 0
                )

                # 7. 调用edit_region并将返回的对比器HTML挂载入第二个sandbox="allow-scripts" iframe，验证双图CORS像素差分正常
                _, cmp_html = _call_mcp_http(
                    "edit_region",
                    {
                        "base_id": image_id,
                        "selection_id": confirmed_sel_id,
                        "instruction": "将选区替换为浅色橡木纹理",
                        "size": "1K",
                    },
                )
                page.eval_on_selector(
                    "#mcp-compare-frame",
                    "(el, html) => { el.srcdoc = html; }",
                    cmp_html,
                )
                cmp_frame = page.frame_locator("#mcp-compare-frame")
                cmp_frame.locator("#compareCanvas").wait_for(state="visible")
                cmp_frame.locator("#compareCanvas").evaluate(
                    """(canvas) => new Promise((resolve, reject) => {
                        const deadline = Date.now() + 5000;
                        function check() {
                            if (canvas.width === 360 && canvas.height === 240) {
                                return resolve(true);
                            }
                            if (Date.now() > deadline) {
                                return reject(new Error('Timeout waiting for compareCanvas load'));
                            }
                            setTimeout(check, 50);
                        }
                        check();
                    })"""
                )
                cmp_frame.locator("#diffCheck").check()
                cmp_pixels = cmp_frame.locator("#compareCanvas").evaluate(
                    """(canvas) => {
                        const ctx = canvas.getContext('2d');
                        const kept = Array.from(ctx.getImageData(Math.round(360 * 0.30), Math.round(240 * 0.50), 1, 1).data);
                        const subtracted = Array.from(ctx.getImageData(Math.round(360 * 0.65), Math.round(240 * 0.50), 1, 1).data);
                        return { kept, subtracted };
                    }"""
                )
                # 选区内已修改像素经buildDiffOverlay标红（R通道显著高于原图145且高于G/B），而subtract挖空区保持原图(145, 130, 115)
                assert cmp_pixels["kept"][0] > 230
                assert cmp_pixels["kept"][0] > cmp_pixels["kept"][1] + 100
                assert cmp_pixels["subtracted"] == [145, 130, 115, 255]
            finally:
                browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=5.0)
