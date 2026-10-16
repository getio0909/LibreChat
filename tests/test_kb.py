# 知识库核心功能与校验器单元测试
from __future__ import annotations

import hashlib
import io
import json
import shutil
from pathlib import Path

import pytest
import yaml
from PIL import Image

from tools.build import build_index_jsonl, build_markdown_views, load_all_records
from tools.ingest import (
    GEMINI_RESPONSE_SCHEMA,
    create_draft_record,
    derive_asset_id,
    generate_derivatives,
)
from tools.validate import validate_repository

# 指向当前仓库根目录的常量路径
REPO_ROOT = Path(__file__).resolve().parent.parent


# 辅助函数：在临时目录中初始化最小合法知识库结构
def _init_temp_repo(tmp_path: Path) -> Path:
    (tmp_path / "schema").mkdir(parents=True, exist_ok=True)
    (tmp_path / "catalog").mkdir(parents=True, exist_ok=True)
    (tmp_path / "records").mkdir(parents=True, exist_ok=True)
    (tmp_path / "assets" / "ref2048").mkdir(parents=True, exist_ok=True)
    (tmp_path / "assets" / "thumb512").mkdir(parents=True, exist_ok=True)

    shutil.copy2(
        REPO_ROOT / "schema" / "record.schema.json",
        tmp_path / "schema" / "record.schema.json",
    )

    materials = {
        "schema": 1,
        "materials": [
            {
                "id": "dunhuang-gold",
                "name": "敦煌金",
                "aliases": ["天川金"],
                "search_terms": ["阿利坦"],
                "group": "天川金大板自家产品",
                "sample_asset_ids": [],
                "all_slab_asset_ids": [],
            }
        ],
    }
    (tmp_path / "catalog" / "materials.yaml").write_text(
        yaml.safe_dump(materials, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    styles = {
        "schema": 1,
        "verified": "2026-10-10",
        "missing_references": [],
        "references": [],
        "packs": [],
    }
    (tmp_path / "catalog" / "styles.yaml").write_text(
        yaml.safe_dump(styles, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return tmp_path


# 测试资产ID由文件内容SHA256前16位派生且与中文文件名无关
def test_derive_asset_id_stability_and_ascii(tmp_path: Path) -> None:
    img = Image.new("RGB", (900, 900), (120, 80, 40))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    raw = buf.getvalue()

    chinese_file_a = tmp_path / "天然美石佛国画卷展墙.jpg"
    chinese_file_b = tmp_path / "天然美石佛國畫卷展墙-副本.jpg"
    chinese_file_a.write_bytes(raw)
    chinese_file_b.write_bytes(raw)

    expected = hashlib.sha256(raw).hexdigest()[:16]
    id_a = derive_asset_id(chinese_file_a)
    id_b = derive_asset_id(chinese_file_b)
    id_bytes = derive_asset_id(raw)

    assert id_a == expected
    assert id_b == expected
    assert id_bytes == expected
    assert len(id_a) == 16
    assert id_a.isascii()
    assert all(ch in "0123456789abcdef" for ch in id_a)


# 测试CMYK模式超大图自动转换为sRGB且长边缩放至2048与512像素以内
def test_cmyk_to_srgb_and_resizing(tmp_path: Path) -> None:
    repo = _init_temp_repo(tmp_path)
    cmyk_img = Image.new("CMYK", (3000, 2000), (20, 60, 100, 10))
    src_file = tmp_path / "室内平面尺寸图.jpg"
    cmyk_img.save(src_file, format="JPEG", quality=92)

    meta = generate_derivatives(
        src_file,
        repo,
        original_rel_path="26年春季佛事展设计方案/室内平面尺寸图.jpg",
    )

    assert meta["original"]["color_mode"] == "CMYK"
    assert meta["original"]["width"] == 3000
    assert meta["original"]["height"] == 2000

    ref_path = repo / meta["derivatives"]["ref2048"]["path"]
    thumb_path = repo / meta["derivatives"]["thumb512"]["path"]
    assert ref_path.is_file()
    assert thumb_path.is_file()

    with Image.open(ref_path) as ref_im:
        assert ref_im.format == "JPEG"
        assert ref_im.mode == "RGB"
        assert ref_im.size == (2048, 1365)
        assert not ref_im.getexif()

    with Image.open(thumb_path) as thumb_im:
        assert thumb_im.format == "WEBP"
        assert thumb_im.mode == "RGB"
        assert thumb_im.size == (512, 341)


# 测试带EXIF旋转方向标签的图像在生成衍生图时被正确转置并剥离EXIF
def test_exif_orientation_transpose_and_strip(tmp_path: Path) -> None:
    repo = _init_temp_repo(tmp_path)
    raw_img = Image.new("RGB", (1200, 800), (10, 20, 200))
    for x in range(200):
        for y in range(200):
            raw_img.putpixel((x, y), (250, 20, 20))

    exif = Image.Exif()
    exif[0x0112] = 6
    src_file = tmp_path / "rotated_photo.jpg"
    raw_img.save(src_file, format="JPEG", quality=95, exif=exif.tobytes())

    meta = generate_derivatives(src_file, repo, original_rel_path="folder/rotated_photo.jpg")
    ref_path = repo / meta["derivatives"]["ref2048"]["path"]
    thumb_path = repo / meta["derivatives"]["thumb512"]["path"]

    with Image.open(ref_path) as ref_im:
        assert ref_im.size == (800, 1200)
        assert len(ref_im.getexif()) == 0
        r_tr, g_tr, b_tr = ref_im.getpixel((750, 50))
        assert r_tr > 180 and b_tr < 80

    with Image.open(thumb_path) as thumb_im:
        assert thumb_im.size == (341, 512)


# 测试合法草稿记录入库后通过完整仓库校验且Gemini模式定义完整
def test_valid_draft_record_passes_validator(tmp_path: Path) -> None:
    repo = _init_temp_repo(tmp_path)
    img = Image.new("RGB", (1024, 900), (180, 150, 90))
    src_file = tmp_path / "slab.jpg"
    img.save(src_file, format="JPEG", quality=90)

    rec_path = create_draft_record(
        src_file,
        repo,
        original_rel_path="天川金大板自家产品/展览中的黄色纹理石材样品.jpg",
        provenance_source="own_product",
    )
    assert rec_path.is_file()
    assert "title_zh" in GEMINI_RESPONSE_SCHEMA["properties"]

    errors = validate_repository(repo)
    assert errors == []


# 测试校验器能否捕获各种违规情况（前缀、绝对路径、哈希篡改、未知石材ID、密钥泄漏）
def test_validator_catches_failure_cases(tmp_path: Path) -> None:
    repo = _init_temp_repo(tmp_path)
    img = Image.new("RGB", (600, 500), (100, 100, 100))
    src_file = tmp_path / "small.jpg"
    img.save(src_file, format="JPEG", quality=85)

    rec_path = create_draft_record(src_file, repo, original_rel_path="folder/small.jpg")
    base_data = yaml.safe_load(rec_path.read_text(encoding="utf-8"))
    assert base_data["quality"]["low_res"] is True
    assert validate_repository(repo) == []

    # 构造包含wiki/前缀、绝对路径、未知石材ID和模拟密钥的违规记录
    bad_data = json.loads(json.dumps(base_data))
    bad_data["source"]["original_path"] = "wiki/folder/small.jpg"
    bad_data["original"]["path"] = "/home/wangtinghu/workspace/zybc/wiki/folder/small.jpg"
    bad_data["material_ids"] = ["non-existent-stone"]
    bad_data["quality"]["notes"] = "Leaked token sk-1234567890abcdefghijklmno and file:///tmp/a"
    rec_path.write_text(yaml.safe_dump(bad_data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    # 篡改衍生图文件内容以触发SHA256不匹配，并写入孤儿衍生图文件
    ref_file = repo / base_data["derivatives"]["ref2048"]["path"]
    ref_file.write_bytes(ref_file.read_bytes() + b"corrupted")
    orphan_file = repo / "assets" / "ref2048" / "0000000000000000.jpg"
    orphan_file.write_bytes(b"orphan")

    errors = validate_repository(repo, check_images=False)
    joined = "\n".join(errors)

    assert "forbidden 'wiki/' prefix" in joined
    assert "Linux home path" in joined
    assert "file:// URI" in joined
    assert "unknown material_id 'non-existent-stone'" in joined
    assert "OpenAI/API secret key" in joined
    assert "sha256 mismatch" in joined
    assert "Orphan derivative file" in joined


# 测试校验器对空摘要及必填核心字段缺失/置空时触发Schema校验失败
def test_validator_fails_on_empty_summary(tmp_path: Path) -> None:
    repo = _init_temp_repo(tmp_path)
    img = Image.new("RGB", (900, 900), (140, 120, 100))
    src_file = tmp_path / "sample.jpg"
    img.save(src_file, format="JPEG", quality=85)

    rec_path = create_draft_record(src_file, repo, original_rel_path="folder/sample.jpg")
    base_data = yaml.safe_load(rec_path.read_text(encoding="utf-8"))
    assert validate_repository(repo, check_images=False) == []

    # 置空summary_zh并验证校验器报错
    empty_summary_data = json.loads(json.dumps(base_data))
    empty_summary_data["summary_zh"] = ""
    rec_path.write_text(
        yaml.safe_dump(empty_summary_data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    errors = validate_repository(repo, check_images=False)
    assert any("schema error at summary_zh" in err for err in errors)

    # 同时测试title_zh为空与studio_roles为空列表时同样触发Schema报错
    empty_required_data = json.loads(json.dumps(base_data))
    empty_required_data["title_zh"] = ""
    empty_required_data["studio_roles"] = []
    rec_path.write_text(
        yaml.safe_dump(empty_required_data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    errors_req = validate_repository(repo, check_images=False)
    assert any("schema error at title_zh" in err for err in errors_req)
    assert any("schema error at studio_roles" in err for err in errors_req)


# 测试真实仓库的368条记录完整性以及索引与分类视图生成无重复和无绝对路径
def test_build_views_and_live_repository(tmp_path: Path) -> None:
    errors = validate_repository(REPO_ROOT, check_images=False)
    assert errors == []

    records = load_all_records(REPO_ROOT)
    assert len(records) == 368

    dist_out = tmp_path / "dist"
    idx_path = build_index_jsonl(REPO_ROOT, records, dist_out)
    idx_lines = idx_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(idx_lines) == 368

    views = build_markdown_views(records, dist_out)
    assert len(views) == 9

    seen_in_views: list[str] = []
    for v_path in views:
        txt = v_path.read_text(encoding="utf-8")
        assert "file://" not in txt
        assert "/home/" not in txt
        for line in txt.splitlines():
            if line.startswith("## "):
                aid = line.split(" — ")[0].replace("## ", "").strip()
                seen_in_views.append(aid)

    assert len(seen_in_views) == 368
    assert len(set(seen_in_views)) == 368

