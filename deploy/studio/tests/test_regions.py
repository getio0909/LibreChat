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
from deploy.studio.regions import (
    DEFAULT_SAM2_ONNX_DIR,
    OnnxSamSegmenter,
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
)
from deploy.studio.store import list_user_images, resolve_image

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
        base_arr = np.frombuffer(
            rng.randbytes(w * h * 3), dtype=np.uint8
        ).reshape((h, w, 3))
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
        comp = composite_local(
            base_path, gen_path, selection, ctx["crop"], dest_path
        )
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
    rec_a = save_confirmed_selection(
        studio_env, USER_A, uid_a1, path_a1, selection
    )
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

    captured_ark_requests: list[dict[str, Any]] = []

    async def _mock_ark_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured_ark_requests.append(body)
        req_w, req_h = (int(x) for x in body["size"].split("x"))
        gen_img = Image.new("RGB", (req_w, req_h), (210, 195, 175))
        buf = io.BytesIO()
        gen_img.save(buf, format="PNG")
        b64_png = base64.b64encode(buf.getvalue()).decode("ascii")
        return httpx.Response(
            200,
            json={"data": [{"b64_json": b64_png}]},
        )

    app = create_app(
        settings=studio_env,
        rules=load_rules(),
        ark_transport=httpx.MockTransport(_mock_ark_handler),
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

            # 验证发往生图服务的提示词包含局部裁切<bbox>约束
            assert len(captured_ark_requests) == 1
            sent_prompt = captured_ark_requests[0]["prompt"]
            assert "<bbox>" in sent_prompt and "</bbox>" in sent_prompt

            # 验证落盘后的结果图在选区外与底图100%字节一致
            saved_resolved = resolve_image(studio_env, USER_A, edit_summary["id"])
            saved_img = open_image(saved_resolved.path)
            mask = mask_for(confirm_selection, base_img.size)
            assert count_outside_changes(base_img, saved_img, mask) == 0

            # 验证 list_images 能识别 edit_region 产出的新图片
            listed = list_user_images(studio_env, USER_A)
            assert any(item["id"] == edit_summary["id"] for item in listed)

    asyncio.run(_run())


# 校验OnnxSamSegmenter的SHA256清单验证、首次懒加载、单图编码缓存、串行推理与点选分割路由
def test_segmentation_adapter_mocked_and_endpoint(
    studio_env: StudioSettings, tmp_path: Path
) -> None:
    model_dir = tmp_path / "sam2_onnx"
    model_dir.mkdir(parents=True, mode=0o700)
    enc_bytes = b"mock-onnx-encoder-weights-v1"
    dec_bytes = b"mock-onnx-decoder-weights-v1"
    (model_dir / "encoder.onnx").write_bytes(enc_bytes)
    (model_dir / "decoder.onnx").write_bytes(dec_bytes)
    manifest = {
        "source": "pinned-test-sam2.1-hiera-tiny-onnx",
        "revision": "0123456789abcdef0123456789abcdef01234567",
        "files": {
            "encoder.onnx": {"sha256": hashlib.sha256(enc_bytes).hexdigest()},
            "decoder.onnx": {"sha256": hashlib.sha256(dec_bytes).hexdigest()},
        },
    }
    (model_dir / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    stats = {"loaded": 0, "encoder_runs": 0, "decoder_runs": 0}

    class _MockNodeArg:
        def __init__(self, name: str) -> None:
            self.name = name

    class _MockSession:
        def __init__(self, model_path: str) -> None:
            stats["loaded"] += 1
            self.is_encoder = model_path.endswith("encoder.onnx")

        def get_inputs(self) -> list[_MockNodeArg]:
            if self.is_encoder:
                return [_MockNodeArg("image")]
            return [
                _MockNodeArg("image_embed"),
                _MockNodeArg("point_coords"),
                _MockNodeArg("point_labels"),
            ]

        def get_outputs(self) -> list[_MockNodeArg]:
            if self.is_encoder:
                return [_MockNodeArg("image_embed")]
            return [_MockNodeArg("masks"), _MockNodeArg("iou_predictions")]

        def run(
            self, output_names: list[str], feeds: dict[str, Any]
        ) -> list[np.ndarray]:
            if self.is_encoder:
                stats["encoder_runs"] += 1
                return [np.ones((1, 256, 64, 64), dtype=np.float32)]
            stats["decoder_runs"] += 1
            mask = np.zeros((1, 1, 256, 256), dtype=np.float32)
            mask[0, 0, 64:192, 64:192] = 5.0
            scores = np.array([[0.94]], dtype=np.float32)
            return [mask, scores]

    segmenter = OnnxSamSegmenter(
        model_dir=model_dir, session_factory=_MockSession
    )
    assert segmenter.ready() is True
    # 首次调用前不应提前加载ONNX会话（懒加载）
    assert stats["loaded"] == 0

    img1_path = tmp_path / "img1.png"
    img2_path = tmp_path / "img2.png"
    save_exact(Image.new("RGB", (200, 160), (100, 120, 140)), img1_path)
    save_exact(Image.new("RGB", (200, 160), (140, 120, 100)), img2_path)

    res1 = segmenter.segment(img1_path, [{"x": 500, "y": 500, "label": 1}])
    assert stats["loaded"] == 2
    assert stats["encoder_runs"] == 1
    assert stats["decoder_runs"] == 1
    assert res1["score"] == pytest.approx(0.94, rel=1e-2)
    assert len(res1["shapes"]) >= 1
    assert res1["selected_pixels"] > 100

    # 对同一张图再次发起点选分割，应复用单图编码器缓存（encoder_runs保持为1）
    res2 = segmenter.segment(
        img1_path,
        [{"x": 500, "y": 500, "label": 1}, {"x": 100, "y": 100, "label": 0}],
    )
    assert stats["encoder_runs"] == 1
    assert stats["decoder_runs"] == 2
    assert res2["selected_pixels"] > 100

    # 切换到第二张图片时，重新编码并替换单图缓存
    segmenter.segment(img2_path, [{"x": 450, "y": 450, "label": 1}])
    assert stats["encoder_runs"] == 2
    assert stats["decoder_runs"] == 3

    # 验证通过 /files/<token> POST action=segment 端到端调用
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
                    "points": [{"x": 500, "y": 500, "label": 1}],
                },
            )
            assert seg_http.status_code == 200
            data = seg_http.json()
            assert len(data["shapes"]) >= 1
            assert data["selected_pixels"] > 100

    asyncio.run(_run_http())

    # 验证若manifest中的SHA256与实际模型文件不匹配，verify_onnx_model_dir拒绝加载
    bad_manifest = {
        "source": "tampered",
        "files": {
            "encoder.onnx": {"sha256": "0" * 64},
            "decoder.onnx": {"sha256": hashlib.sha256(dec_bytes).hexdigest()},
        },
    }
    (model_dir / "manifest.json").write_text(
        json.dumps(manifest | bad_manifest), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        verify_onnx_model_dir(model_dir)


# 若本地已安装通过SHA256校验的真实SAM 2.1 Tiny ONNX模型则执行真实CPU冒烟推理，否则跳过
def test_real_onnx_sam_segmenter_smoke_if_available(tmp_path: Path) -> None:
    candidate_dir = Path(
        os.environ.get("STUDIO_SAM2_ONNX_DIR") or DEFAULT_SAM2_ONNX_DIR
    )
    segmenter = OnnxSamSegmenter(model_dir=candidate_dir)
    if not segmenter.ready():
        pytest.skip(
            f"Verified local SAM 2.1 Tiny ONNX model not present at {candidate_dir}; skipping real ONNX smoke test"
        )
    sample_path = tmp_path / "real_smoke.png"
    img = Image.new("RGB", (256, 256), (40, 40, 40))
    draw = ImageDraw.Draw(img)
    draw.rectangle((64, 64, 192, 192), fill=(230, 220, 200))
    save_exact(img, sample_path)
    out = segmenter.segment(sample_path, [{"x": 500, "y": 500, "label": 1}])
    assert len(out["shapes"]) >= 1
    assert out["selected_pixels"] >= 16


# 回归测试（Finding #1）：调色板P模式含tRNS透明像素的PNG底图经局部合回后，选区外RGBA四通道（含Alpha）100%一致
def test_palette_png_with_trns_preserves_alpha_outside_selection(
    tmp_path: Path,
) -> None:
    w, h = 160, 120
    pal_img = Image.new("P", (w, h), 0)
    palette: list[int] = [
        10, 20, 30,     # 索引0：完全透明背景
        180, 140, 90,   # 索引1：半透明边框
        60, 130, 190,   # 索引2：不透明主体
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
            save_exact(
                Image.new("RGB", (240, 180), (141, 120, 100)), base_file
            )
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
    rec = save_confirmed_selection(
        studio_env, USER_A, upload_id, base_file, selection
    )
    sel_id = rec["selection_id"]

    async def _mutating_ark_handler(request: httpx.Request) -> httpx.Response:
        # 模拟并发请求在供应商响应返回前修改了底图文件字节
        save_exact(Image.new("RGB", (320, 240), (141, 120, 100)), base_file)
        body = json.loads(request.content.decode("utf-8"))
        req_w, req_h = (int(x) for x in body["size"].split("x"))
        gen_img = Image.new("RGB", (req_w, req_h), (210, 190, 160))
        buf = io.BytesIO()
        gen_img.save(buf, format="PNG")
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "b64_json": base64.b64encode(buf.getvalue()).decode(
                            "ascii"
                        )
                    }
                ]
            },
        )

    app = create_app(
        settings=studio_env,
        rules=load_rules(),
        ark_transport=httpx.MockTransport(_mutating_ark_handler),
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
    rec = save_confirmed_selection(
        studio_env, USER_A, upload_id, base_file, selection
    )
    sel_id = rec["selection_id"]

    provider_calls = 0

    async def _ark_handler_then_transient_base_fault(
        request: httpx.Request,
    ) -> httpx.Response:
        nonlocal provider_calls
        provider_calls += 1
        # 模拟供应商已成功生成并计费，但此时底图短暂被占用/替换导致回贴阶段失败
        save_exact(Image.new("RGB", (320, 240), (99, 99, 99)), base_file)
        body = json.loads(request.content.decode("utf-8"))
        req_w, req_h = (int(x) for x in body["size"].split("x"))
        gen_img = Image.new("RGB", (req_w, req_h), (215, 185, 125))
        buf = io.BytesIO()
        gen_img.save(buf, format="PNG")
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "b64_json": base64.b64encode(buf.getvalue()).decode(
                            "ascii"
                        )
                    }
                ]
            },
        )

    app = create_app(
        settings=studio_env,
        rules=load_rules(),
        ark_transport=httpx.MockTransport(
            _ark_handler_then_transient_base_fault
        ),
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
            assert "finish_region_edit" in err_payload["error"]
            job_id = err_payload["job_id"]
            assert job_id.startswith("img_")

            job_dir = studio_env.data_dir / "users" / USER_A / "jobs" / job_id
            assert (job_dir / "crop.png").is_file()
            assert (job_dir / "local_recovery.json").is_file()
            assert not (job_dir / "original.png").exists()

            # 2. list_images 必须暴露该 job_id 为 recoverable 状态并附带计费与恢复警告
            list_res = await client.post(
                "/mcp",
                headers=headers_a,
                json={
                    "jsonrpc": "2.0",
                    "id": 42,
                    "method": "tools/call",
                    "params": {"name": "list_images", "arguments": {}},
                },
            )
            assert list_res.status_code == 200
            list_payload = json.loads(
                list_res.json()["result"]["content"][0]["text"]
            )
            rec_items = [
                item
                for item in list_payload["images"]
                if item["id"] == job_id
            ]
            assert len(rec_items) == 1
            assert rec_items[0]["state"] == "recoverable"
            assert rec_items[0]["warning"] is True
            assert any(
                job_id in w and "finish_region_edit" in w
                for w in list_payload["warnings"]
            )

            # 3. 恢复原底图字节后，调用 finish_region_edit(job_id) 直接用已保存的 generated_crop.png 完成回贴，不触发第二次供应商请求
            base_file.write_bytes(original_base_bytes)
            recover_res = await client.post(
                "/mcp",
                headers=headers_a,
                json={
                    "jsonrpc": "2.0",
                    "id": 43,
                    "method": "tools/call",
                    "params": {
                        "name": "finish_region_edit",
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
            assert (
                count_outside_changes(original_base_img, saved_img, mask) == 0
            )
            assert saved_img.getpixel((160, 120)) == (215, 185, 125)

    asyncio.run(_run())


