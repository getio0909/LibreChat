# 知识库构建脚本：生成检索索引、分类视图与原图归档包
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

# 定义九大标准图库分类顺序
LIBRARY_TYPES: list[str] = [
    "造像雕塑",
    "石刻纹样",
    "法器供器",
    "家具木作",
    "建筑景观",
    "空间展陈",
    "材质纹理",
    "工程图纸",
    "矿山车间",
]


# 加载石材目录以构建石材ID到名称和别名的映射
def _load_materials_map(repo_root: Path) -> dict[str, dict[str, Any]]:
    mat_path = repo_root / "catalog" / "materials.yaml"
    if not mat_path.is_file():
        return {}
    data = yaml.safe_load(mat_path.read_text(encoding="utf-8")) or {}
    result: dict[str, dict[str, Any]] = {}
    for item in data.get("materials") or []:
        mid = item.get("id")
        if mid:
            result[mid] = item
    return result


# 读取并按资产ID排序所有YAML记录
def load_all_records(repo_root: Path) -> list[dict[str, Any]]:
    records_dir = repo_root / "records"
    records: list[dict[str, Any]] = []
    for rec_path in sorted(records_dir.glob("*.yaml")):
        rec = yaml.safe_load(rec_path.read_text(encoding="utf-8"))
        if isinstance(rec, dict):
            records.append(rec)
    records.sort(key=lambda r: str(r.get("id", "")))
    return records


# 构建供工作台与检索服务使用的JSONL索引文件
def build_index_jsonl(
    repo_root: Path,
    records: list[dict[str, Any]],
    dist_dir: Path,
) -> Path:
    materials_map = _load_materials_map(repo_root)
    dist_dir.mkdir(parents=True, exist_ok=True)
    index_path = dist_dir / "index.jsonl"

    lines: list[str] = []
    for rec in records:
        material_names: list[str] = []
        for mid in rec.get("material_ids") or []:
            m_info = materials_map.get(mid)
            if m_info:
                if m_info.get("name"):
                    material_names.append(m_info["name"])
                material_names.extend(m_info.get("aliases") or [])

        entry = {
            "id": rec["id"],
            "title_zh": rec["title_zh"],
            "source": rec["source"],
            "original": rec["original"],
            "derivatives": rec["derivatives"],
            "library_type": rec["library_type"],
            "region": rec["region"],
            "studio_roles": rec["studio_roles"],
            "category_l1": rec["category_l1"],
            "category_l2": rec["category_l2"],
            "entity_type": rec["entity_type"],
            "summary_zh": rec["summary_zh"],
            "materials": rec["materials"],
            "colors": rec["colors"],
            "style": rec["style"],
            "ai_design_usage": rec["ai_design_usage"],
            "prompt_zh": rec["prompt_zh"],
            "prompt_en": rec.get("prompt_en", ""),
            "tags": rec.get("tags", []),
            "material_ids": rec["material_ids"],
            "material_aliases": sorted(set(material_names)),
            "provenance": rec["provenance"],
            "quality": rec["quality"],
            "review": rec["review"],
        }
        lines.append(json.dumps(entry, ensure_ascii=False))

    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return index_path


# 按图库分类生成无重复、仅含相对路径的Markdown视图文件
def build_markdown_views(
    records: list[dict[str, Any]],
    dist_dir: Path,
) -> list[Path]:
    views_dir = dist_dir / "views"
    if views_dir.exists():
        shutil.rmtree(views_dir)
    views_dir.mkdir(parents=True, exist_ok=True)

    grouped: dict[str, list[dict[str, Any]]] = {lt: [] for lt in LIBRARY_TYPES}
    for rec in records:
        lt = rec["library_type"]
        grouped.setdefault(lt, []).append(rec)

    written_files: list[Path] = []
    for lt in LIBRARY_TYPES:
        items = grouped.get(lt, [])
        md_path = views_dir / f"{lt}.md"
        out: list[str] = [
            f"# {lt} ({len(items)})",
            "",
            f"> Generated view for library_type `{lt}`. Each asset appears in exactly one view.",
            "",
        ]
        for rec in items:
            aid = rec["id"]
            title = rec["title_zh"]
            orig = rec["original"]
            qual = rec["quality"]
            prov = rec["provenance"]
            roles_str = ", ".join(rec.get("studio_roles") or [])
            mats_str = ", ".join(rec.get("materials") or [])
            colors_str = ", ".join(rec.get("colors") or [])
            mat_ids_str = ", ".join(rec.get("material_ids") or []) or "none"
            flags: list[str] = []
            if qual.get("low_res"):
                flags.append("low_res")
            if qual.get("ai_generated"):
                flags.append("ai_generated")
            if qual.get("third_party_watermark"):
                flags.append("third_party_watermark")
            if qual.get("text_overlay"):
                flags.append("text_overlay")
            flags_str = ", ".join(flags) if flags else "clean"

            out.extend(
                [
                    f"## {aid} — {title}",
                    "",
                    f"![{title}](../../assets/thumb512/{aid}.webp)",
                    "",
                    f"- **ID**: `{aid}`",
                    f"- **Record**: `records/{aid}.yaml`",
                    f"- **Ref2048**: `assets/ref2048/{aid}.jpg`",
                    f"- **Thumb512**: `assets/thumb512/{aid}.webp`",
                    f"- **Original Path**: `{orig['path']}` (`{orig['width']}x{orig['height']}`, `{orig['color_mode']}`, `{orig['bytes']}` bytes)",
                    f"- **Library Type**: `{lt}` | **Studio Roles**: `{roles_str}` | **Entity Type**: `{rec['entity_type']}`",
                    f"- **Category**: `{rec['category_l1']}` / `{rec['category_l2']}`",
                    f"- **Cultural Region**: `{rec['region']}`",
                    f"- **Catalog Stones (`material_ids`)**: `{mat_ids_str}`",
                    f"- **Materials**: {mats_str}",
                    f"- **Colors**: {colors_str}",
                    f"- **Style**: {rec['style']}",
                    f"- **AI Design Usage**: {rec['ai_design_usage']}",
                    f"- **Provenance**: `{prov['source']}` | license=`{prov['license']}` (`{prov['license_status']}`)",
                    f"- **Quality Flags**: `{flags_str}` (min_side={qual['min_side_px']}px) — {qual['notes']}",
                    f"- **Summary**: {rec['summary_zh']}",
                    "",
                ]
            )
        md_path.write_text("\n".join(out), encoding="utf-8")
        written_files.append(md_path)

    return written_files


# 打包只读源目录中的原始图像文件与SHA256清单为归档压缩包
def build_originals_archive(
    records: list[dict[str, Any]],
    dist_dir: Path,
    wiki_root: Path,
    delta: bool = False,
) -> Path:
    dist_dir.mkdir(parents=True, exist_ok=True)
    manifest_entries: list[dict[str, Any]] = []
    sha256_lines: list[str] = []

    for rec in records:
        orig = rec["original"]
        rel_path = orig["path"]
        src_file = wiki_root / rel_path
        if not src_file.is_file():
            raise FileNotFoundError(
                f"Original source file missing in wiki root: {src_file}"
            )
        if (
            Path(rel_path).is_absolute()
            or ".." in Path(rel_path).parts
            or not src_file.resolve().is_relative_to(wiki_root.resolve())
        ):
            raise ValueError("Original archive path escapes wiki root")
        raw = src_file.read_bytes()
        if (
            len(raw) != orig["bytes"]
            or hashlib.sha256(raw).hexdigest() != orig["sha256"]
        ):
            raise ValueError(f"Original SHA256/size mismatch: {rec['id']}")
        manifest_entries.append(
            {
                "id": rec["id"],
                "sha256": orig["sha256"],
                "bytes": orig["bytes"],
                "width": orig["width"],
                "height": orig["height"],
                "format": orig["format"],
                "color_mode": orig["color_mode"],
                "path": rel_path,
            }
        )
        sha256_lines.append(f"{orig['sha256']}  {rel_path}")

    prefix = "originals-delta" if delta else "originals"
    manifest_path = dist_dir / f"{prefix}-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": "v1",
                "kind": "delta" if delta else "full",
                "count": len(manifest_entries),
                "files": manifest_entries,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    sums_path = dist_dir / ("originals-delta-SHA256SUMS" if delta else "SHA256SUMS")
    sums_path.write_text("\n".join(sha256_lines) + "\n", encoding="utf-8")

    use_zstd = shutil.which("zstd") is not None
    archive_name = f"{prefix}-v1.tar.zst" if use_zstd else f"{prefix}-v1.tar.gz"
    archive_path = dist_dir / archive_name

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_p = Path(tmp_dir)
        shutil.copy2(manifest_path, tmp_p / "originals-manifest.json")
        shutil.copy2(sums_path, tmp_p / "SHA256SUMS")
        file_list_path = tmp_p / "files.txt"
        rel_paths = [r["original"]["path"] for r in records]
        file_list_path.write_bytes("\0".join(rel_paths).encode("utf-8") + b"\0")

        cmd: list[str] = ["tar"]
        if use_zstd:
            cmd.extend(["--zstd"])
        else:
            cmd.extend(["-z"])
        cmd.extend(
            [
                "-cf",
                str(archive_path),
                "-C",
                str(tmp_p),
                "originals-manifest.json",
                "SHA256SUMS",
                "-C",
                str(wiki_root),
                "--null",
                "--verbatim-files-from",
                "-T",
                str(file_list_path),
            ]
        )
        subprocess.run(cmd, check=True)

    return archive_path


# 显式选择增量原图集合，拒绝空集合、重复或不存在的资产标识
def select_archive_records(
    records: list[dict[str, Any]], ids: list[str]
) -> list[dict[str, Any]]:
    wanted = set(ids)
    if not wanted or len(wanted) != len(ids):
        raise ValueError("Archive IDs must be nonempty and unique")
    existing = {record["id"] for record in records}
    if not wanted.issubset(existing):
        raise ValueError("Archive IDs include unknown assets")
    return [record for record in records if record["id"] in wanted]


# 命令行入口：执行dist目录下的索引、视图和原图归档构建
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build dist/index.jsonl, dist/views/*.md, and dist/originals-v1.tar.zst."
    )
    parser.add_argument(
        "--archive-ids",
        type=Path,
        help="Text file with one asset ID per line; package only those originals as a delta.",
    )
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parent.parent),
        help="Path to the zybc-knowledge repository root.",
    )
    parser.add_argument(
        "--wiki-root",
        default=str(Path(__file__).resolve().parent.parent.parent / "wiki"),
        help="Read-only path to the original wiki directory for packaging originals.",
    )
    parser.add_argument(
        "--skip-archive",
        action="store_true",
        help="Skip building the originals tar.zst archive.",
    )
    args = parser.parse_args(argv)
    if args.archive_ids and args.skip_archive:
        parser.error("--archive-ids cannot be combined with --skip-archive.")

    repo_root = Path(args.repo_root).resolve()
    dist_dir = repo_root / "dist"
    records = load_all_records(repo_root)
    if not records:
        print("Error: No records found in records/.", file=sys.stderr)
        return 1

    archive_records = records
    if args.archive_ids:
        try:
            archive_records = select_archive_records(
                records, args.archive_ids.read_text(encoding="utf-8").splitlines()
            )
        except (OSError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    index_path = build_index_jsonl(repo_root, records, dist_dir)
    views = build_markdown_views(records, dist_dir)
    print(f"Built {index_path.relative_to(repo_root)} ({len(records)} records)")
    print(f"Built {len(views)} markdown views in dist/views/")

    if not args.skip_archive:
        wiki_root = Path(args.wiki_root).resolve()
        archive_path = build_originals_archive(
            archive_records, dist_dir, wiki_root, delta=bool(args.archive_ids)
        )
        size_mb = archive_path.stat().st_size / (1024 * 1024)
        print(
            f"Built {archive_path.relative_to(repo_root)} ({len(archive_records)} originals, {size_mb:.2f} MiB)"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
