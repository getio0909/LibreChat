# 知识库完整性与规范性校验脚本
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import jsonschema
import yaml
from PIL import Image

# 定义常见密钥与凭证的正则表达式模式
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("OpenAI/API secret key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("AWS access key ID", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub personal access token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("Private key block", re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----")),
]

# 定义绝对路径与禁止URI前缀的正则表达式模式
FORBIDDEN_PATH_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("file:// URI", re.compile(r"file://", re.IGNORECASE)),
    ("Linux home path", re.compile(r"/home/[A-Za-z0-9_\-]+")),
    ("macOS Users path", re.compile(r"/Users/[A-Za-z0-9_\-]+")),
]


# 计算指定文件的SHA256摘要与字节大小
def _file_sha256_and_size(path: Path) -> tuple[str, int]:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


# 递归提取数据结构中的所有字符串及对应字段路径
def _iter_strings(obj: Any, prefix: str = "") -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    if isinstance(obj, str):
        items.append((prefix, obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            sub = f"{prefix}.{k}" if prefix else str(k)
            items.extend(_iter_strings(v, sub))
    elif isinstance(obj, list):
        for idx, v in enumerate(obj):
            sub = f"{prefix}[{idx}]"
            items.extend(_iter_strings(v, sub))
    return items


# 扫描文本内容是否包含敏感密钥或禁止的绝对路径
def _scan_text_security_and_paths(text: str, rel_file: str, check_paths: bool = True) -> list[str]:
    errors: list[str] = []
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            errors.append(f"{rel_file}: detected forbidden secret pattern ({label})")
    if check_paths:
        for label, pattern in FORBIDDEN_PATH_PATTERNS:
            if pattern.search(text):
                errors.append(f"{rel_file}: detected forbidden path pattern ({label})")
    return errors


# 校验整个知识库仓库的结构、记录、衍生图与目录引用
def validate_repository(repo_root: Path | str, check_images: bool = True) -> list[str]:
    root = Path(repo_root).resolve()
    errors: list[str] = []

    schema_path = root / "schema" / "record.schema.json"
    materials_path = root / "catalog" / "materials.yaml"
    styles_path = root / "catalog" / "styles.yaml"
    records_dir = root / "records"
    ref_dir = root / "assets" / "ref2048"
    thumb_dir = root / "assets" / "thumb512"

    if not schema_path.is_file():
        errors.append("Missing schema/record.schema.json")
        return errors

    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validator = jsonschema.Draft202012Validator(schema)
    except Exception as exc:
        errors.append(f"Invalid JSON schema in schema/record.schema.json: {exc}")
        return errors

    catalog_material_ids: set[str] = set()
    referenced_catalog_assets: list[tuple[str, str]] = []

    if not materials_path.is_file():
        errors.append("Missing catalog/materials.yaml")
    else:
        try:
            mat_text = materials_path.read_text(encoding="utf-8")
            errors.extend(_scan_text_security_and_paths(mat_text, "catalog/materials.yaml"))
            mat_data = yaml.safe_load(mat_text)
            if not isinstance(mat_data, dict) or not isinstance(mat_data.get("materials"), list):
                errors.append("catalog/materials.yaml must contain a 'materials' list")
            else:
                for entry in mat_data["materials"]:
                    mid = entry.get("id")
                    if not mid or not isinstance(mid, str):
                        errors.append("catalog/materials.yaml entry missing string 'id'")
                    elif mid in catalog_material_ids:
                        errors.append(f"catalog/materials.yaml duplicate material id: {mid}")
                    else:
                        catalog_material_ids.add(mid)
                    for aid in entry.get("sample_asset_ids", []) or []:
                        referenced_catalog_assets.append(("catalog/materials.yaml", aid))
                    for aid in entry.get("all_slab_asset_ids", []) or []:
                        referenced_catalog_assets.append(("catalog/materials.yaml", aid))
        except Exception as exc:
            errors.append(f"Failed to parse catalog/materials.yaml: {exc}")

    if not styles_path.is_file():
        errors.append("Missing catalog/styles.yaml")
    else:
        try:
            sty_text = styles_path.read_text(encoding="utf-8")
            errors.extend(_scan_text_security_and_paths(sty_text, "catalog/styles.yaml"))
            sty_data = yaml.safe_load(sty_text)
            if not isinstance(sty_data, dict):
                errors.append("catalog/styles.yaml must parse to a mapping")
            else:
                for ref in sty_data.get("references", []) or []:
                    aid = ref.get("asset_id")
                    if aid:
                        referenced_catalog_assets.append(("catalog/styles.yaml", aid))
                for pack in sty_data.get("packs", []) or []:
                    for aid in pack.get("asset_ids", []) or []:
                        referenced_catalog_assets.append(("catalog/styles.yaml", aid))
        except Exception as exc:
            errors.append(f"Failed to parse catalog/styles.yaml: {exc}")

    readme_path = root / "README.md"
    if readme_path.is_file():
        errors.extend(
            _scan_text_security_and_paths(readme_path.read_text(encoding="utf-8"), "README.md")
        )

    if not records_dir.is_dir():
        errors.append("Missing records/ directory")
        return errors

    record_files = sorted(records_dir.glob("*.yaml"))
    if not record_files:
        errors.append("No YAML record files found in records/")
        return errors

    seen_ids: set[str] = set()
    seen_orig_shas: set[str] = set()
    expected_refs: set[str] = set()
    expected_thumbs: set[str] = set()

    for rec_file in record_files:
        rel_rec = f"records/{rec_file.name}"
        try:
            raw_text = rec_file.read_text(encoding="utf-8")
        except Exception as exc:
            errors.append(f"{rel_rec}: failed to read UTF-8 text: {exc}")
            continue

        errors.extend(_scan_text_security_and_paths(raw_text, rel_rec))

        try:
            data = yaml.safe_load(raw_text)
        except Exception as exc:
            errors.append(f"{rel_rec}: YAML parse error: {exc}")
            continue

        if not isinstance(data, dict):
            errors.append(f"{rel_rec}: YAML root must be a mapping")
            continue

        for v_err in validator.iter_errors(data):
            loc = ".".join(str(p) for p in v_err.absolute_path) or "<root>"
            errors.append(f"{rel_rec}: schema error at {loc}: {v_err.message}")

        for field_path, str_val in _iter_strings(data):
            is_web_url = field_path == "provenance.url" and str_val.startswith(("https://", "http://"))
            if str_val.startswith("wiki/") or (not is_web_url and "/wiki/" in str_val):
                errors.append(f"{rel_rec}: forbidden 'wiki/' prefix in {field_path}: {str_val}")
            if field_path in (
                "source.original_path",
                "original.path",
                "derivatives.ref2048.path",
                "derivatives.thumb512.path",
            ):
                if str_val.startswith("/") or ".." in Path(str_val).parts:
                    errors.append(f"{rel_rec}: non-relative or traversal path in {field_path}: {str_val}")

        rec_id = data.get("id")
        if not isinstance(rec_id, str):
            continue

        if rec_file.stem != rec_id:
            errors.append(f"{rel_rec}: filename stem '{rec_file.stem}' does not match record id '{rec_id}'")

        if rec_id in seen_ids:
            errors.append(f"{rel_rec}: duplicate record id '{rec_id}'")
        seen_ids.add(rec_id)

        orig = data.get("original") or {}
        orig_sha = orig.get("sha256", "")
        if isinstance(orig_sha, str) and orig_sha:
            if orig_sha[:16] != rec_id:
                errors.append(
                    f"{rel_rec}: record id '{rec_id}' does not match original.sha256 prefix '{orig_sha[:16]}'"
                )
            if orig_sha in seen_orig_shas:
                errors.append(f"{rel_rec}: duplicate original.sha256 '{orig_sha}'")
            seen_orig_shas.add(orig_sha)

        src_obj = data.get("source") or {}
        if src_obj.get("original_path") != orig.get("path"):
            errors.append(
                f"{rel_rec}: source.original_path '{src_obj.get('original_path')}' != original.path '{orig.get('path')}'"
            )

        qual = data.get("quality") or {}
        if isinstance(orig.get("width"), int) and isinstance(orig.get("height"), int):
            expected_min = min(orig["width"], orig["height"])
            if qual.get("min_side_px") != expected_min:
                errors.append(
                    f"{rel_rec}: quality.min_side_px ({qual.get('min_side_px')}) != min(width, height) ({expected_min})"
                )
            if qual.get("low_res") != (expected_min < 800):
                errors.append(
                    f"{rel_rec}: quality.low_res ({qual.get('low_res')}) != ({expected_min < 800})"
                )

        for mid in data.get("material_ids") or []:
            if mid not in catalog_material_ids:
                errors.append(f"{rel_rec}: unknown material_id '{mid}' not in catalog/materials.yaml")

        derivs = data.get("derivatives") or {}
        ref_meta = derivs.get("ref2048") or {}
        thumb_meta = derivs.get("thumb512") or {}

        exp_ref_rel = f"assets/ref2048/{rec_id}.jpg"
        exp_thumb_rel = f"assets/thumb512/{rec_id}.webp"
        expected_refs.add(f"{rec_id}.jpg")
        expected_thumbs.add(f"{rec_id}.webp")

        if ref_meta.get("path") != exp_ref_rel:
            errors.append(f"{rel_rec}: derivatives.ref2048.path must be '{exp_ref_rel}'")
        if thumb_meta.get("path") != exp_thumb_rel:
            errors.append(f"{rel_rec}: derivatives.thumb512.path must be '{exp_thumb_rel}'")

        ref_file = root / exp_ref_rel
        if not ref_file.is_file() or ref_file.is_symlink():
            errors.append(f"{rel_rec}: missing or symlink derivative {exp_ref_rel}")
        else:
            actual_sha, actual_size = _file_sha256_and_size(ref_file)
            if actual_sha != ref_meta.get("sha256"):
                errors.append(
                    f"{rel_rec}: {exp_ref_rel} sha256 mismatch (expected {ref_meta.get('sha256')}, got {actual_sha})"
                )
            if actual_size != ref_meta.get("size") or actual_size != ref_meta.get("bytes"):
                errors.append(
                    f"{rel_rec}: {exp_ref_rel} size mismatch (file={actual_size}, size={ref_meta.get('size')}, bytes={ref_meta.get('bytes')})"
                )
            if check_images:
                try:
                    with Image.open(ref_file) as im:
                        if im.format != "JPEG" or im.mode != "RGB":
                            errors.append(
                                f"{rel_rec}: {exp_ref_rel} must be RGB JPEG (got {im.format} {im.mode})"
                            )
                        if max(im.size) > 2048:
                            errors.append(f"{rel_rec}: {exp_ref_rel} exceeds 2048px ({im.size})")
                        if im.size != (ref_meta.get("width"), ref_meta.get("height")):
                            errors.append(
                                f"{rel_rec}: {exp_ref_rel} dimensions {im.size} != metadata {(ref_meta.get('width'), ref_meta.get('height'))}"
                            )
                        if im.getexif():
                            errors.append(f"{rel_rec}: {exp_ref_rel} still contains EXIF data")
                except Exception as exc:
                    errors.append(f"{rel_rec}: failed to open {exp_ref_rel}: {exc}")

        thumb_file = root / exp_thumb_rel
        if not thumb_file.is_file() or thumb_file.is_symlink():
            errors.append(f"{rel_rec}: missing or symlink derivative {exp_thumb_rel}")
        else:
            actual_sha, actual_size = _file_sha256_and_size(thumb_file)
            if actual_sha != thumb_meta.get("sha256"):
                errors.append(
                    f"{rel_rec}: {exp_thumb_rel} sha256 mismatch (expected {thumb_meta.get('sha256')}, got {actual_sha})"
                )
            if actual_size != thumb_meta.get("size") or actual_size != thumb_meta.get("bytes"):
                errors.append(
                    f"{rel_rec}: {exp_thumb_rel} size mismatch (file={actual_size}, size={thumb_meta.get('size')}, bytes={thumb_meta.get('bytes')})"
                )
            if check_images:
                try:
                    with Image.open(thumb_file) as im:
                        if im.format != "WEBP" or im.mode != "RGB":
                            errors.append(
                                f"{rel_rec}: {exp_thumb_rel} must be RGB WEBP (got {im.format} {im.mode})"
                            )
                        if max(im.size) > 512:
                            errors.append(f"{rel_rec}: {exp_thumb_rel} exceeds 512px ({im.size})")
                        if im.size != (thumb_meta.get("width"), thumb_meta.get("height")):
                            errors.append(
                                f"{rel_rec}: {exp_thumb_rel} dimensions {im.size} != metadata {(thumb_meta.get('width'), thumb_meta.get('height'))}"
                            )
                except Exception as exc:
                    errors.append(f"{rel_rec}: failed to open {exp_thumb_rel}: {exc}")

    if ref_dir.is_dir():
        actual_refs = {p.name for p in ref_dir.iterdir() if p.is_file()}
        for orphan in sorted(actual_refs - expected_refs):
            errors.append(f"Orphan derivative file: assets/ref2048/{orphan}")

    if thumb_dir.is_dir():
        actual_thumbs = {p.name for p in thumb_dir.iterdir() if p.is_file()}
        for orphan in sorted(actual_thumbs - expected_thumbs):
            errors.append(f"Orphan derivative file: assets/thumb512/{orphan}")

    for src_catalog, aid in referenced_catalog_assets:
        if aid not in seen_ids:
            errors.append(f"{src_catalog}: references non-existent asset_id '{aid}'")

    return errors


# 命令行入口：执行仓库校验并输出校验统计结果
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the zybc-knowledge repository.")
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parent.parent),
        help="Path to the zybc-knowledge repository root.",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Skip decoding every derivative image with Pillow (still verifies SHA256 and byte sizes).",
    )
    args = parser.parse_args(argv)

    root = Path(args.repo_root).resolve()
    errors = validate_repository(root, check_images=not args.fast)
    if errors:
        print(f"Validation FAILED with {len(errors)} error(s):", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    rec_count = len(list((root / "records").glob("*.yaml")))
    ref_count = len(list((root / "assets" / "ref2048").glob("*.jpg")))
    thumb_count = len(list((root / "assets" / "thumb512").glob("*.webp")))
    print(
        f"Validation PASSED: {rec_count} records, {ref_count} ref2048 images, {thumb_count} thumb512 images."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
