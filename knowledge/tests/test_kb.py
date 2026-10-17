# 知识库核心功能与校验器单元测试
from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
from collections import Counter
from pathlib import Path

import pytest
import yaml
from PIL import Image

# 指向知识库根目录的常量路径并加入模块搜索路径以支持从仓库根目录运行测试
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.build import (
    build_index_jsonl,
    build_markdown_views,
    build_originals_archive,
    load_all_records,
    select_archive_records,
)
from tools.ingest import (
    GEMINI_RESPONSE_SCHEMA,
    create_draft_record,
    derive_asset_id,
    generate_derivatives,
    ingest_index,
)
from tools.validate import validate_repository


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

    meta = generate_derivatives(
        src_file, repo, original_rel_path="folder/rotated_photo.jpg"
    )
    ref_path = repo / meta["derivatives"]["ref2048"]["path"]
    thumb_path = repo / meta["derivatives"]["thumb512"]["path"]

    with Image.open(ref_path) as ref_im:
        assert ref_im.size == (800, 1200)
        assert len(ref_im.getexif()) == 0
        r_tr, _g_tr, b_tr = ref_im.getpixel((750, 50))
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
    bad_data["original"]["path"] = (
        "/home/wangtinghu/workspace/zybc/wiki/folder/small.jpg"
    )
    bad_data["material_ids"] = ["non-existent-stone"]
    bad_data["quality"]["notes"] = (
        "Leaked token sk-1234567890abcdefghijklmno and file:///tmp/a"
    )
    rec_path.write_text(
        yaml.safe_dump(bad_data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

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

    rec_path = create_draft_record(
        src_file, repo, original_rel_path="folder/sample.jpg"
    )
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


# 测试真实仓库的607条记录完整性以及索引与分类视图生成无重复和无绝对路径
def test_build_views_and_live_repository(tmp_path: Path) -> None:
    errors = validate_repository(REPO_ROOT, check_images=False)
    assert errors == []

    records = load_all_records(REPO_ROOT)
    assert len(records) == 607

    dist_out = tmp_path / "dist"
    idx_path = build_index_jsonl(REPO_ROOT, records, dist_out)
    idx_lines = idx_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(idx_lines) == 607

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

    assert len(seen_in_views) == 607
    assert len(set(seen_in_views)) == 607


# 构造带真实字节、地域标签和审计侧录的最小新增素材批次
def _staged_batch(tmp_path: Path, cell: str = "SS", license_name: str = "CC BY 4.0"):
    wiki = tmp_path / "wiki"
    folder = wiki / "references"
    folder.mkdir(parents=True)
    image_path = folder / "sample.jpg"
    Image.new("RGB", (1000, 900), (90, 120, 160)).save(image_path, quality=90)
    doc_path = wiki / "text_wiki" / "09-reference.md"
    doc_path.parent.mkdir()
    doc_path.write_text("# Reference volume\n", encoding="utf-8")
    categories = {
        "S": "造像雕塑",
        "R": "法器供器",
        "P": "石刻纹样",
        "A": "建筑景观",
        "F": "家具木作",
    }
    regions = {
        "W": "西方",
        "S": "东南亚/南亚",
        "N": "中国北方",
        "C": "中国南方",
        "T": "中国藏区·藏传佛教",
    }
    category = categories[cell[0]]
    region = regions[cell[1]]
    entity = "建筑场景实景" if cell[0] == "A" else "实物对象写实"
    entry = {
        "id": "references/sample.jpg",
        "folder": "references",
        "filename": "sample.jpg",
        "rel_path": "wiki/references/sample.jpg",
        "wiki_doc": "wiki/text_wiki/09-reference.md",
        "entity_type": entity,
        "category_l1": category,
        "category_l2": "已审计参考",
        "summary_zh": "原图可见形态描述。",
        "materials": ["石材"],
        "colors": ["灰色"],
        "style": region + "传统参考",
        "ai_design_usage": "轮廓参考",
        "prompt_zh": "保留可见轮廓。",
        "prompt_en": "Preserve the visible silhouette.",
        "tags": [category, region, "真实摄影"],
        "entity_type_norm": entity,
    }
    source = {
        "id": entry["id"],
        "status": "accepted",
        "cell": cell,
        "rel_path": entry["rel_path"],
        "wiki_doc": entry["wiki_doc"],
        "source_record_url": "https://commons.wikimedia.org/wiki/File:sample.jpg",
        "source_url": "https://upload.wikimedia.org/sample.jpg",
        "license": license_name,
        "license_url": "http://creativecommons.org/publicdomain/zero/1.0/"
        if license_name == "CC0"
        else "https://creativecommons.org/licenses/by/4.0/",
        "credit_line": "Sample photographer",
        "sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
        "bytes": image_path.stat().st_size,
        "width": 1000,
        "height": 900,
        "full_image_inspected": True,
        "crops_inspected": [{"native_resolution": True}, {"native_resolution": True}],
        "catalog_note": "Source title and photographed object differ.",
    }
    index = tmp_path / "index.jsonl"
    provenance = tmp_path / "sources.jsonl"
    index.write_text(json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8")
    provenance.write_text(
        json.dumps(source, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return wiki, index, provenance, entry, source


# 覆盖全部新增授权类型及审计修正后的文化地域和建筑场景映射
@pytest.mark.parametrize(
    "cell,license_name,expected_region,expected_license",
    [
        ("SS", "CC0", "南亚东南亚", "cc0"),
        ("SW", "Public domain", "西方", "public-domain"),
        ("RN", "CC BY 2.0", "中国北方", "cc-by-2.0"),
        ("PC", "CC BY 3.0", "中国南方", "cc-by-3.0"),
        ("FT", "CC BY 4.0", "中国藏区", "cc-by-4.0"),
        ("AC", "CC BY-SA 2.0", "中国南方", "cc-by-sa-2.0"),
        ("SS", "CC BY-SA 3.0", "南亚东南亚", "cc-by-sa-3.0"),
        ("RT", "CC BY-SA 4.0", "中国藏区", "cc-by-sa-4.0"),
    ],
)
def test_audited_ingest_metadata_and_no_overwrite(
    tmp_path, cell, license_name, expected_region, expected_license
):
    repo = _init_temp_repo(tmp_path / "kb")
    wiki, index, provenance, entry, source = _staged_batch(tmp_path, cell, license_name)
    paths = ingest_index(index, provenance, wiki, repo)
    assert len(paths) == 1
    record = yaml.safe_load(paths[0].read_text())
    assert record["region"] == expected_region
    assert record["library_type"] == entry["category_l1"]
    assert record["provenance"]["license"] == expected_license
    assert record["provenance"]["credit"] == source["credit_line"]
    assert record["provenance"]["license_url"] == source["license_url"]
    assert record["provenance"]["license_status"] == "confirmed"
    assert record["provenance"]["source"] == "web"
    assert record["review"]["status"] == "draft"
    assert "source_catalog_photo_mismatch" in record["review"]["issues"]
    assert record["quality"]["min_side_px"] == 900
    assert record["quality"]["low_res"] is False
    assert record["entity_type"] == (
        "空间场景设计" if cell[0] == "A" else "实物对象写实"
    )
    assert record["prompt_en"] == entry["prompt_en"]
    manifest = repo / record["source"]["import_manifest"]
    assert json.loads(manifest.read_text()) == {
        "id": record["id"],
        "index": entry,
        "provenance": source,
    }
    assert validate_repository(repo) == []
    before = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="overwrite"):
        ingest_index(index, provenance, wiki, repo)
    assert {p: p.read_bytes() for p in repo.rglob("*") if p.is_file()} == before
    manifest.write_text(
        manifest.read_text().replace("Sample photographer", "Changed photographer")
    )
    assert any(
        "manifest hash mismatch" in error
        for error in validate_repository(repo, check_images=False)
    )


# 检查审计输入损坏或误归地域时整批拒绝，不能留下部分衍生图
@pytest.mark.parametrize(
    "failure",
    ["hash", "region", "license", "url", "path", "missing_field", "duplicate"],
)
def test_audited_ingest_rejects_invalid_batch(tmp_path, failure):
    repo = _init_temp_repo(tmp_path / "kb")
    wiki, index, provenance, entry, source = _staged_batch(tmp_path)
    if failure == "hash":
        source["sha256"] = "0" * 64
    elif failure == "region":
        source["cell"] = "SW"
    elif failure == "license":
        source["license"] = "All rights reserved"
    elif failure == "url":
        source["license_url"] = "file:///tmp/license"
    elif failure == "path":
        entry["rel_path"] = "wiki/../outside.jpg"
    elif failure == "missing_field":
        entry.pop("prompt_en")
    index.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    provenance.write_text(json.dumps(source) + "\n", encoding="utf-8")
    if failure == "duplicate":
        provenance.write_text(provenance.read_text() * 2)
    with pytest.raises(ValueError):
        ingest_index(index, provenance, wiki, repo)
    assert list((repo / "records").iterdir()) == []
    assert list((repo / "assets/ref2048").iterdir()) == []
    assert list((repo / "assets/thumb512").iterdir()) == []


# 检查地域必填和未知值的约束，不允许旧记录遗漏迁移
def test_region_schema_is_required_and_restricted(tmp_path):
    repo = _init_temp_repo(tmp_path / "kb")
    image_path = tmp_path / "sample.jpg"
    Image.new("RGB", (900, 900)).save(image_path)
    path = create_draft_record(image_path, repo)
    record = yaml.safe_load(path.read_text())
    assert record["region"] == "未知"
    for invalid in (None, "Tibet"):
        if invalid is None:
            record.pop("region")
        else:
            record["region"] = invalid
        path.write_text(yaml.safe_dump(record, allow_unicode=True))
        assert any(
            "region" in error for error in validate_repository(repo, check_images=False)
        )


# 验证增量归档只含指定原图，解包后的实际字节与清单一致且篡改原图会被拒绝
def test_delta_archive_members_and_source_integrity(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    repo = _init_temp_repo(tmp_path / "kb")
    records = []
    for name, color in [("existing.jpg", "red"), ("-new original.jpg", "blue")]:
        image_path = wiki / name
        Image.new("RGB", (900, 900), color).save(image_path)
        path = create_draft_record(image_path, repo)
        records.append(yaml.safe_load(path.read_text()))
    selected = select_archive_records(records, [records[1]["id"]])
    for bad_ids in ([], ["0" * 16], [records[1]["id"]] * 2):
        with pytest.raises(ValueError):
            select_archive_records(records, bad_ids)
    archive = build_originals_archive(selected, repo / "dist", wiki, delta=True)
    raw = (
        subprocess.run(
            ["zstd", "-dc", str(archive)], check=True, capture_output=True
        ).stdout
        if archive.name.endswith(".zst")
        else None
    )
    with (
        tarfile.open(fileobj=io.BytesIO(raw), mode="r:")
        if raw is not None
        else tarfile.open(archive, "r:gz")
    ) as tar:
        assert set(tar.getnames()) == {
            "originals-manifest.json",
            "SHA256SUMS",
            "-new original.jpg",
        }
        assert (
            tar.extractfile("-new original.jpg").read()
            == (wiki / "-new original.jpg").read_bytes()
        )
        manifest = json.load(tar.extractfile("originals-manifest.json"))
        assert manifest["count"] == 1 and manifest["kind"] == "delta"
        assert manifest["files"][0]["id"] == records[1]["id"]
    (wiki / "-new original.jpg").write_bytes(b"modified original")
    with pytest.raises(ValueError, match="SHA256/size mismatch"):
        build_originals_archive(selected, repo / "dist", wiki, delta=True)


# 核对已合并批次的真实覆盖量及全部索引、来源字段，防止迁移时丢失审计信息
def test_open_license_corpus_metadata():
    records = load_all_records(REPO_ROOT)
    additions = [
        record for record in records if record["source"].get("import_manifest")
    ]
    assert len(additions) == 239
    assert Counter(record["library_type"] for record in additions) == {
        "造像雕塑": 55,
        "法器供器": 50,
        "石刻纹样": 41,
        "建筑景观": 42,
        "家具木作": 51,
    }
    assert Counter(record["region"] for record in additions) == {
        "西方": 50,
        "南亚东南亚": 58,
        "中国北方": 58,
        "中国南方": 53,
        "中国藏区": 20,
    }
    manifests = {record["source"]["import_manifest"] for record in additions}
    assert len(manifests) == 1
    items = [
        json.loads(line)
        for line in (REPO_ROOT / manifests.pop()).read_text().splitlines()
    ]
    by_id = {item["id"]: item for item in items}
    assert set(by_id) == {record["id"] for record in additions}
    for record in additions:
        item = by_id[record["id"]]
        entry = item["index"]
        source = item["provenance"]
        assert len(entry) == 17
        for field in (
            "summary_zh",
            "materials",
            "colors",
            "style",
            "ai_design_usage",
            "prompt_zh",
            "prompt_en",
            "tags",
        ):
            assert record[field] == entry[field]
        assert record["original"]["sha256"] == source["sha256"]
        assert record["provenance"]["url"] == source["source_record_url"]
        assert record["provenance"]["download_url"] == source["source_url"]
        assert record["provenance"]["license_url"] == source["license_url"]
        assert record["provenance"]["credit"] == source["credit_line"]
        assert record["provenance"]["license_status"] == "confirmed"
        assert record["review"]["status"] == "draft"


# 保留带有多图容器信息的原始字节，衍生图只使用容器中的主图
def test_mpo_original_preserved_and_primary_frame_used(tmp_path):
    repo = _init_temp_repo(tmp_path / "kb")
    source = tmp_path / "multi-picture.jpg"
    primary = Image.new("RGB", (1000, 900), "red")
    secondary = Image.new("RGB", (1000, 900), "blue")
    primary.save(source, format="MPO", save_all=True, append_images=[secondary])
    original_bytes = source.read_bytes()
    record_path = create_draft_record(source, repo)
    record = yaml.safe_load(record_path.read_text())
    assert record["original"]["format"] == "MPO"
    assert source.read_bytes() == original_bytes
    assert record["original"]["sha256"] == hashlib.sha256(original_bytes).hexdigest()
    with Image.open(repo / record["derivatives"]["ref2048"]["path"]) as derivative:
        red, green, blue = derivative.getpixel((500, 400))
        assert red > 240 and green < 10 and blue < 10
    assert validate_repository(repo) == []


# 验证真实knowledge/语料在复合中文查询（含口语停用词与多词组合）下的分词与加权检索结果
def test_real_kb_chinese_multi_term_search() -> None:
    project_root = REPO_ROOT.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    from deploy.studio.kb import load_kb_index

    kb_index = load_kb_index(REPO_ROOT)
    assert len(kb_index.records) == 607

    for query in (
        "「须弥座石雕参考」",
        "须弥座石雕参考",
        "找几张须弥座石雕参考图片",
    ):
        results = kb_index.search(query, limit=6)
        assert len(results) == 6
        result_ids = [r.kb_id for r in results]
        assert "kb_9ffe71e1ee8bd6e7" in result_ids
        assert "kb_db8c9deb470a3da2" in result_ids
        for rec in results:
            assert not rec.is_flagged
            assert "须弥座" in rec.search_all and "石雕" in rec.search_all
