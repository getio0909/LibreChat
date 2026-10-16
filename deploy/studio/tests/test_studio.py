import asyncio
import base64
import io
import json
import os
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image, ImageCms

from deploy.studio.app import create_app
from deploy.studio.config import StudioSettings, load_rules, load_settings
from deploy.studio.images import (
    NATIVE,
    GenerationUncertain,
    build_edit_prompt,
    build_generate_prompt,
    generate_seedream,
    normalize_reference,
    source_dimensions,
)
from deploy.studio.kb import load_kb_index
from deploy.studio.models3d import (
    build_seed3d_create_payload,
    extract_glb_bytes,
    parse_seed3d_create_response,
    parse_seed3d_query_response,
    sign_file_token,
)
from deploy.studio.planner import build_planner_system_prompt, plan_concepts
from deploy.studio.store import list_user_images, resolve_image

# 测试固定常量定义
VALID_TOKEN = "a" * 64
VALID_SIGNING_KEY = "f" * 64
USER_A = "65f00000000000000000000a"
USER_B = "65f00000000000000000000b"
FIXTURE_KB_TEMPLATE = Path(__file__).resolve().parent / "fixtures" / "kb"


# 生成指定尺寸与格式的测试图像字节
def _make_image_bytes(
    width: int = 64,
    height: int = 48,
    color: tuple[int, ...] = (180, 140, 90),
    fmt: str = "JPEG",
    mode: str = "RGB",
    exif_orientation: int | None = None,
    with_icc: bool = False,
) -> bytes:
    img = Image.new(mode, (width, height), color)
    buf = io.BytesIO()
    save_kwargs: dict[str, Any] = {}
    if exif_orientation is not None:
        exif = img.getexif()
        exif[0x0112] = exif_orientation
        save_kwargs["exif"] = exif.tobytes()
    if with_icc:
        srgb_profile = ImageCms.createProfile("sRGB")
        cms_prof = ImageCms.ImageCmsProfile(srgb_profile)
        save_kwargs["icc_profile"] = cms_prof.tobytes()
    img.save(buf, format=fmt, **save_kwargs)
    return buf.getvalue()


# 从测试模板目录构建包含记录、目录与衍生图的微型测试知识库
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
        (ref_dir / f"{asset_id}.jpg").write_bytes(
            _make_image_bytes(160, 120, color=color, fmt="JPEG")
        )
        (thumb_dir / f"{asset_id}.webp").write_bytes(
            _make_image_bytes(64, 48, color=color, fmt="WEBP")
        )
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
        "gemini_api_key": "test-gemini-key-no-spaces",
        "ark_api_key": "test-ark-key-no-spaces",
        "librechat_images_dir": str(lc_dir),
        "data_dir": str(data_dir),
        "kb_root": str(kb_dir),
        "seed3d_poll_initial_seconds": 0.01,
        "seed3d_poll_max_interval_seconds": 0.02,
    }
    secret_file.write_text(json.dumps(secret_data), encoding="utf-8")
    secret_file.chmod(0o600)
    return load_settings(secret_file)


# 校验配置文件权限与加载逻辑
def test_settings_permissions_and_defaults(tmp_path: Path) -> None:
    secret_file = tmp_path / "bad_perms.json"
    secret_file.write_text(
        json.dumps(
            {
                "shared_token": VALID_TOKEN,
                "file_signing_key": VALID_SIGNING_KEY,
                "gemini_api_key": "gkey",
                "ark_api_key": "akey",
            }
        ),
        encoding="utf-8",
    )
    secret_file.chmod(0o644)
    with pytest.raises(ValueError, match="permissions"):
        load_settings(secret_file)

    secret_file.chmod(0o600)
    loaded = load_settings(secret_file)
    assert loaded.seedream_model == "doubao-seedream-5-0-pro-260628"
    assert loaded.seed3d_model == "doubao-seed3d-2-0-260328"
    assert loaded.planner_model == "gemini-4-argon"
    assert loaded.planner_thinking_level == "HIGH"
    assert loaded.max_parallel_per_user == 3
    assert loaded.seed3d_max_parallel == 5
    assert loaded.public_base_url == "https://dev.xuqian.io"
    assert loaded.file_token_ttl_seconds == 86400
    assert loaded.seedream_timeout_seconds == 240
    assert loaded.planner_timeout_seconds == 540
    assert loaded.kb_root == Path("/kb")


# 校验/health端点与/mcp端点的认证和用户ID隔离检查
def test_auth_and_health_endpoints(studio_env: StudioSettings) -> None:
    app = create_app(settings=studio_env, rules=load_rules())

    async def _run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://studio:8000"
        ) as client:
            health_res = await client.get("/health")
            assert health_res.status_code == 200
            assert health_res.text == "OK"

            rpc_body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

            # 缺失X-Studio-Token返回403
            res_no_token = await client.post(
                "/mcp",
                headers={"X-LibreChat-User-ID": USER_A},
                json=rpc_body,
            )
            assert res_no_token.status_code == 403

            # 错误X-Studio-Token返回403
            res_bad_token = await client.post(
                "/mcp",
                headers={"X-Studio-Token": "b" * 64, "X-LibreChat-User-ID": USER_A},
                json=rpc_body,
            )
            assert res_bad_token.status_code == 403

            # 缺失或非法X-LibreChat-User-ID返回403
            res_no_user = await client.post(
                "/mcp",
                headers={"X-Studio-Token": VALID_TOKEN},
                json=rpc_body,
            )
            assert res_no_user.status_code == 403

            for bad_uid in (
                "not-a-hex-id",
                "65f00000000000000000000G",
                "../65f00000000000000000000a",
                "1234",
            ):
                res_bad_uid = await client.post(
                    "/mcp",
                    headers={
                        "X-Studio-Token": VALID_TOKEN,
                        "X-LibreChat-User-ID": bad_uid,
                    },
                    json=rpc_body,
                )
                assert res_bad_uid.status_code == 403

    asyncio.run(_run())


# 校验图片ID解析（上传__、生成-、@latest、studio img_）以及跨用户隔离
def test_image_resolution_and_cross_user_isolation(studio_env: StudioSettings) -> None:
    user_a_lc = studio_env.librechat_images_dir / USER_A
    user_b_lc = studio_env.librechat_images_dir / USER_B
    user_a_lc.mkdir(parents=True, mode=0o700)
    user_b_lc.mkdir(parents=True, mode=0o700)

    upload_id_a = "11111111-2222-3333-4444-555555555555"
    gen_id_a = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    upload_id_b = "99999999-8888-7777-6666-555555555555"

    img_upload_a = user_a_lc / f"{upload_id_a}__altar_photo.jpg"
    img_upload_a.write_bytes(_make_image_bytes(120, 90))
    os.utime(img_upload_a, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))

    img_gen_a = user_a_lc / f"{gen_id_a}-generate_image_img_AbCdEf123.webp"
    img_gen_a.write_bytes(_make_image_bytes(160, 120, fmt="WEBP"))
    os.utime(img_gen_a, ns=(1_700_000_005_000_000_000, 1_700_000_005_000_000_000))

    # 创建工作台原图记录
    studio_id_a = "img_0123456789abcdef0123456789abcdef"
    job_dir_a = studio_env.data_dir / "users" / USER_A / "jobs" / studio_id_a
    job_dir_a.mkdir(parents=True, mode=0o700)
    orig_a = job_dir_a / "original.png"
    orig_bytes = _make_image_bytes(200, 150, fmt="PNG")
    orig_a.write_bytes(orig_bytes)
    os.utime(orig_a, ns=(1_700_000_010_000_000_000, 1_700_000_010_000_000_000))
    (job_dir_a / "receipt.json").write_text(
        json.dumps(
            {
                "state": "saved",
                "width": 200,
                "height": 150,
                "file": "original.png",
                "sha256": "deadbeef",
            }
        ),
        encoding="utf-8",
    )

    # 用户B的上传图片
    img_upload_b = user_b_lc / f"{upload_id_b}__secret.jpg"
    img_upload_b.write_bytes(_make_image_bytes(80, 80))

    # 验证用户A可以解析自己的三种图片以及@latest
    res_up = resolve_image(studio_env, USER_A, upload_id_a)
    assert res_up.id == upload_id_a
    assert res_up.kind == "upload"
    assert (res_up.width, res_up.height) == (120, 90)

    res_gen = resolve_image(studio_env, USER_A, gen_id_a)
    assert res_gen.id == gen_id_a
    assert res_gen.kind == "generated"
    assert (res_gen.width, res_gen.height) == (160, 120)

    res_studio = resolve_image(studio_env, USER_A, studio_id_a)
    assert res_studio.id == studio_id_a
    assert res_studio.kind == "studio"
    assert (res_studio.width, res_studio.height) == (200, 150)

    res_latest = resolve_image(studio_env, USER_A, "@latest")
    assert res_latest.id == studio_id_a

    # 验证list_user_images按时间倒序排列且只包含用户A自己的图片
    listed_a = list_user_images(studio_env, USER_A, limit=10)
    assert [item["id"] for item in listed_a] == [studio_id_a, gen_id_a, upload_id_a]
    assert [item["kind"] for item in listed_a] == ["studio", "generated", "upload"]

    # 验证用户A无法解析用户B的file_id，用户B也无法解析用户A的studio_id或file_id
    with pytest.raises(ValueError, match="not found"):
        resolve_image(studio_env, USER_A, upload_id_b)
    with pytest.raises(ValueError, match="not found"):
        resolve_image(studio_env, USER_B, upload_id_a)
    with pytest.raises(ValueError, match="not found"):
        resolve_image(studio_env, USER_B, studio_id_a)

    # 验证路径穿越与符号链接被拒绝
    for invalid_id in ("../secret", "a/b", "a\\b", ".."):
        with pytest.raises(ValueError):
            resolve_image(studio_env, USER_A, invalid_id)

    sym_uuid = "dddddddd-eeee-ffff-0000-111111111111"
    symlink_file = user_a_lc / f"{sym_uuid}__link.jpg"
    symlink_file.symlink_to(img_upload_b)
    with pytest.raises(ValueError):
        resolve_image(studio_env, USER_A, sym_uuid)


# 校验参考图规范化（CMYK转sRGB、EXIF方向旋转并剥离、长边缩放至<=2048px、>40MP拒绝）
def test_reference_normalization_fixtures(tmp_path: Path) -> None:
    allowed = (tmp_path,)

    # 1. CMYK图像转sRGB JPEG
    cmyk_path = tmp_path / "cmyk_sample.jpg"
    cmyk_path.write_bytes(
        _make_image_bytes(100, 80, color=(20, 80, 120, 10), fmt="JPEG", mode="CMYK")
    )
    cmyk_uri = normalize_reference(cmyk_path, allowed)
    assert cmyk_uri.startswith("data:image/jpeg;base64,")
    with Image.open(
        io.BytesIO(base64.b64decode(cmyk_uri.split(",", 1)[1]))
    ) as out_cmyk:
        assert out_cmyk.format == "JPEG"
        assert out_cmyk.mode == "RGB"
        assert out_cmyk.size == (100, 80)

    # 2. EXIF Orientation=6（顺时针90度）转置并剥离EXIF
    exif_path = tmp_path / "rotated.jpg"
    exif_path.write_bytes(
        _make_image_bytes(120, 60, fmt="JPEG", exif_orientation=6, with_icc=True)
    )
    exif_uri = normalize_reference(exif_path, allowed)
    with Image.open(io.BytesIO(base64.b64decode(exif_uri.split(",", 1)[1]))) as out_rot:
        assert out_rot.size == (60, 120)
        assert not out_rot.getexif()

    # 3. 超过2048像素的图像按比例缩放到长边<=2048
    large_path = tmp_path / "large.png"
    large_path.write_bytes(_make_image_bytes(3000, 1500, fmt="PNG"))
    large_uri = normalize_reference(large_path, allowed)
    with Image.open(
        io.BytesIO(base64.b64decode(large_uri.split(",", 1)[1]))
    ) as out_large:
        assert out_large.size == (2048, 1024)

    # 4. 超过40MP的输入被拒绝
    huge_path = tmp_path / "huge.png"
    Image.new("RGB", (7000, 6000), (0, 0, 0)).save(huge_path, format="PNG")
    with pytest.raises(ValueError, match="40 MP"):
        normalize_reference(huge_path, allowed)


# 校验石材名称中性化（敦煌金/天川金/敦煌壁/梵高皇/金镶玉/梦幻庄园 -> 图N所示石材，保留敦煌壁画）与用途标签
def test_prompt_rules_and_stone_neutralization(tmp_path: Path) -> None:
    rules = load_rules()
    ref1 = tmp_path / "ref1.jpg"
    ref2 = tmp_path / "ref2.jpg"
    ref1.write_bytes(_make_image_bytes(32, 32))
    ref2.write_bytes(_make_image_bytes(32, 32))

    # 整图修改下底图是图1，参考图从图2开始计数
    edit_prompt, edit_paths = build_edit_prompt(
        "把佛龛台面换成敦煌金，背板用梵高皇，墙上保留敦煌壁画，底座换成敦煌壁",
        [
            {"id": "r1", "path": ref1, "use": "材质", "note": "天川金台面样板"},
            {"id": "r2", "path": ref2, "use": "材质", "note": "梵高皇背板样板"},
        ],
        rules,
    )
    assert edit_paths == [ref1, ref2]
    assert "敦煌金" not in edit_prompt
    assert "天川金" not in edit_prompt
    assert "梵高皇" not in edit_prompt
    assert "图2所示石材" in edit_prompt
    assert "图3所示石材" in edit_prompt
    assert "敦煌壁画" in edit_prompt
    assert rules.edit_rules in edit_prompt
    assert "图1是唯一的修改底图" in edit_prompt

    # 生图模式下参考图从图1开始计数
    gen_prompt, gen_paths = build_generate_prompt(
        "设计一款金镶玉与梦幻庄园结合的供桌",
        [{"id": "r1", "path": ref1, "use": "材质", "note": "金镶玉纹理"}],
        rules,
    )
    assert gen_paths == [ref1]
    assert "金镶玉" not in gen_prompt
    assert "梦幻庄园" not in gen_prompt
    assert "图1所示石材" in gen_prompt
    assert rules.reference_rules in gen_prompt


# 校验Seedream请求载荷、各比例尺寸、receipt状态流转与超时uncertain无重试
def test_seedream_payloads_sizes_and_uncertain_timeout(
    studio_env: StudioSettings, tmp_path: Path
) -> None:
    raw_provider_jpg = _make_image_bytes(64, 48, fmt="JPEG")
    captured_requests: list[dict[str, Any]] = []
    seen_pending_states: list[str] = []

    # 校验所有1K与2K档位的预设尺寸及原图比例尺寸
    expected_sizes = {
        ("1K", "1:1"): "1024x1024",
        ("1K", "4:3"): "1152x864",
        ("1K", "3:4"): "864x1152",
        ("1K", "16:9"): "1280x720",
        ("1K", "9:16"): "720x1280",
        ("2K", "1:1"): "2048x2048",
        ("2K", "4:3"): "2304x1728",
        ("2K", "3:4"): "1728x2304",
        ("2K", "16:9"): "2560x1440",
        ("2K", "9:16"): "1440x2560",
    }
    for (sz, asp), expected_str in expected_sizes.items():
        w, h = NATIVE[sz][asp]
        assert f"{w}x{h}" == expected_str
        assert 921600 <= w * h <= 4624220

    src_w, src_h = source_dimensions("2K", (1200, 900))
    assert (src_w, src_h) == (2368, 1776)

    async def _run() -> None:
        current_job_dir: Path | None = None

        async def _ark_handler(request: httpx.Request) -> httpx.Response:
            assert request.url == httpx.URL(
                "https://ark.cn-beijing.volces.com/api/v3/images/generations"
            )
            assert (
                request.headers["authorization"] == f"Bearer {studio_env.ark_api_key}"
            )
            if current_job_dir is not None:
                receipt_now = json.loads(
                    (current_job_dir / "receipt.json").read_text(encoding="utf-8")
                )
                seen_pending_states.append(receipt_now["state"])
            body = json.loads(request.content.decode("utf-8"))
            captured_requests.append(body)
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"b64_json": base64.b64encode(raw_provider_jpg).decode("ascii")}
                    ]
                },
            )

        mock_transport = httpx.MockTransport(_ark_handler)

        # 单张参考图作为字符串发送，保存原始字节与receipt状态流转pending -> saved
        current_job_dir = tmp_path / "job_single"
        res1 = await generate_seedream(
            studio_env,
            "test prompt",
            ["data:image/jpeg;base64,AAAA"],
            size="1K",
            aspect="4:3",
            destination=current_job_dir,
            transport=mock_transport,
        )
        assert seen_pending_states == ["pending"]
        assert captured_requests[-1]["model"] == "doubao-seedream-5-0-pro-260628"
        assert captured_requests[-1]["watermark"] is False
        assert captured_requests[-1]["size"] == "1152x864"
        assert captured_requests[-1]["image"] == "data:image/jpeg;base64,AAAA"
        assert res1["path"].read_bytes() == raw_provider_jpg
        receipt1 = json.loads(
            (current_job_dir / "receipt.json").read_text(encoding="utf-8")
        )
        assert receipt1["state"] == "saved"
        assert "prompt" not in receipt1
        assert "key" not in receipt1

        # 多张参考图作为列表发送，按原图source比例计算尺寸
        current_job_dir = tmp_path / "job_multi"
        await generate_seedream(
            studio_env,
            "edit prompt",
            ["data:image/jpeg;base64,AAAA", "data:image/jpeg;base64,BBBB"],
            size="2K",
            aspect="source",
            source_size=(900, 1200),
            destination=current_job_dir,
            transport=httpx.MockTransport(_ark_handler),
        )
        assert captured_requests[-1]["size"] == "1776x2368"
        assert captured_requests[-1]["image"] == [
            "data:image/jpeg;base64,AAAA",
            "data:image/jpeg;base64,BBBB",
        ]

        # 网络超时后标记uncertain且不自动重试
        timeout_calls = 0

        async def _timeout_handler(request: httpx.Request) -> httpx.Response:
            nonlocal timeout_calls
            timeout_calls += 1
            raise httpx.ReadTimeout("Simulated network timeout after request dispatch")

        timeout_job_dir = tmp_path / "job_timeout"
        with pytest.raises(GenerationUncertain, match="may have been billed"):
            await generate_seedream(
                studio_env,
                "timeout prompt",
                [],
                size="2K",
                aspect="1:1",
                destination=timeout_job_dir,
                transport=httpx.MockTransport(_timeout_handler),
            )
        assert timeout_calls == 1
        timeout_receipt = json.loads(
            (timeout_job_dir / "receipt.json").read_text(encoding="utf-8")
        )
        assert timeout_receipt["state"] == "uncertain"

    asyncio.run(_run())


# 校验Gemini 4 Argon概念规划器REST载荷（thinkingLevel HIGH、JSON Schema、无重试）
def test_planner_gemini_rest_payload_and_no_retry(studio_env: StudioSettings) -> None:
    rules = load_rules()
    captured: list[dict[str, Any]] = []

    async def _gemini_handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-4-argon:generateContent"
        )
        assert request.headers["x-goog-api-key"] == studio_env.gemini_api_key
        payload = json.loads(request.content.decode("utf-8"))
        captured.append(payload)
        concepts = [
            {
                "title": f"方案{i}",
                "positioning": f"定位说明{i}",
                "description": f"整体视觉与结构描述{i}",
                "materials": f"主材建议（待确认）{i}",
                "key_details": f"关键工艺细节{i}",
                "image_prompt": f"A coherent studio concept view {i} with warm stone texture.",
            }
            for i in range(1, 4)
        ]
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"thought": True, "text": "internal reasoning"},
                                {
                                    "text": json.dumps(
                                        {"concepts": concepts}, ensure_ascii=False
                                    )
                                },
                            ]
                        }
                    }
                ]
            },
        )

    async def _run() -> None:
        concepts = await plan_concepts(
            studio_env,
            rules,
            brief="设计一座寺庙大殿供桌",
            count=3,
            transport=httpx.MockTransport(_gemini_handler),
        )
        assert len(concepts) == 3
        assert len(captured) == 1
        gen_cfg = captured[0]["generationConfig"]
        assert gen_cfg["thinkingConfig"]["thinkingLevel"] == "HIGH"
        assert gen_cfg["responseMimeType"] == "application/json"
        assert "responseSchema" in gen_cfg
        sys_text = captured[0]["systemInstruction"]["parts"][0]["text"]
        assert rules.planner_system.replace("{count}", "3") in sys_text
        assert rules.planner_rules.replace("{count}", "3") in sys_text
        assert rules.no_reference_rules.replace("{count}", "3") in sys_text

        # 验证超时不自动重试
        fail_calls = 0

        async def _fail_handler(request: httpx.Request) -> httpx.Response:
            nonlocal fail_calls
            fail_calls += 1
            raise httpx.ReadTimeout("timeout")

        with pytest.raises(RuntimeError, match="no automatic retry"):
            await plan_concepts(
                studio_env,
                rules,
                brief="测试超时",
                count=2,
                transport=httpx.MockTransport(_fail_handler),
            )
        assert fail_calls == 1

    asyncio.run(_run())


# 递归检查JSON Schema中不包含禁用关键字
def _assert_flat_schema(node: Any) -> None:
    forbidden = {"prefixItems", "oneOf", "anyOf", "$ref", "$defs"}
    if isinstance(node, dict):
        overlap = forbidden.intersection(node.keys())
        assert not overlap, f"Forbidden schema keys found: {overlap}"
        for value in node.values():
            _assert_flat_schema(value)
    elif isinstance(node, list):
        for item in node:
            _assert_flat_schema(item)


# 校验MCP tools/list扁平Schema与tools/call端到端调用（generate_image、edit_image、design_concepts、list_images）
def test_mcp_tools_list_and_end_to_end_calls(studio_env: StudioSettings) -> None:
    # 为用户A准备一张上传参考图
    user_a_lc = studio_env.librechat_images_dir / USER_A
    user_a_lc.mkdir(parents=True, mode=0o700)
    upload_id = "f0000000-1111-2222-3333-444444444444"
    (user_a_lc / f"{upload_id}__stone.jpg").write_bytes(
        _make_image_bytes(160, 120, fmt="JPEG")
    )

    # 构造大于3MB的PNG模拟云端返回，验证原图无损保留且MCP展示图自动压缩到<=3MB
    rng_bytes = os.urandom(1100 * 1100 * 3)
    big_png_buf = io.BytesIO()
    Image.frombytes("RGB", (1100, 1100), rng_bytes).save(
        big_png_buf, format="PNG", compress_level=0
    )
    raw_big_png = big_png_buf.getvalue()
    assert len(raw_big_png) > 3 * 1024 * 1024

    ark_requests: list[dict[str, Any]] = []

    async def _ark_handler(request: httpx.Request) -> httpx.Response:
        ark_requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "data": [{"b64_json": base64.b64encode(raw_big_png).decode("ascii")}]
            },
        )

    async def _gemini_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "HIGH"
        concepts = [
            {
                "title": f"禅意石案 {i}",
                "positioning": "大殿主供案",
                "description": "简洁庄严的石木结合供案",
                "materials": "黄色纹理石材（待确认）",
                "key_details": "倒角收边与内收束腰",
                "image_prompt": f"Minimalist temple stone altar concept {i} using 敦煌金.",
            }
            for i in range(1, 3)
        ]
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {"concepts": concepts}, ensure_ascii=False
                                    )
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
        ark_transport=httpx.MockTransport(_ark_handler),
        gemini_transport=httpx.MockTransport(_gemini_handler),
    )

    headers = {
        "X-Studio-Token": VALID_TOKEN,
        "X-LibreChat-User-ID": USER_A,
        "X-LibreChat-Conversation-ID": "NO_PARENT",
    }

    async def _run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=transport, base_url="http://studio:8000"
            ) as client,
        ):
            assert app.state.session_manager is not None
            assert getattr(app.state.session_manager, "_task_group", None) is not None
            # 1. 验证tools/list返回恰好8个工具且Schema完全扁平
            list_res = await client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
            assert list_res.status_code == 200
            tools = list_res.json()["result"]["tools"]
            assert [t["name"] for t in tools] == [
                "design_concepts",
                "generate_image",
                "edit_image",
                "list_images",
                "search_library",
                "compare_materials",
                "create_3d_model",
                "check_3d_model",
            ]
            for tool in tools:
                _assert_flat_schema(tool["inputSchema"])
                serialized = json.dumps(tool, ensure_ascii=False).casefold()
                for forbidden_vendor in (
                    "seedream",
                    "seed3d",
                    "volcengine",
                    "doubao",
                    "ark",
                    "gemini",
                    "argon",
                    "deepseek",
                ):
                    assert forbidden_vendor not in serialized

            # 2. 端到端调用generate_image，返回文本JSON块+小于3MB的展示图像块，且落盘保留原始PNG字节
            call_gen_res = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "generate_image",
                        "arguments": {
                            "prompt": "用敦煌金制作一尊佛龛底座",
                            "aspect": "4:3",
                            "size": "2K",
                            "references": [
                                {"id": upload_id, "use": "材质", "note": "敦煌金样板"}
                            ],
                        },
                    },
                },
            )
            assert call_gen_res.status_code == 200
            result_gen = call_gen_res.json()["result"]
            assert result_gen.get("isError") is False
            content_blocks = result_gen["content"]
            assert len(content_blocks) == 2
            assert content_blocks[0]["type"] == "text"
            assert content_blocks[1]["type"] == "image"

            text_meta = json.loads(content_blocks[0]["text"])
            generated_studio_id = text_meta["id"]
            assert generated_studio_id.startswith("img_")
            assert text_meta["size"] == "1100x1100"
            for forbidden_key in (
                "prompt",
                "model",
                "provider",
                "seedream",
                "doubao",
                "ark",
            ):
                assert forbidden_key not in content_blocks[0]["text"].casefold()

            display_raw = base64.b64decode(content_blocks[1]["data"])
            assert content_blocks[1]["mimeType"] in ("image/jpeg", "image/webp")
            assert len(display_raw) <= 3 * 1024 * 1024

            saved_orig = (
                studio_env.data_dir
                / "users"
                / USER_A
                / "jobs"
                / generated_studio_id
                / "original.png"
            )
            assert saved_orig.read_bytes() == raw_big_png
            assert "敦煌金" not in ark_requests[-1]["prompt"]
            assert "图1所示石材" in ark_requests[-1]["prompt"]

            # 3. 端到端调用edit_image（使用@latest作为底图，保持底图1:1比例）
            call_edit_res = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "edit_image",
                        "arguments": {
                            "base_id": "@latest",
                            "instruction": "将顶部线脚改为天川金材质",
                            "references": [
                                {"id": upload_id, "use": "材质", "note": "天川金样板"}
                            ],
                            "size": "1K",
                        },
                    },
                },
            )
            assert call_edit_res.status_code == 200
            edit_result = call_edit_res.json()["result"]
            assert edit_result.get("isError") is False
            assert ark_requests[-1]["size"] == "1024x1024"
            assert isinstance(ark_requests[-1]["image"], list)
            assert len(ark_requests[-1]["image"]) == 2
            assert "天川金" not in ark_requests[-1]["prompt"]
            assert "图2所示石材" in ark_requests[-1]["prompt"]

            # 4. 端到端调用design_concepts，验证规划+并发出图且不泄露image_prompt与模型名
            call_concepts_res = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "design_concepts",
                        "arguments": {
                            "brief": "设计两款寺庙石材供桌",
                            "count": 2,
                            "with_images": True,
                            "aspect": "16:9",
                            "size": "2K",
                            "reference_ids": [upload_id],
                        },
                    },
                },
            )
            assert call_concepts_res.status_code == 200
            concepts_result = call_concepts_res.json()["result"]
            assert concepts_result.get("isError") is False
            assert len(concepts_result["content"]) == 3
            concepts_summary = json.loads(concepts_result["content"][0]["text"])
            assert concepts_summary["count"] == 2
            for c_item in concepts_summary["concepts"]:
                assert "image_prompt" not in c_item
                assert c_item["image_id"].startswith("img_")

            # 5. 端到端调用list_images
            call_list_res = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {"name": "list_images", "arguments": {"limit": 10}},
                },
            )
            assert call_list_res.status_code == 200
            listed_payload = json.loads(
                call_list_res.json()["result"]["content"][0]["text"]
            )
            assert listed_payload["count"] == 5

    asyncio.run(_run())


# 校验每用户并发信号量限制与MCP超时uncertain错误提示及4xx拒绝状态
def test_concurrency_cap_and_mcp_error_states(
    studio_env: StudioSettings, tmp_path: Path
) -> None:
    # 校验HTTP 401被标记为rejected且不泄露上游响应体
    calls_401 = 0

    async def _handler_401(request: httpx.Request) -> httpx.Response:
        nonlocal calls_401
        calls_401 += 1
        return httpx.Response(401, content=b"sensitive-upstream-secret-detail")

    async def _run_401() -> None:
        job_401 = tmp_path / "job_401"
        with pytest.raises(RuntimeError, match="authentication failed") as exc_info:
            await generate_seedream(
                studio_env,
                "prompt",
                [],
                size="1K",
                aspect="1:1",
                destination=job_401,
                transport=httpx.MockTransport(_handler_401),
            )
        assert "sensitive-upstream-secret-detail" not in str(exc_info.value)
        assert calls_401 == 1
        rec = json.loads((job_401 / "receipt.json").read_text(encoding="utf-8"))
        assert rec["state"] == "rejected"
        assert rec["http_status"] == 401

    asyncio.run(_run_401())

    # 校验单用户并发上限max_parallel_per_user=2
    capped_settings = studio_env.model_copy(update={"max_parallel_per_user": 2})
    active_in_flight = 0
    max_observed_in_flight = 0
    small_jpg = _make_image_bytes(32, 32, fmt="JPEG")

    async def _slow_ark_handler(request: httpx.Request) -> httpx.Response:
        nonlocal active_in_flight, max_observed_in_flight
        active_in_flight += 1
        max_observed_in_flight = max(max_observed_in_flight, active_in_flight)
        await asyncio.sleep(0.05)
        active_in_flight -= 1
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(small_jpg).decode("ascii")}]},
        )

    async def _gemini_3_concepts(request: httpx.Request) -> httpx.Response:
        concepts = [
            {
                "title": f"方向{i}",
                "positioning": "定位",
                "description": "描述",
                "materials": "材质",
                "key_details": "细节",
                "image_prompt": f"Concept {i}",
            }
            for i in range(1, 4)
        ]
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {"concepts": concepts}, ensure_ascii=False
                                    )
                                }
                            ]
                        }
                    }
                ]
            },
        )

    app = create_app(
        settings=capped_settings,
        rules=load_rules(),
        ark_transport=httpx.MockTransport(_slow_ark_handler),
        gemini_transport=httpx.MockTransport(_gemini_3_concepts),
    )

    async def _run_concurrency() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://studio:8000"
        ) as client:
            res = await client.post(
                "/mcp",
                headers={"X-Studio-Token": VALID_TOKEN, "X-LibreChat-User-ID": USER_A},
                json={
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/call",
                    "params": {
                        "name": "design_concepts",
                        "arguments": {
                            "brief": "三方案并发限制测试",
                            "count": 3,
                            "with_images": True,
                        },
                    },
                },
            )
            assert res.status_code == 200
            assert max_observed_in_flight == 2

    asyncio.run(_run_concurrency())


# 校验POST派发后取消请求会落盘uncertain，且启动时自动将遗留pending对账为uncertain并在list_images警告展示
def test_cancellation_marks_uncertain_and_startup_reconciles_pending(
    studio_env: StudioSettings, tmp_path: Path
) -> None:
    dispatched = asyncio.Event()

    async def _hanging_ark_handler(request: httpx.Request) -> httpx.Response:
        dispatched.set()
        await asyncio.sleep(60)
        return httpx.Response(200, json={"data": []})

    async def _run() -> None:
        cancel_job_dir = (
            studio_env.data_dir / "users" / USER_A / "jobs" / "img_cancelled01"
        )
        task = asyncio.create_task(
            generate_seedream(
                studio_env,
                "cancelled prompt",
                [],
                size="1K",
                aspect="1:1",
                destination=cancel_job_dir,
                transport=httpx.MockTransport(_hanging_ark_handler),
            )
        )
        await dispatched.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        cancel_receipt = json.loads(
            (cancel_job_dir / "receipt.json").read_text(encoding="utf-8")
        )
        assert cancel_receipt["state"] == "uncertain"
        assert "finished" in cancel_receipt

        # 构造一个因进程崩溃遗留的pending状态任务，验证应用启动时自动对账为uncertain
        abandoned_job_dir = (
            studio_env.data_dir / "users" / USER_A / "jobs" / "img_abandoned02"
        )
        abandoned_job_dir.mkdir(parents=True, mode=0o700)
        (abandoned_job_dir / "receipt.json").write_text(
            json.dumps(
                {
                    "state": "pending",
                    "started": 1700000000.0,
                    "requested_size": "2048x2048",
                }
            ),
            encoding="utf-8",
        )

        app = create_app(settings=studio_env, rules=load_rules())
        reconciled_receipt = json.loads(
            (abandoned_job_dir / "receipt.json").read_text(encoding="utf-8")
        )
        assert reconciled_receipt["state"] == "uncertain"

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://studio:8000"
        ) as client:
            res = await client.post(
                "/mcp",
                headers={"X-Studio-Token": VALID_TOKEN, "X-LibreChat-User-ID": USER_A},
                json={
                    "jsonrpc": "2.0",
                    "id": 20,
                    "method": "tools/call",
                    "params": {"name": "list_images", "arguments": {"limit": 10}},
                },
            )
            assert res.status_code == 200
            payload = json.loads(res.json()["result"]["content"][0]["text"])
            by_id = {item["id"]: item for item in payload["images"]}
            assert "img_cancelled01" in by_id
            assert by_id["img_cancelled01"]["state"] == "uncertain"
            assert by_id["img_cancelled01"]["warning"] is True
            assert "img_abandoned02" in by_id
            assert by_id["img_abandoned02"]["state"] == "uncertain"
            assert by_id["img_abandoned02"]["warning"] is True
            assert len(payload["warnings"]) >= 2

    asyncio.run(_run())


# 校验初始pending凭据写入后若磁盘故障导致后续write_receipt报错，仍保留GenerationUncertain与计费警告
def test_receipt_write_failure_preserves_uncertain_warning(
    studio_env: StudioSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import deploy.studio.images as images_mod

    real_write_receipt = images_mod.write_receipt

    # 第一次写入pending成功，之后写入抛OSError
    def _flaky_write_receipt(directory: Path, values: dict[str, Any]) -> None:
        if values.get("state") == "pending":
            real_write_receipt(directory, values)
            return
        raise OSError("Simulated storage write failure after dispatch")

    monkeypatch.setattr(images_mod, "write_receipt", _flaky_write_receipt)

    async def _timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout after dispatch")

    async def _502_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=b"bad gateway")

    async def _invalid_body_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    async def _run() -> None:
        for idx, handler in enumerate(
            (_timeout_handler, _502_handler, _invalid_body_handler)
        ):
            with pytest.raises(GenerationUncertain, match="may have been billed"):
                await generate_seedream(
                    studio_env,
                    "prompt",
                    [],
                    size="1K",
                    aspect="1:1",
                    destination=tmp_path / f"flaky_{idx}",
                    transport=httpx.MockTransport(handler),
                )

        # 通过MCP调用generate_image时同样返回uncertain状态与可能已计费提示
        app = create_app(
            settings=studio_env,
            rules=load_rules(),
            ark_transport=httpx.MockTransport(_timeout_handler),
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://studio:8000"
        ) as client:
            res = await client.post(
                "/mcp",
                headers={"X-Studio-Token": VALID_TOKEN, "X-LibreChat-User-ID": USER_A},
                json={
                    "jsonrpc": "2.0",
                    "id": 21,
                    "method": "tools/call",
                    "params": {
                        "name": "generate_image",
                        "arguments": {"prompt": "供桌设计"},
                    },
                },
            )
            assert res.status_code == 200
            result = res.json()["result"]
            assert result["isError"] is True
            err_body = json.loads(result["content"][0]["text"])
            assert err_body["state"] == "uncertain"
            assert "may have been billed" in err_body["error"]

    asyncio.run(_run())


# 校验design_concepts规划阶段受每用户并发上限与全局并发上限约束，且在出图前释放规划信号量
def test_planner_concurrency_caps_single_and_multi_user(
    studio_env: StudioSettings,
) -> None:
    user_c = "65f00000000000000000000c"
    capped = studio_env.model_copy(
        update={"max_parallel_per_user": 2, "global_max_parallel": 3}
    )

    active_planner = 0
    max_planner_observed = 0

    async def _slow_gemini_handler(request: httpx.Request) -> httpx.Response:
        nonlocal active_planner, max_planner_observed
        active_planner += 1
        max_planner_observed = max(max_planner_observed, active_planner)
        await asyncio.sleep(0.05)
        active_planner -= 1
        concept = {
            "title": "禅案",
            "positioning": "定位",
            "description": "描述",
            "materials": "材质（待确认）",
            "key_details": "细节",
            "image_prompt": "A stone altar concept.",
        }
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {"concepts": [concept]}, ensure_ascii=False
                                    )
                                }
                            ]
                        }
                    }
                ]
            },
        )

    app = create_app(
        settings=capped,
        rules=load_rules(),
        gemini_transport=httpx.MockTransport(_slow_gemini_handler),
    )

    async def _call_plan(client: httpx.AsyncClient, uid: str, req_id: int) -> None:
        res = await client.post(
            "/mcp",
            headers={"X-Studio-Token": VALID_TOKEN, "X-LibreChat-User-ID": uid},
            json={
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/call",
                "params": {
                    "name": "design_concepts",
                    "arguments": {
                        "brief": "并发规划限制测试",
                        "count": 1,
                        "with_images": False,
                    },
                },
            },
        )
        assert res.status_code == 200
        assert res.json()["result"]["isError"] is False

    async def _run() -> None:
        nonlocal max_planner_observed
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://studio:8000"
        ) as client:
            # 1. 单用户发起4个并发with_images=False规划请求，受max_parallel_per_user=2限制
            max_planner_observed = 0
            await asyncio.gather(*(_call_plan(client, USER_A, i) for i in range(1, 5)))
            assert max_planner_observed == 2

            # 2. 三个用户同时各发起2个并发with_images=False规划请求（共6个），受global_max_parallel=3限制
            max_planner_observed = 0
            await asyncio.gather(
                _call_plan(client, USER_A, 11),
                _call_plan(client, USER_A, 12),
                _call_plan(client, USER_B, 13),
                _call_plan(client, USER_B, 14),
                _call_plan(client, user_c, 15),
                _call_plan(client, user_c, 16),
            )
            assert max_planner_observed == 3

    asyncio.run(_run())


# 校验LibreChat用户目录中的头像文件（avatar-*、agent-*-avatar-*）与非UUID前缀文件不会被列出或成为@latest
def test_avatar_and_non_uuid_files_excluded(studio_env: StudioSettings) -> None:
    user_lc = studio_env.librechat_images_dir / USER_A
    user_lc.mkdir(parents=True, mode=0o700)

    valid_upload_id = "12345678-1234-1234-1234-123456789abc"
    valid_file = user_lc / f"{valid_upload_id}__design.png"
    valid_file.write_bytes(_make_image_bytes(100, 100, fmt="PNG"))
    os.utime(valid_file, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000))

    # 写入更新时间戳的头像与非UUID文件，验证不会抢占@latest或进入列表
    for bogus_name in (
        "avatar-1700000999.png",
        "agent-xyz123-avatar-1700000999.png",
        "customprefix__photo.jpg",
        "not-a-uuid-at-all.webp",
    ):
        bogus_file = user_lc / bogus_name
        bogus_file.write_bytes(_make_image_bytes(64, 64, fmt="PNG"))
        os.utime(bogus_file, ns=(1_700_000_999_000_000_000, 1_700_000_999_000_000_000))

    listed = list_user_images(studio_env, USER_A, limit=20)
    assert [item["id"] for item in listed] == [valid_upload_id]

    latest = resolve_image(studio_env, USER_A, "@latest")
    assert latest.id == valid_upload_id

    for bad_lookup in ("avatar", "agent", "customprefix", "avatar-1700000999"):
        with pytest.raises(ValueError, match="not found"):
            resolve_image(studio_env, USER_A, bad_lookup)


# 校验石材名称仅在提供匹配目录材质参考图时才替换为图N所示石材，无参考或无关参考时保留原词
def test_stone_neutralization_only_for_matching_references(
    studio_env: StudioSettings, tmp_path: Path
) -> None:
    rules = load_rules()
    unrelated_ref = tmp_path / "wooden_chair.jpg"
    unrelated_ref.write_bytes(_make_image_bytes(32, 32))
    matched_ref = tmp_path / "vangogh_sample.jpg"
    matched_ref.write_bytes(_make_image_bytes(32, 32))

    # 1. 无参考图生图：保留客户原文“敦煌金”，不得凭空生成“图1所示石材”
    no_ref_prompt, no_ref_paths = build_generate_prompt("用敦煌金设计供桌", [], rules)
    assert no_ref_paths == []
    assert "敦煌金" in no_ref_prompt
    assert "所示石材" not in no_ref_prompt

    # 2. 无额外参考图改图：保留客户原文“敦煌金”，不得指向底图“图1所示石材”
    no_ref_edit, no_ref_edit_paths = build_edit_prompt("把台面换成敦煌金", [], rules)
    assert no_ref_edit_paths == []
    assert "敦煌金" in no_ref_edit
    assert "所示石材" not in no_ref_edit

    # 3. 含无关参考图与另一款匹配石材参考图：只替换匹配的梵高皇/敦煌壁，保留未匹配的敦煌金
    mixed_prompt, _ = build_generate_prompt(
        "台面用敦煌金，背板用梵高皇",
        [
            {"id": "u1", "path": unrelated_ref, "use": "造型", "note": "明式束腰造型"},
            {"id": "u2", "path": matched_ref, "use": "材质", "note": "梵高皇样板"},
        ],
        rules,
    )
    assert "敦煌金" in mixed_prompt
    assert "图1所示石材" not in mixed_prompt
    assert "梵高皇" not in mixed_prompt
    assert "图2所示石材" in mixed_prompt


# 校验EXIF Orientation 6与8的4000x3000底图在edit_image中旋转像素尺寸与请求输出比例一致（竖幅）
def test_edit_image_exif_orientation_dimensions(studio_env: StudioSettings) -> None:
    user_lc = studio_env.librechat_images_dir / USER_A
    user_lc.mkdir(parents=True, mode=0o700)

    id_orient_6 = "66666666-1111-2222-3333-444444444444"
    id_orient_8 = "88888888-1111-2222-3333-444444444444"

    (user_lc / f"{id_orient_6}__photo6.jpg").write_bytes(
        _make_image_bytes(4000, 3000, fmt="JPEG", exif_orientation=6)
    )
    (user_lc / f"{id_orient_8}__photo8.jpg").write_bytes(
        _make_image_bytes(4000, 3000, fmt="JPEG", exif_orientation=8)
    )

    # 验证store解析与列表尺寸均为EXIF转置后的竖幅3000x4000
    for fid in (id_orient_6, id_orient_8):
        resolved = resolve_image(studio_env, USER_A, fid)
        assert (resolved.width, resolved.height) == (3000, 4000)

    captured_requests: list[dict[str, Any]] = []
    out_jpg = _make_image_bytes(1776, 2368, fmt="JPEG")

    async def _ark_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(out_jpg).decode("ascii")}]},
        )

    app = create_app(
        settings=studio_env,
        rules=load_rules(),
        ark_transport=httpx.MockTransport(_ark_handler),
    )

    async def _run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://studio:8000"
        ) as client:
            for idx, fid in enumerate((id_orient_6, id_orient_8), start=1):
                res = await client.post(
                    "/mcp",
                    headers={
                        "X-Studio-Token": VALID_TOKEN,
                        "X-LibreChat-User-ID": USER_A,
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": idx,
                        "method": "tools/call",
                        "params": {
                            "name": "edit_image",
                            "arguments": {
                                "base_id": fid,
                                "instruction": "微调边角倒角",
                                "size": "2K",
                            },
                        },
                    },
                )
                assert res.status_code == 200
                assert res.json()["result"]["isError"] is False
                req_body = captured_requests[-1]
                assert req_body["size"] == "1776x2368"
                sent_b64 = req_body["image"].split(",", 1)[1]
                with Image.open(io.BytesIO(base64.b64decode(sent_b64))) as sent_im:
                    assert sent_im.width < sent_im.height
                    assert sent_im.size == (1536, 2048)

    asyncio.run(_run())


# 校验概念方案渲染时即使规划器的image_prompt遗漏了必选副件与空间布局，仍会附带原始brief作为design_constraints发送给生图接口
def test_concept_rendering_includes_binding_design_constraints(
    studio_env: StudioSettings,
) -> None:
    ark_prompts: list[str] = []
    small_jpg = _make_image_bytes(64, 48, fmt="JPEG")

    async def _ark_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        ark_prompts.append(body["prompt"])
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(small_jpg).decode("ascii")}]},
        )

    # 模拟规划器在image_prompt中遗漏了“配套三足香炉”和“禅堂全景布局”，只写了孤立供桌
    async def _omitting_gemini_handler(request: httpx.Request) -> httpx.Response:
        concept = {
            "title": "极简石案",
            "positioning": "主供桌定位",
            "description": "石质供桌配三足香炉",
            "materials": "青石（待确认）",
            "key_details": "直腿内收",
            "image_prompt": "An isolated stone altar table in a neutral studio.",
        }
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {"concepts": [concept]}, ensure_ascii=False
                                    )
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
        ark_transport=httpx.MockTransport(_ark_handler),
        gemini_transport=httpx.MockTransport(_omitting_gemini_handler),
    )

    brief_text = (
        "设计禅堂中轴线上的石材供桌，桌面正中必须放置配套三足香炉，保留完整禅堂空间布局"
    )

    async def _run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://studio:8000"
        ) as client:
            res = await client.post(
                "/mcp",
                headers={"X-Studio-Token": VALID_TOKEN, "X-LibreChat-User-ID": USER_A},
                json={
                    "jsonrpc": "2.0",
                    "id": 30,
                    "method": "tools/call",
                    "params": {
                        "name": "design_concepts",
                        "arguments": {
                            "brief": brief_text,
                            "count": 1,
                            "with_images": True,
                        },
                    },
                },
            )
            assert res.status_code == 200
            assert res.json()["result"]["isError"] is False
            assert len(ark_prompts) == 1
            sent_prompt = ark_prompts[0]
            assert "design_constraints" in sent_prompt
            assert (
                json.dumps({"design_constraints": brief_text}, ensure_ascii=False)
                in sent_prompt
            )
            assert "配套三足香炉" in sent_prompt
            assert "完整禅堂空间布局" in sent_prompt

    asyncio.run(_run())


# 校验count=1/2/3时组装的systemInstruction与user提示词中无冲突数量指令，且仅引用responseSchema定义的6个字段
def test_planner_instructions_consistent_for_counts_1_to_3(
    studio_env: StudioSettings,
) -> None:
    rules = load_rules()
    forbidden_count_tokens = ("exactly 4", "3–4", "3-4", "4 个", "4个", "{count}")
    forbidden_legacy_fields = (
        '"summary"',
        "material_notes",
        '"silhouette"',
        "key_features",
        '"craftsmanship"',
        '"styling"',
        "render_prompt",
    )
    expected_schema_fields = (
        "title",
        "positioning",
        "description",
        "materials",
        "key_details",
        "image_prompt",
    )

    async def _run() -> None:
        for count in (1, 2, 3):
            sys_no_ref = build_planner_system_prompt(
                rules, has_references=False, count=count
            )
            sys_with_ref = build_planner_system_prompt(
                rules, has_references=True, count=count
            )
            for sys_text in (sys_no_ref, sys_with_ref):
                assert f"围绕当前品类和目标提出 {count} 个不同" in sys_text
                assert f"concepts长度严格为 {count}" in sys_text
                assert f"在全部 {count} 个方向中均须保留" in sys_text
                for bad_tok in forbidden_count_tokens:
                    assert bad_tok not in sys_text
                for legacy_field in forbidden_legacy_fields:
                    assert legacy_field not in sys_text
                for valid_field in expected_schema_fields:
                    assert valid_field in sys_text
            assert f"完成既定 JSON 格式的 {count} 个完整设计方向" in sys_no_ref

            captured: list[dict[str, Any]] = []

            async def _gemini_handler(
                request: httpx.Request,
                target_count: int = count,
                bucket: list[dict[str, Any]] = captured,
            ) -> httpx.Response:
                body = json.loads(request.content.decode("utf-8"))
                bucket.append(body)
                concepts = [
                    {
                        "title": f"方案{i}",
                        "positioning": "禅意定位",
                        "description": "造型说明",
                        "materials": "石材（待确认）",
                        "key_details": "工艺收口",
                        "image_prompt": f"Concept {i} render prompt.",
                    }
                    for i in range(1, target_count + 1)
                ]
                return httpx.Response(
                    200,
                    json={
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {
                                            "text": json.dumps(
                                                {"concepts": concepts},
                                                ensure_ascii=False,
                                            )
                                        }
                                    ]
                                }
                            }
                        ]
                    },
                )

            result = await plan_concepts(
                studio_env,
                rules,
                brief="设计寺庙供桌",
                count=count,
                transport=httpx.MockTransport(_gemini_handler),
            )
            assert len(result) == count
            sent_body = captured[0]
            sent_sys = sent_body["systemInstruction"]["parts"][0]["text"]
            sent_user = sent_body["contents"][0]["parts"][0]["text"]
            schema_props = sent_body["generationConfig"]["responseSchema"][
                "properties"
            ]["concepts"]["items"]["properties"]
            assert set(schema_props.keys()) == set(expected_schema_fields)
            for text_part in (sent_sys, sent_user):
                for bad_tok in forbidden_count_tokens:
                    assert bad_tok not in text_part
                for legacy_field in forbidden_legacy_fields:
                    assert legacy_field not in text_part

    asyncio.run(_run())


# 校验StudioSettings拒绝过短、过长或非十六进制的shared_token与file_signing_key，仅接受64位十六进制字符串
def test_shared_token_64_hex_validation() -> None:
    valid_hex = "0123456789abcdefABCDEF0123456789abcdefABCDEF0123456789abcdefABCD"
    s = StudioSettings(
        shared_token=valid_hex,
        file_signing_key=valid_hex,
        gemini_api_key="test-gemini-key",
        ark_api_key="test-ark-key",
    )
    assert s.shared_token == valid_hex
    assert s.file_signing_key == valid_hex

    for invalid_token in (
        "x",
        "a" * 63,
        "a" * 65,
        "g" * 64,
        "a" * 32 + "-" + "a" * 31,
        "placeholder-token",
    ):
        with pytest.raises(ValueError, match="64-character hexadecimal"):
            StudioSettings(
                shared_token=invalid_token,
                file_signing_key=valid_hex,
                gemini_api_key="test-gemini-key",
                ark_api_key="test-ark-key",
            )
        with pytest.raises(ValueError, match="64-character hexadecimal"):
            StudioSettings(
                shared_token=valid_hex,
                file_signing_key=invalid_token,
                gemini_api_key="test-gemini-key",
                ark_api_key="test-ark-key",
            )


# 校验进入应用lifespan共享StreamableHTTPSessionManager时，User A与User B并发MCP请求上下文与资产隔离不串号
def test_mcp_lifespan_shared_session_manager_concurrent_user_isolation(
    studio_env: StudioSettings,
) -> None:
    user_a_lc = studio_env.librechat_images_dir / USER_A
    user_b_lc = studio_env.librechat_images_dir / USER_B
    user_a_lc.mkdir(parents=True, mode=0o700)
    user_b_lc.mkdir(parents=True, mode=0o700)

    upload_a = "aaaaaaaa-1111-2222-3333-444444444444"
    upload_b = "bbbbbbbb-1111-2222-3333-444444444444"
    (user_a_lc / f"{upload_a}__a.jpg").write_bytes(
        _make_image_bytes(80, 60, fmt="JPEG")
    )
    (user_b_lc / f"{upload_b}__b.jpg").write_bytes(
        _make_image_bytes(100, 100, fmt="JPEG")
    )

    both_in_flight = asyncio.Event()
    in_flight_count = 0
    small_jpg = _make_image_bytes(64, 64, fmt="JPEG")

    async def _barrier_ark_handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight_count
        in_flight_count += 1
        if in_flight_count >= 2:
            both_in_flight.set()
        await asyncio.wait_for(both_in_flight.wait(), timeout=5.0)
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(small_jpg).decode("ascii")}]},
        )

    app = create_app(
        settings=studio_env,
        rules=load_rules(),
        ark_transport=httpx.MockTransport(_barrier_ark_handler),
    )

    async def _run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=transport, base_url="http://studio:8000"
            ) as client,
        ):
            shared_mgr = app.state.session_manager
            assert shared_mgr is not None
            assert getattr(shared_mgr, "_task_group", None) is not None

            # 并发执行User A与User B的generate_image，且各自只允许解析自身名下的参考图
            req_a = client.post(
                "/mcp",
                headers={
                    "X-Studio-Token": VALID_TOKEN,
                    "X-LibreChat-User-ID": USER_A,
                    "X-LibreChat-Conversation-ID": "conv-user-a-001",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 101,
                    "method": "tools/call",
                    "params": {
                        "name": "generate_image",
                        "arguments": {
                            "prompt": "用户A专属供案",
                            "references": [{"id": upload_a, "use": "造型"}],
                        },
                    },
                },
            )
            req_b = client.post(
                "/mcp",
                headers={
                    "X-Studio-Token": VALID_TOKEN,
                    "X-LibreChat-User-ID": USER_B,
                    "X-LibreChat-Conversation-ID": "conv-user-b-002",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 102,
                    "method": "tools/call",
                    "params": {
                        "name": "generate_image",
                        "arguments": {
                            "prompt": "用户B专属香炉",
                            "references": [{"id": upload_b, "use": "造型"}],
                        },
                    },
                },
            )

            res_a, res_b = await asyncio.gather(req_a, req_b)
            assert res_a.status_code == 200
            assert res_b.status_code == 200
            body_a = res_a.json()["result"]
            body_b = res_b.json()["result"]
            assert body_a["isError"] is False
            assert body_b["isError"] is False

            img_id_a = json.loads(body_a["content"][0]["text"])["id"]
            img_id_b = json.loads(body_b["content"][0]["text"])["id"]
            assert img_id_a != img_id_b

            # 验证生成结果分别落盘在各自用户的目录且互不可见
            assert (studio_env.data_dir / "users" / USER_A / "jobs" / img_id_a).is_dir()
            assert not (
                studio_env.data_dir / "users" / USER_A / "jobs" / img_id_b
            ).exists()
            assert (studio_env.data_dir / "users" / USER_B / "jobs" / img_id_b).is_dir()
            assert not (
                studio_env.data_dir / "users" / USER_B / "jobs" / img_id_a
            ).exists()

            # 并发调用list_images验证各自只返回自己的上传图与生成图
            list_a, list_b = await asyncio.gather(
                client.post(
                    "/mcp",
                    headers={
                        "X-Studio-Token": VALID_TOKEN,
                        "X-LibreChat-User-ID": USER_A,
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 103,
                        "method": "tools/call",
                        "params": {"name": "list_images", "arguments": {"limit": 10}},
                    },
                ),
                client.post(
                    "/mcp",
                    headers={
                        "X-Studio-Token": VALID_TOKEN,
                        "X-LibreChat-User-ID": USER_B,
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 104,
                        "method": "tools/call",
                        "params": {"name": "list_images", "arguments": {"limit": 10}},
                    },
                ),
            )
            ids_a = {
                item["id"]
                for item in json.loads(list_a.json()["result"]["content"][0]["text"])[
                    "images"
                ]
            }
            ids_b = {
                item["id"]
                for item in json.loads(list_b.json()["result"]["content"][0]["text"])[
                    "images"
                ]
            }
            assert ids_a == {upload_a, img_id_a}
            assert ids_b == {upload_b, img_id_b}

    asyncio.run(_run())


# 校验search_library关键词排序、石材别名展开、类型与角色筛选、质量与审核状态过滤及thumb512返回
def test_search_library_ranking_filters_aliases_and_flagged_handling(
    studio_env: StudioSettings,
) -> None:
    kb_index = load_kb_index(studio_env.kb_root)
    assert len(kb_index.records) == 9

    # 1. 石材别名展开：查询“天川金”命中“敦煌金”关联记录，标题与材质均匹配的样板排在前面
    alias_hits = kb_index.search(query="天川金", limit=6, include_flagged=False)
    alias_ids = [rec.id for rec in alias_hits]
    assert alias_ids[:2] == ["1111111111111111", "1111111111111112"]
    assert "5555555555555551" in alias_ids
    assert "8888888888888881" not in alias_ids

    # 2. 筛选library_type与role
    furniture_hits = kb_index.search(
        query="天川金",
        library_type="家具木作",
        role="造型",
        limit=6,
        include_flagged=False,
    )
    assert [rec.id for rec in furniture_hits] == ["5555555555555551"]

    # 3. 默认排除ai_generated / third_party_watermark / low_res / rejected，开启include_flagged后返回并带齐标记
    carving_default = kb_index.search(
        query="莲花纹",
        library_type="石刻纹样",
        include_flagged=False,
    )
    assert carving_default == []

    carving_flagged = kb_index.search(
        query="莲花纹",
        library_type="石刻纹样",
        include_flagged=True,
    )
    flagged_by_id = {rec.id: rec for rec in carving_flagged}
    assert set(flagged_by_id.keys()) == {
        "6666666666666661",
        "7777777777777771",
        "8888888888888881",
    }
    assert list(flagged_by_id["6666666666666661"].exclusion_flags) == ["ai_generated"]
    assert list(flagged_by_id["7777777777777771"].exclusion_flags) == [
        "third_party_watermark",
        "low_res",
    ]
    assert list(flagged_by_id["8888888888888881"].exclusion_flags) == ["rejected"]

    # 4. 通过MCP端到端调用search_library，验证kb_前缀、<=120字摘要、质量/版权字段及thumb512图像块
    app = create_app(settings=studio_env, rules=load_rules())

    async def _run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://studio:8000"
        ) as client:
            res = await client.post(
                "/mcp",
                headers={"X-Studio-Token": VALID_TOKEN, "X-LibreChat-User-ID": USER_A},
                json={
                    "jsonrpc": "2.0",
                    "id": 201,
                    "method": "tools/call",
                    "params": {
                        "name": "search_library",
                        "arguments": {
                            "query": "天川金 供桌",
                            "limit": 3,
                            "include_flagged": False,
                        },
                    },
                },
            )
            assert res.status_code == 200
            result = res.json()["result"]
            assert result.get("isError") is False
            blocks = result["content"]
            payload = json.loads(blocks[0]["text"])
            assert payload["count"] == 2
            assert len(blocks) == 1 + 2
            # 确认标题直接命中“供桌”的记录排在首位，且摘要被截断至<=120字符
            first_item = payload["results"][0]
            assert first_item["id"] == "kb_5555555555555551"
            for item in payload["results"]:
                assert item["id"].startswith("kb_")
                assert len(item["summary"]) <= 120
                assert "licence" in item
                assert "review_status" in item
                assert "flags" in item
                assert set(item["flags"].keys()) >= {
                    "ai_generated",
                    "third_party_watermark",
                    "low_res",
                    "rejected",
                }

            long_summary_item = next(
                it for it in payload["results"] if it["id"] == "kb_1111111111111111"
            )
            assert len(long_summary_item["summary"]) == 120
            assert long_summary_item["summary"].endswith("…")

            for img_block in blocks[1:]:
                assert img_block["type"] == "image"
                assert img_block["mimeType"] == "image/webp"
                with Image.open(
                    io.BytesIO(base64.b64decode(img_block["data"]))
                ) as thumb_im:
                    assert thumb_im.size == (64, 48)

    asyncio.run(_run())


# 校验kb_<id>解析、路径穿越防护、石材样图中性化联动以及显式传入带标记记录时的警告提示
def test_kb_id_resolution_isolation_and_flagged_warnings(
    studio_env: StudioSettings, tmp_path: Path
) -> None:
    kb_index = load_kb_index(studio_env.kb_root)

    # 1. 正常解析kb_<16位hex>至/kb/assets/ref2048/<id>.jpg
    resolved = resolve_image(
        studio_env, USER_A, "kb_1111111111111111", kb_index=kb_index
    )
    assert resolved.id == "kb_1111111111111111"
    assert resolved.kind == "library"
    assert (
        resolved.path
        == (
            studio_env.kb_root / "assets" / "ref2048" / "1111111111111111.jpg"
        ).resolve()
    )
    assert (resolved.width, resolved.height) == (160, 120)
    assert resolved.warning is False
    assert resolved.warning_reasons == ()

    # 2. 拒绝非法格式、路径穿越、不存在的ID以及逃逸符号链接
    for bad_kb_id in (
        "kb_../secret",
        "kb_../../etc/passwd",
        "kb_1111",
        "kb_111111111111111g",
        "kb_1111111111111111/../../x",
        "kb_9999999999999999",
    ):
        with pytest.raises(ValueError):
            resolve_image(studio_env, USER_A, bad_kb_id, kb_index=kb_index)

    outside_img = tmp_path / "outside.jpg"
    outside_img.write_bytes(_make_image_bytes(32, 32))
    orig_ref = studio_env.kb_root / "assets" / "ref2048" / "1111111111111112.jpg"
    orig_bytes = orig_ref.read_bytes()
    orig_ref.unlink()
    orig_ref.symlink_to(outside_img)
    try:
        with pytest.raises(ValueError):
            resolve_image(studio_env, USER_A, "kb_1111111111111112", kb_index=kb_index)
    finally:
        orig_ref.unlink()
        orig_ref.write_bytes(orig_bytes)

    # 3. 显式传入带质量或审核标记的kb_<id>允许解析，但生成明确警告并在工具结果中返回
    flagged_ai = resolve_image(
        studio_env, USER_A, "kb_6666666666666661", kb_index=kb_index
    )
    assert flagged_ai.warning is True
    assert "ai_generated" in flagged_ai.warning_reasons

    flagged_wm = resolve_image(
        studio_env, USER_A, "kb_7777777777777771", kb_index=kb_index
    )
    assert flagged_wm.warning is True
    assert "third_party_watermark" in flagged_wm.warning_reasons
    assert "low_res" in flagged_wm.warning_reasons

    flagged_rej = resolve_image(
        studio_env, USER_A, "kb_8888888888888881", kb_index=kb_index
    )
    assert flagged_rej.warning is True
    assert "rejected" in flagged_rej.warning_reasons

    # 4. 在generate_image与edit_image中使用kb_<id>作为材质参考或底图，验证石材名通过catalog资产ID中性化且返回flagged警告
    ark_requests: list[dict[str, Any]] = []
    out_jpg = _make_image_bytes(160, 120, fmt="JPEG")

    async def _ark_handler(request: httpx.Request) -> httpx.Response:
        ark_requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(out_jpg).decode("ascii")}]},
        )

    app = create_app(
        settings=studio_env,
        rules=load_rules(),
        ark_transport=httpx.MockTransport(_ark_handler),
    )

    async def _run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://studio:8000"
        ) as client:
            # 传入kb_1111111111111111（不写note）与带标记的kb_6666666666666661
            gen_res = await client.post(
                "/mcp",
                headers={"X-Studio-Token": VALID_TOKEN, "X-LibreChat-User-ID": USER_A},
                json={
                    "jsonrpc": "2.0",
                    "id": 202,
                    "method": "tools/call",
                    "params": {
                        "name": "generate_image",
                        "arguments": {
                            "prompt": "采用敦煌金台面配莲花纹浮雕制作供桌",
                            "references": [
                                {"id": "kb_1111111111111111", "use": "材质"},
                                {"id": "kb_6666666666666661", "use": "纹样"},
                            ],
                        },
                    },
                },
            )
            assert gen_res.status_code == 200
            gen_result = gen_res.json()["result"]
            assert gen_result.get("isError") is False
            meta = json.loads(gen_result["content"][0]["text"])
            assert "warnings" in meta
            assert any("kb_6666666666666661" in w for w in meta["warnings"])
            # 即使未提供note，通过kb_1111111111111111匹配catalog中的敦煌金后仍将“敦煌金”中性化为“图1所示石材”
            assert "敦煌金" not in ark_requests[-1]["prompt"]
            assert "图1所示石材" in ark_requests[-1]["prompt"]

            # 使用kb_5555555555555551作为edit_image的base_id
            edit_res = await client.post(
                "/mcp",
                headers={"X-Studio-Token": VALID_TOKEN, "X-LibreChat-User-ID": USER_A},
                json={
                    "jsonrpc": "2.0",
                    "id": 203,
                    "method": "tools/call",
                    "params": {
                        "name": "edit_image",
                        "arguments": {
                            "base_id": "kb_5555555555555551",
                            "instruction": "将供桌台面替换为梵高皇",
                            "references": [
                                {"id": "kb_2222222222222221", "use": "材质"}
                            ],
                        },
                    },
                },
            )
            assert edit_res.status_code == 200
            assert edit_res.json()["result"].get("isError") is False
            assert "梵高皇" not in ark_requests[-1]["prompt"]
            assert "图2所示石材" in ark_requests[-1]["prompt"]

    asyncio.run(_run())


# 校验compare_materials同一底图独立渲染、目录样图引用、Mac指令模板、部分失败保留成功结果与未知石材报错
def test_compare_materials_payloads_partial_failure_and_unknown_material(
    studio_env: StudioSettings,
) -> None:
    user_lc = studio_env.librechat_images_dir / USER_A
    user_lc.mkdir(parents=True, mode=0o700)
    base_upload_id = "cccccccc-1111-2222-3333-444444444444"
    (user_lc / f"{base_upload_id}__altar_base.jpg").write_bytes(
        _make_image_bytes(160, 120, color=(90, 80, 70), fmt="JPEG")
    )

    rules = load_rules()
    allowed_roots = (
        studio_env.librechat_images_dir,
        studio_env.data_dir,
        studio_env.kb_root,
    )
    expected_base_uri = normalize_reference(
        user_lc / f"{base_upload_id}__altar_base.jpg", allowed_roots
    )
    expected_dunhuang_uris = [
        normalize_reference(
            studio_env.kb_root / "assets" / "ref2048" / "1111111111111111.jpg",
            allowed_roots,
        ),
        normalize_reference(
            studio_env.kb_root / "assets" / "ref2048" / "1111111111111112.jpg",
            allowed_roots,
        ),
    ]
    expected_vangogh_uris = [
        normalize_reference(
            studio_env.kb_root / "assets" / "ref2048" / "2222222222222221.jpg",
            allowed_roots,
        )
    ]

    ark_requests: list[dict[str, Any]] = []
    out_jpg = _make_image_bytes(160, 120, fmt="JPEG")

    async def _ark_ok_handler(request: httpx.Request) -> httpx.Response:
        ark_requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(out_jpg).decode("ascii")}]},
        )

    app = create_app(
        settings=studio_env,
        rules=rules,
        ark_transport=httpx.MockTransport(_ark_ok_handler),
    )

    async def _run_ok_and_unknown() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://studio:8000"
        ) as client:
            # 1. 未知石材名称立即报错并列出已知石材名称，不发起任何付费调用
            unknown_res = await client.post(
                "/mcp",
                headers={"X-Studio-Token": VALID_TOKEN, "X-LibreChat-User-ID": USER_A},
                json={
                    "jsonrpc": "2.0",
                    "id": 301,
                    "method": "tools/call",
                    "params": {
                        "name": "compare_materials",
                        "arguments": {
                            "base_id": base_upload_id,
                            "target": "供桌台面",
                            "materials": ["敦煌金", "未知火星石"],
                        },
                    },
                },
            )
            assert unknown_res.status_code == 200
            unknown_result = unknown_res.json()["result"]
            assert unknown_result.get("isError") is True
            err_text = unknown_result["content"][0]["text"]
            assert "未知火星石" in err_text
            for known_name in ("敦煌金", "敦煌壁", "金镶玉", "蓝金灰"):
                assert known_name in err_text
            assert len(ark_requests) == 0

            # 2. 对比“天川金”（别名->敦煌金，2张样图）与“梵高皇”（别名->敦煌壁，1张样图），验证同一底图独立渲染且绝不链式串改
            cmp_res = await client.post(
                "/mcp",
                headers={"X-Studio-Token": VALID_TOKEN, "X-LibreChat-User-ID": USER_A},
                json={
                    "jsonrpc": "2.0",
                    "id": 302,
                    "method": "tools/call",
                    "params": {
                        "name": "compare_materials",
                        "arguments": {
                            "base_id": base_upload_id,
                            "target": "供桌台面",
                            "materials": ["天川金", "梵高皇"],
                        },
                    },
                },
            )
            assert cmp_res.status_code == 200
            cmp_result = cmp_res.json()["result"]
            assert cmp_result.get("isError") is False
            blocks = cmp_result["content"]
            assert len(blocks) == 1 + 2
            payload = json.loads(blocks[0]["text"])
            assert payload["base_id"] == base_upload_id
            assert payload["target"] == "供桌台面"
            assert payload["count"] == 2
            assert [c["material"] for c in payload["comparisons"]] == [
                "敦煌金",
                "敦煌壁",
            ]
            assert [c["requested_material"] for c in payload["comparisons"]] == [
                "天川金",
                "梵高皇",
            ]
            assert payload["comparisons"][0]["reference_ids"] == [
                "kb_1111111111111111",
                "kb_1111111111111112",
            ]
            assert payload["comparisons"][1]["reference_ids"] == ["kb_2222222222222221"]

            assert len(ark_requests) == 2
            # 两个变体的首张图（图1底图）完全一致，均来自原始base_id而非前一次生成结果
            assert ark_requests[0]["image"][0] == expected_base_uri
            assert ark_requests[1]["image"][0] == expected_base_uri
            assert ark_requests[0]["image"][1:] == expected_dunhuang_uris
            assert ark_requests[1]["image"][1:] == expected_vangogh_uris
            for req in ark_requests:
                assert (
                    "将图1的供桌台面替换为本次材质样图所示石材，其他部分不变。"
                    in req["prompt"]
                )
                assert rules.edit_rules in req["prompt"]
                assert "敦煌金" not in req["prompt"]
                assert "天川金" not in req["prompt"]
                assert "梵高皇" not in req["prompt"]
                assert "敦煌壁" not in req["prompt"]

    asyncio.run(_run_ok_and_unknown())

    # 3. 验证部分失败时保留成功生成的变体图像并返回告警
    call_counter = 0

    async def _partial_fail_ark_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_counter
        call_counter += 1
        if call_counter == 2:
            return httpx.Response(400, content=b"bad request on second variant")
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(out_jpg).decode("ascii")}]},
        )

    app_partial = create_app(
        settings=studio_env,
        rules=rules,
        ark_transport=httpx.MockTransport(_partial_fail_ark_handler),
    )

    async def _run_partial() -> None:
        transport = httpx.ASGITransport(app=app_partial)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://studio:8000"
        ) as client:
            res = await client.post(
                "/mcp",
                headers={"X-Studio-Token": VALID_TOKEN, "X-LibreChat-User-ID": USER_A},
                json={
                    "jsonrpc": "2.0",
                    "id": 303,
                    "method": "tools/call",
                    "params": {
                        "name": "compare_materials",
                        "arguments": {
                            "base_id": base_upload_id,
                            "target": "供桌台面",
                            "materials": ["敦煌金", "梵高皇", "金镶玉"],
                        },
                    },
                },
            )
            assert res.status_code == 200
            result = res.json()["result"]
            assert result.get("isError") is False
            blocks = result["content"]
            # 3个石材中2个成功、1个失败 -> 1个JSON文本块 + 2个图像块
            assert len(blocks) == 1 + 2
            payload = json.loads(blocks[0]["text"])
            assert payload["comparisons"][0]["image_id"].startswith("img_")
            assert "image_id" not in payload["comparisons"][1]
            assert "error" in payload["comparisons"][1]
            assert payload["comparisons"][2]["image_id"].startswith("img_")
            assert "warnings" in payload
            assert len(payload["warnings"]) == 1

    asyncio.run(_run_partial())


# 构造最小合法GLB二进制字节或包含该GLB的ZIP压缩包字节
def _make_glb_bytes(extra_payload: bytes = b"TEST_MESH_DATA_0123456789ABCDEF") -> bytes:
    header = b"glTF" + (2).to_bytes(4, "little")
    total_len = (12 + len(extra_payload)).to_bytes(4, "little")
    return header + total_len + extra_payload


# 将GLB字节封装为ZIP压缩包字节（模拟方舟3D生成任务返回的压缩包结构）
def _make_zip_with_glb(glb_bytes: bytes, entry_name: str = "output/mesh.glb") -> bytes:
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(entry_name, glb_bytes)
        zf.writestr("output/preview.txt", b"preview metadata")
    return buf.getvalue()


# 校验Seed3D请求构造、响应解析与GLB/ZIP字节提取适配器
def test_seed3d_adapters_and_glb_extraction() -> None:
    payload_std, sub_std, faces_std = build_seed3d_create_payload(
        model="doubao-seed3d-2-0-260328",
        image_data_uri="data:image/jpeg;base64,AAAA",
        detail="standard",
    )
    assert sub_std == "low"
    assert faces_std == 100_000
    assert payload_std == {
        "model": "doubao-seed3d-2-0-260328",
        "content": [
            {
                "type": "text",
                "text": "--subdivisionlevel low --fileformat glb",
            },
            {
                "type": "image_url",
                "image_url": {"url": "data:image/jpeg;base64,AAAA"},
            },
        ],
    }

    payload_high, sub_high, faces_high = build_seed3d_create_payload(
        model="doubao-seed3d-2-0-260328",
        image_data_uri="data:image/jpeg;base64,BBBB",
        detail="high",
    )
    assert sub_high == "medium"
    assert faces_high == 500_000
    assert (
        payload_high["content"][0]["text"]
        == "--subdivisionlevel medium --fileformat glb"
    )

    with pytest.raises(ValueError, match="detail must be 'standard' or 'high'"):
        build_seed3d_create_payload(
            model="doubao-seed3d-2-0-260328",
            image_data_uri="data:image/jpeg;base64,AAAA",
            detail="ultra",
        )

    assert (
        parse_seed3d_create_response({"id": "cgt-20261016-001"}) == "cgt-20261016-001"
    )
    with pytest.raises(RuntimeError, match="missing required task id field"):
        parse_seed3d_create_response({"id": ""})

    q_running = parse_seed3d_query_response({"id": "cgt-1", "status": "running"})
    assert q_running == {"status": "running"}

    q_ok = parse_seed3d_query_response(
        {
            "id": "cgt-1",
            "status": "succeeded",
            "content": {"file_url": "https://cdn.example.test/out.zip"},
        }
    )
    assert q_ok == {
        "status": "succeeded",
        "file_url": "https://cdn.example.test/out.zip",
    }

    q_fail = parse_seed3d_query_response(
        {
            "id": "cgt-1",
            "status": "failed",
            "error": {"code": "InvalidImage", "message": "unusable subject"},
        }
    )
    assert q_fail["status"] == "failed"
    assert q_fail["error_code"] == "InvalidImage"

    with pytest.raises(RuntimeError, match="missing valid content.file_url"):
        parse_seed3d_query_response(
            {"id": "cgt-1", "status": "succeeded", "content": {}}
        )

    raw_glb = _make_glb_bytes()
    assert extract_glb_bytes(raw_glb) == raw_glb
    zipped_glb = _make_zip_with_glb(raw_glb)
    assert extract_glb_bytes(zipped_glb) == raw_glb
    with pytest.raises(
        RuntimeError,
        match="neither a valid GLB binary nor a ZIP archive",
    ):
        extract_glb_bytes(b"not-a-glb-or-zip")


# 校验create_3d_model端到端成功流程（标准100k与高精度500k、直出GLB与ZIP内嵌GLB）、MCP UI资源与无密钥/厂商泄露
def test_create_3d_model_success_ui_resource_and_no_secret_leak(
    studio_env: StudioSettings,
) -> None:
    user_lc = studio_env.librechat_images_dir / USER_A
    user_lc.mkdir(parents=True, mode=0o700)
    upload_id = "dddddddd-1111-2222-3333-444444444444"
    (user_lc / f"{upload_id}__censer.jpg").write_bytes(
        _make_image_bytes(180, 140, color=(150, 120, 90), fmt="JPEG")
    )

    raw_glb = _make_glb_bytes(b"CENSER_GLB_BINARY_PAYLOAD_BLOCKS_0123456789")
    zipped_glb = _make_zip_with_glb(raw_glb)
    create_payloads: list[dict[str, Any]] = []
    poll_counts: dict[str, int] = {}

    async def _seed3d_handler(request: httpx.Request) -> httpx.Response:
        if (
            request.method == "POST"
            and request.url.path == "/api/v3/contents/generations/tasks"
        ):
            body = json.loads(request.content.decode("utf-8"))
            create_payloads.append(body)
            task_id = f"cgt-task-{len(create_payloads)}"
            poll_counts[task_id] = 0
            return httpx.Response(200, json={"id": task_id})

        if request.method == "GET" and request.url.path.startswith(
            "/api/v3/contents/generations/tasks/"
        ):
            task_id = request.url.path.rsplit("/", 1)[-1]
            poll_counts[task_id] = poll_counts.get(task_id, 0) + 1
            if poll_counts[task_id] == 1:
                return httpx.Response(200, json={"id": task_id, "status": "queued"})
            if poll_counts[task_id] == 2:
                return httpx.Response(200, json={"id": task_id, "status": "running"})
            ext = "zip" if task_id.endswith("-1") else "glb"
            return httpx.Response(
                200,
                json={
                    "id": task_id,
                    "status": "succeeded",
                    "content": {
                        "file_url": f"https://tos.example.test/models/{task_id}.{ext}"
                    },
                },
            )

        if request.method == "GET" and request.url.host == "tos.example.test":
            if request.url.path.endswith(".zip"):
                return httpx.Response(200, content=zipped_glb)
            return httpx.Response(200, content=raw_glb)

        return httpx.Response(404)

    app = create_app(
        settings=studio_env,
        rules=load_rules(),
        ark_transport=httpx.MockTransport(_seed3d_handler),
    )

    async def _run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://studio:8000"
        ) as client:
            # 1. 标准精度（100k faces / low）+ ZIP内嵌GLB解包
            res_std = await client.post(
                "/mcp",
                headers={"X-Studio-Token": VALID_TOKEN, "X-LibreChat-User-ID": USER_A},
                json={
                    "jsonrpc": "2.0",
                    "id": 401,
                    "method": "tools/call",
                    "params": {
                        "name": "create_3d_model",
                        "arguments": {
                            "image_id": upload_id,
                            "detail": "standard",
                            "note": "三足石雕香炉三维预览",
                        },
                    },
                },
            )
            assert res_std.status_code == 200
            result_std = res_std.json()["result"]
            assert result_std.get("isError") is False
            blocks = result_std["content"]
            assert len(blocks) == 3
            assert blocks[0]["type"] == "text"
            assert blocks[1]["type"] == "resource"
            assert blocks[2]["type"] == "image"

            meta = json.loads(blocks[0]["text"])
            model_id = meta["model_id"]
            assert model_id.startswith("mdl_")
            assert meta["state"] == "saved"
            assert meta["detail"] == "standard"
            assert meta["faces"] == 100_000
            assert meta["file_size"] == len(raw_glb)
            assert meta["source_image_id"] == upload_id
            assert meta["download_url"].startswith(
                "https://dev.xuqian.io/studio-files/"
            )
            assert "visual" in meta["disclaimer"].casefold()
            assert "not a manufacturing" in meta["disclaimer"].casefold()

            # 校验UI Resource结构、签名URL嵌入、Three.js 0.170.0固定版本、交互控件与无密钥/厂商泄露
            ui_res = blocks[1]["resource"]
            assert ui_res["uri"] == f"ui://zyb_studio/model/{model_id}"
            assert ui_res["mimeType"] == "text/html"
            html_text = ui_res["text"]
            assert meta["download_url"] in html_text
            assert "three@0.170.0" in html_text
            assert "OrbitControls" in html_text
            assert "RoomEnvironment" in html_text
            assert "ui-size-change" in html_text
            assert "仅供视觉预览（非工程制造模型）" in html_text
            for secret_or_vendor in (
                VALID_TOKEN,
                VALID_SIGNING_KEY,
                studio_env.ark_api_key,
                studio_env.gemini_api_key,
                "seed3d",
                "volcengine",
                "doubao",
                "seedream",
                "gemini",
                "argon",
            ):
                assert secret_or_vendor not in html_text.casefold()
                assert secret_or_vendor not in blocks[0]["text"].casefold()

            # 校验落盘GLB与receipt权限均为0600
            model_dir = studio_env.data_dir / "users" / USER_A / "models" / model_id
            glb_file = model_dir / "model.glb"
            receipt_file = model_dir / "receipt.json"
            assert glb_file.read_bytes() == raw_glb
            assert (glb_file.stat().st_mode & 0o777) == 0o600
            assert (receipt_file.stat().st_mode & 0o777) == 0o600

            # 2. 高精度（500k faces / medium）+ 知识库图片kb_<id>
            res_high = await client.post(
                "/mcp",
                headers={"X-Studio-Token": VALID_TOKEN, "X-LibreChat-User-ID": USER_A},
                json={
                    "jsonrpc": "2.0",
                    "id": 402,
                    "method": "tools/call",
                    "params": {
                        "name": "create_3d_model",
                        "arguments": {
                            "image_id": "kb_5555555555555551",
                            "detail": "high",
                        },
                    },
                },
            )
            assert res_high.status_code == 200
            result_high = res_high.json()["result"]
            assert result_high.get("isError") is False
            meta_high = json.loads(result_high["content"][0]["text"])
            assert meta_high["detail"] == "high"
            assert meta_high["faces"] == 500_000
            assert (
                create_payloads[0]["content"][0]["text"]
                == "三足石雕香炉三维预览 --subdivisionlevel low --fileformat glb"
            )
            assert (
                create_payloads[1]["content"][0]["text"]
                == "--subdivisionlevel medium --fileformat glb"
            )

    asyncio.run(_run())


# 校验GET /files/<token>签名校验、过期、篡改、跨用户隔离、目录枚举404、Range分片与仅在该路由返回CORS头
def test_signed_file_endpoint_auth_expiry_tamper_cross_user_range_and_cors(
    studio_env: StudioSettings,
) -> None:
    model_id = "mdl_0123456789abcdef0123456789abcdef"
    model_dir = studio_env.data_dir / "users" / USER_A / "models" / model_id
    model_dir.mkdir(parents=True, mode=0o700)
    glb_bytes = _make_glb_bytes(
        b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_RANGE_TEST_BYTES"
    )
    (model_dir / "model.glb").write_bytes(glb_bytes)
    (model_dir / "model.glb").chmod(0o600)
    (model_dir / "receipt.json").write_text(
        json.dumps(
            {
                "state": "saved",
                "user_id": USER_A,
                "model_id": model_id,
                "file": "model.glb",
                "bytes": len(glb_bytes),
            }
        ),
        encoding="utf-8",
    )
    (model_dir / "receipt.json").chmod(0o600)

    valid_tok, _ = sign_file_token(
        studio_env,
        USER_A,
        model_id,
        expires_at=1_700_003_600,
    )
    expired_tok, _ = sign_file_token(
        studio_env,
        USER_A,
        model_id,
        expires_at=1_699_999_940,
    )
    other_user_tok, _ = sign_file_token(
        studio_env,
        USER_B,
        model_id,
        expires_at=1_700_003_600,
    )
    missing_model_tok, _ = sign_file_token(
        studio_env,
        USER_A,
        "mdl_ffffffffffffffffffffffffffffffff",
        expires_at=1_700_003_600,
    )
    payload_part, sig_part = valid_tok.split(".", 1)
    tampered_sig_tok = f"{payload_part}.{'0' * 64}"
    tampered_payload_tok = f"e30.{sig_part}"

    import deploy.studio.models3d as models3d_mod

    app = create_app(settings=studio_env, rules=load_rules())

    async def _run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://studio:8000"
        ) as client:
            orig_time = models3d_mod.time.time
            models3d_mod.time.time = lambda: 1_700_000_010.0
            try:
                # 1. 合法签名URL下载完整GLB，验证Content-Type、Accept-Ranges与CORS头
                res_ok = await client.get(f"/files/{valid_tok}")
                assert res_ok.status_code == 200
                assert res_ok.content == glb_bytes
                assert res_ok.headers["content-type"] == "model/gltf-binary"
                assert res_ok.headers["accept-ranges"] == "bytes"
                assert res_ok.headers["access-control-allow-origin"] == "*"
                assert int(res_ok.headers["content-length"]) == len(glb_bytes)

                # 2. HEAD与OPTIONS请求支持
                res_head = await client.head(f"/files/{valid_tok}")
                assert res_head.status_code == 200
                assert res_head.content == b""
                assert res_head.headers["access-control-allow-origin"] == "*"

                res_opt = await client.options(f"/files/{valid_tok}")
                assert res_opt.status_code == 204
                assert res_opt.headers["access-control-allow-origin"] == "*"
                assert "Range" in res_opt.headers["access-control-allow-headers"]

                # 3. Range请求支持（前缀区间、开放区间、后缀区间及非法Range 416）
                res_r1 = await client.get(
                    f"/files/{valid_tok}", headers={"Range": "bytes=0-15"}
                )
                assert res_r1.status_code == 206
                assert res_r1.content == glb_bytes[:16]
                assert res_r1.headers["content-range"] == f"bytes 0-15/{len(glb_bytes)}"
                assert res_r1.headers["access-control-allow-origin"] == "*"

                res_r2 = await client.get(
                    f"/files/{valid_tok}", headers={"Range": "bytes=20-"}
                )
                assert res_r2.status_code == 206
                assert res_r2.content == glb_bytes[20:]
                assert (
                    res_r2.headers["content-range"]
                    == f"bytes 20-{len(glb_bytes) - 1}/{len(glb_bytes)}"
                )

                res_r3 = await client.get(
                    f"/files/{valid_tok}", headers={"Range": "bytes=-12"}
                )
                assert res_r3.status_code == 206
                assert res_r3.content == glb_bytes[-12:]

                for bad_range in ("bytes=9999-10000", "bytes=30-10", "items=0-5"):
                    res_416 = await client.get(
                        f"/files/{valid_tok}", headers={"Range": bad_range}
                    )
                    assert res_416.status_code == 416
                    assert (
                        res_416.headers["content-range"] == f"bytes */{len(glb_bytes)}"
                    )
                    assert res_416.headers["access-control-allow-origin"] == "*"

                # 4. 过期令牌、篡改令牌、跨用户令牌、不存在模型及目录枚举一律返回404
                for bad_path in (
                    f"/files/{expired_tok}",
                    f"/files/{tampered_sig_tok}",
                    f"/files/{tampered_payload_tok}",
                    f"/files/{other_user_tok}",
                    f"/files/{missing_model_tok}",
                    "/files/not-a-valid-token",
                    "/files/a/b",
                    "/files",
                    "/files/",
                ):
                    res_404 = await client.get(bad_path)
                    assert res_404.status_code == 404, (
                        f"Expected 404 for {bad_path}, got {res_404.status_code}"
                    )

                # 5. 验证Access-Control-Allow-Origin: * 仅出现在/files路由，不出现在/health或/mcp
                res_health = await client.get("/health")
                assert "access-control-allow-origin" not in res_health.headers
                res_mcp = await client.post(
                    "/mcp",
                    headers={
                        "X-Studio-Token": VALID_TOKEN,
                        "X-LibreChat-User-ID": USER_A,
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 499,
                        "method": "tools/list",
                        "params": {},
                    },
                )
                assert "access-control-allow-origin" not in res_mcp.headers
            finally:
                models3d_mod.time.time = orig_time

    asyncio.run(_run())


# 校验Seed3D上游失败、超时转uncertain不自动重提、后台继续轮询返回job_id与check_3d_model恢复
def test_create_3d_model_failure_uncertain_background_poll_and_check_resumption(
    studio_env: StudioSettings,
) -> None:
    user_lc = studio_env.librechat_images_dir / USER_A
    user_lc.mkdir(parents=True, mode=0o700)
    upload_id = "eeeeeeee-1111-2222-3333-444444444444"
    (user_lc / f"{upload_id}__pagoda.jpg").write_bytes(
        _make_image_bytes(160, 160, fmt="JPEG")
    )
    raw_glb = _make_glb_bytes(b"PAGODA_GLB_MODEL")

    # 1. 上游任务返回status="failed"时记录failed且不自动重试
    failed_create_calls = 0

    async def _failed_task_handler(request: httpx.Request) -> httpx.Response:
        nonlocal failed_create_calls
        if request.method == "POST":
            failed_create_calls += 1
            return httpx.Response(200, json={"id": "cgt-fail-1"})
        return httpx.Response(
            200,
            json={
                "id": "cgt-fail-1",
                "status": "failed",
                "error": {"code": "MeshFailed", "message": "failed to reconstruct"},
            },
        )

    app_fail = create_app(
        settings=studio_env,
        rules=load_rules(),
        ark_transport=httpx.MockTransport(_failed_task_handler),
    )

    # 2. 创建任务请求超时（已发出POST但未收到响应） -> 转为uncertain且绝不自动重新提交
    timeout_create_calls = 0

    async def _timeout_create_handler(request: httpx.Request) -> httpx.Response:
        nonlocal timeout_create_calls
        timeout_create_calls += 1
        raise httpx.ReadTimeout("timeout after POST create-task")

    app_uncertain = create_app(
        settings=studio_env,
        rules=load_rules(),
        ark_transport=httpx.MockTransport(_timeout_create_handler),
    )

    # 3. 首次工具调用轮询窗口超时后返回job_id并在后台继续轮询，随后通过check_3d_model获取完成结果
    slow_polls = 0
    release_slow_task = asyncio.Event()

    async def _slow_task_handler(request: httpx.Request) -> httpx.Response:
        nonlocal slow_polls
        if request.method == "POST":
            return httpx.Response(200, json={"id": "cgt-slow-1"})
        if request.url.path.endswith("/cgt-slow-1"):
            slow_polls += 1
            if not release_slow_task.is_set():
                return httpx.Response(
                    200, json={"id": "cgt-slow-1", "status": "running"}
                )
            return httpx.Response(
                200,
                json={
                    "id": "cgt-slow-1",
                    "status": "succeeded",
                    "content": {"file_url": "https://tos.example.test/slow.glb"},
                },
            )
        if request.url.host == "tos.example.test":
            return httpx.Response(200, content=raw_glb)
        return httpx.Response(404)

    short_poll_settings = studio_env.model_copy(
        update={
            "seed3d_poll_timeout_seconds": 0.03,
            "seed3d_poll_initial_seconds": 0.01,
            "seed3d_poll_max_interval_seconds": 0.01,
        }
    )
    app_slow = create_app(
        settings=short_poll_settings,
        rules=load_rules(),
        ark_transport=httpx.MockTransport(_slow_task_handler),
    )

    async def _run() -> None:
        # 验证1：上游failed
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_fail), base_url="http://studio:8000"
        ) as client:
            res = await client.post(
                "/mcp",
                headers={"X-Studio-Token": VALID_TOKEN, "X-LibreChat-User-ID": USER_A},
                json={
                    "jsonrpc": "2.0",
                    "id": 501,
                    "method": "tools/call",
                    "params": {
                        "name": "create_3d_model",
                        "arguments": {"image_id": upload_id},
                    },
                },
            )
            result = res.json()["result"]
            assert result["isError"] is True
            assert failed_create_calls == 1

        # 验证2：POST超时转uncertain且不重提
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_uncertain),
            base_url="http://studio:8000",
        ) as client:
            res = await client.post(
                "/mcp",
                headers={"X-Studio-Token": VALID_TOKEN, "X-LibreChat-User-ID": USER_A},
                json={
                    "jsonrpc": "2.0",
                    "id": 502,
                    "method": "tools/call",
                    "params": {
                        "name": "create_3d_model",
                        "arguments": {"image_id": upload_id},
                    },
                },
            )
            result = res.json()["result"]
            assert result["isError"] is True
            err_payload = json.loads(result["content"][0]["text"])
            assert err_payload["state"] == "uncertain"
            assert "may have been billed" in err_payload["error"]
            assert timeout_create_calls == 1

        # 验证3：窗口超时返回job_id + 后台轮询完成 + check_3d_model获取结果与跨用户隔离
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_slow), base_url="http://studio:8000"
        ) as client:
            res_create = await client.post(
                "/mcp",
                headers={"X-Studio-Token": VALID_TOKEN, "X-LibreChat-User-ID": USER_A},
                json={
                    "jsonrpc": "2.0",
                    "id": 503,
                    "method": "tools/call",
                    "params": {
                        "name": "create_3d_model",
                        "arguments": {"image_id": upload_id, "detail": "standard"},
                    },
                },
            )
            result_create = res_create.json()["result"]
            assert result_create.get("isError") is False
            pending_meta = json.loads(result_create["content"][0]["text"])
            assert pending_meta["state"] == "running"
            job_id = pending_meta["job_id"]
            assert job_id.startswith("mdl_")

            # 用户B不能查询用户A的job_id
            res_other = await client.post(
                "/mcp",
                headers={"X-Studio-Token": VALID_TOKEN, "X-LibreChat-User-ID": USER_B},
                json={
                    "jsonrpc": "2.0",
                    "id": 504,
                    "method": "tools/call",
                    "params": {
                        "name": "check_3d_model",
                        "arguments": {"job_id": job_id},
                    },
                },
            )
            assert res_other.json()["result"]["isError"] is True

            # 放行后台轮询任务并调用check_3d_model等待完成
            release_slow_task.set()
            await asyncio.sleep(0.05)
            res_check = await client.post(
                "/mcp",
                headers={"X-Studio-Token": VALID_TOKEN, "X-LibreChat-User-ID": USER_A},
                json={
                    "jsonrpc": "2.0",
                    "id": 505,
                    "method": "tools/call",
                    "params": {
                        "name": "check_3d_model",
                        "arguments": {"job_id": job_id},
                    },
                },
            )
            result_check = res_check.json()["result"]
            assert result_check.get("isError") is False
            check_blocks = result_check["content"]
            assert len(check_blocks) == 3
            check_meta = json.loads(check_blocks[0]["text"])
            assert check_meta["model_id"] == job_id
            assert check_meta["state"] == "saved"
            assert check_blocks[1]["type"] == "resource"
            assert check_blocks[2]["type"] == "image"

        # 验证4：进程重启时残留的running/pending模型receipt在create_app启动时被标记为uncertain
        abandoned_model_dir = (
            studio_env.data_dir
            / "users"
            / USER_A
            / "models"
            / "mdl_abandoned0123456789abcdef012345"
        )
        abandoned_model_dir.mkdir(parents=True, mode=0o700)
        (abandoned_model_dir / "receipt.json").write_text(
            json.dumps({"state": "running", "task_id": "cgt-abandoned"}),
            encoding="utf-8",
        )
        create_app(settings=studio_env, rules=load_rules())
        reconciled = json.loads(
            (abandoned_model_dir / "receipt.json").read_text(encoding="utf-8")
        )
        assert reconciled["state"] == "uncertain"

    asyncio.run(_run())


# 校验Seed3D与每用户并发限制器共享配额且受全局seed3d_max_parallel约束
def test_seed3d_per_user_and_global_concurrency_limits(
    studio_env: StudioSettings,
) -> None:
    user_c = "65f00000000000000000000c"
    for uid in (USER_A, USER_B, user_c):
        ulc = studio_env.librechat_images_dir / uid
        ulc.mkdir(parents=True, mode=0o700)
        (ulc / "11111111-1111-1111-1111-111111111111__src.jpg").write_bytes(
            _make_image_bytes(64, 64, fmt="JPEG")
        )

    capped = studio_env.model_copy(
        update={"max_parallel_per_user": 2, "seed3d_max_parallel": 3}
    )
    raw_glb = _make_glb_bytes()
    active_seed3d = 0
    max_seed3d_observed = 0
    task_seq = 0

    async def _concurrency_handler(request: httpx.Request) -> httpx.Response:
        nonlocal active_seed3d, max_seed3d_observed, task_seq
        if request.method == "POST":
            task_seq += 1
            active_seed3d += 1
            max_seed3d_observed = max(max_seed3d_observed, active_seed3d)
            await asyncio.sleep(0.04)
            active_seed3d -= 1
            return httpx.Response(200, json={"id": f"cgt-conc-{task_seq}"})
        if request.url.path.startswith("/api/v3/contents/generations/tasks/"):
            tid = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    "id": tid,
                    "status": "succeeded",
                    "content": {"file_url": f"https://tos.example.test/{tid}.glb"},
                },
            )
        return httpx.Response(200, content=raw_glb)

    app = create_app(
        settings=capped,
        rules=load_rules(),
        ark_transport=httpx.MockTransport(_concurrency_handler),
    )

    async def _call_3d(client: httpx.AsyncClient, uid: str, req_id: int) -> None:
        res = await client.post(
            "/mcp",
            headers={"X-Studio-Token": VALID_TOKEN, "X-LibreChat-User-ID": uid},
            json={
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/call",
                "params": {
                    "name": "create_3d_model",
                    "arguments": {
                        "image_id": "11111111-1111-1111-1111-111111111111",
                        "detail": "standard",
                    },
                },
            },
        )
        assert res.status_code == 200
        assert res.json()["result"]["isError"] is False

    async def _run() -> None:
        nonlocal max_seed3d_observed
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://studio:8000"
        ) as client:
            # 单用户发起4个并发请求，受max_parallel_per_user=2限制
            max_seed3d_observed = 0
            await asyncio.gather(*(_call_3d(client, USER_A, i) for i in range(1, 5)))
            assert max_seed3d_observed == 2

            # 三用户各发起2个并发请求（共6个），受seed3d_max_parallel=3限制
            max_seed3d_observed = 0
            await asyncio.gather(
                _call_3d(client, USER_A, 11),
                _call_3d(client, USER_A, 12),
                _call_3d(client, USER_B, 13),
                _call_3d(client, USER_B, 14),
                _call_3d(client, user_c, 15),
                _call_3d(client, user_c, 16),
            )
            assert max_seed3d_observed == 3

    asyncio.run(_run())
