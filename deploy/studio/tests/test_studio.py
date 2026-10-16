import asyncio
import base64
import io
import json
import os
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
from deploy.studio.planner import plan_concepts
from deploy.studio.store import list_user_images, resolve_image

# 测试固定常量定义
VALID_TOKEN = "a" * 64
USER_A = "65f00000000000000000000a"
USER_B = "65f00000000000000000000b"


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


# 构建测试用StudioSettings实例
@pytest.fixture
def studio_env(tmp_path: Path) -> StudioSettings:
    lc_dir = tmp_path / "librechat_images"
    data_dir = tmp_path / "data"
    lc_dir.mkdir(parents=True, mode=0o700)
    data_dir.mkdir(parents=True, mode=0o700)
    secret_file = tmp_path / "studio.json"
    secret_data = {
        "shared_token": VALID_TOKEN,
        "gemini_api_key": "test-gemini-key-no-spaces",
        "ark_api_key": "test-ark-key-no-spaces",
        "librechat_images_dir": str(lc_dir),
        "data_dir": str(data_dir),
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
    assert loaded.planner_model == "gemini-4-argon"
    assert loaded.planner_thinking_level == "HIGH"
    assert loaded.max_parallel_per_user == 3
    assert loaded.seedream_timeout_seconds == 240
    assert loaded.planner_timeout_seconds == 540


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

    symlink_file = user_a_lc / "symid__link.jpg"
    symlink_file.symlink_to(img_upload_b)
    with pytest.raises(ValueError):
        resolve_image(studio_env, USER_A, "symid")


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
        assert rules.planner_system in sys_text
        assert rules.planner_rules in sys_text
        assert rules.no_reference_rules in sys_text

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
        async with httpx.AsyncClient(
            transport=transport, base_url="http://studio:8000"
        ) as client:
            # 1. 验证tools/list返回恰好4个工具且Schema完全扁平
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
            ]
            for tool in tools:
                _assert_flat_schema(tool["inputSchema"])
                serialized = json.dumps(tool, ensure_ascii=False).casefold()
                for forbidden_vendor in (
                    "seedream",
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
                            "references": [{"id": upload_id, "use": "材质"}],
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
