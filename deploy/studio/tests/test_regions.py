import asyncio
import base64
import hashlib
import io
import json
import os
import random
import re
import shutil
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
    load_confirmed_selection,
    local_context,
    mask_for,
    object_placement,
    open_image,
    save_confirmed_selection,
    save_exact,
    selection_proof,
    selection_value,
    sign_image_token,
    source_dimensions,
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
            assert summary["editor_url"].startswith(
                "https://dev.xuqian.io/studio-files/"
            )

            ui_res = content[1]["resource"]
            assert ui_res["uri"] == f"ui://zyb_studio/selection/{upload_id}"
            assert ui_res["mimeType"] == "text/html"
            editor_html = ui_res["text"]

            # 验证UI HTML仅包含签名URL，不含任何密钥、内部文件路径或模型厂商名
            assert summary["editor_url"] in editor_html
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
            token = summary["editor_url"].rsplit("/", 1)[-1]
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
        token, _ = sign_image_token(studio_env, USER_A, upload_id)
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
