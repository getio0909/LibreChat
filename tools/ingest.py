# 新资产入库与衍生图生成脚本
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
from pathlib import Path
from typing import Any

import yaml
from PIL import Image, ImageCms, ImageOps

# 允许加载超过默认像素上限的高分辨率原图
Image.MAX_IMAGE_PIXELS = 120_000_000

# 审计报告D节定义的Gemini视觉标注结构化JSON输出模式
GEMINI_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "title_zh",
        "entity_type",
        "library_type",
        "roles",
        "category_l1",
        "category_l2",
        "summary_zh",
        "visible_text_ocr",
        "materials",
        "colors",
        "color_hex_palette",
        "style",
        "ai_design_usage",
        "style_take",
        "caution",
        "tags",
        "detected_stone_hint",
        "is_ai_generated_suspect",
        "watermark_detected",
        "watermark_text",
        "confidence",
        "needs_review",
        "review_reason",
    ],
    "properties": {
        "title_zh": {
            "type": "string",
            "description": "Concise factual Chinese title (8-22 chars) describing only what is visible.",
        },
        "entity_type": {
            "type": "string",
            "enum": ["实物对象写实", "空间场景设计", "工程设计图纸"],
        },
        "library_type": {
            "type": "string",
            "enum": [
                "造像雕塑",
                "石刻纹样",
                "法器供器",
                "家具木作",
                "建筑景观",
                "空间展陈",
                "材质纹理",
                "工程图纸",
                "矿山车间",
            ],
        },
        "roles": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["参考", "物件", "造型", "材质", "纹样", "风格"],
            },
            "minItems": 1,
            "maxItems": 3,
        },
        "category_l1": {
            "type": "string",
            "enum": [
                "自家核心石材大板",
                "常用石材大板",
                "佛事展展位方案",
                "传统与跨文化风格",
                "佛教家具与陈设",
                "佛堂空间参考",
                "佛教空间案例",
                "佛教造像与法器",
            ],
        },
        "category_l2": {
            "type": "string",
            "enum": [
                "奢石大板样本",
                "常用大理石样本",
                "矿山与车间场景",
                "佛像造像",
                "菩萨与护法造像",
                "法器供具",
                "灯具与幢幡",
                "石雕浮雕与饰板",
                "须弥座与栏杆构件",
                "木雕与藻井构件",
                "镂空石屏与花窗",
                "汉式嵌石家具",
                "传统实木供桌与条案",
                "藏式雕花家具",
                "现代佛龛柜",
                "法座宝座",
                "传统寺院大殿",
                "现代佛教展厅",
                "现代奢石万佛堂",
                "现代禅修茶室",
                "中式居家与会所佛堂",
                "寺院建筑与园林景观",
                "展览工程图纸",
            ],
        },
        "summary_zh": {
            "type": "string",
            "description": "60-140 chars factual Chinese visual description. Describe ONLY visible elements.",
        },
        "visible_text_ocr": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Exact transcription of any visible plaques, dimensions, prices, or watermarks. Empty array if none.",
        },
        "materials": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 6,
        },
        "colors": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 5,
        },
        "color_hex_palette": {
            "type": "array",
            "items": {"type": "string", "pattern": "^#[0-9a-fA-F]{6}$"},
            "minItems": 2,
            "maxItems": 4,
        },
        "style": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 3,
        },
        "ai_design_usage": {
            "type": "string",
            "description": "Concrete studio usage (e.g. texture replacement, object placement, layout reference).",
        },
        "style_take": {
            "type": "string",
            "description": "What visual/craft elements to borrow vs. what background/context elements NOT to copy.",
        },
        "caution": {
            "type": "string",
            "description": "Religious, structural, material, or watermark cautions for designers.",
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 4,
            "maxItems": 8,
        },
        "detected_stone_hint": {
            "type": ["string", "null"],
            "enum": ["dunhuang-gold", "dunhuang-wall", "gold-jade", "blue-gold-grey", None],
        },
        "is_ai_generated_suspect": {"type": "boolean"},
        "watermark_detected": {"type": "boolean"},
        "watermark_text": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "needs_review": {"type": "boolean"},
        "review_reason": {"type": "string"},
    },
}


# 计算文件或字节流的完整SHA256十六进制摘要
def compute_sha256(data_or_path: bytes | Path | str) -> str:
    if isinstance(data_or_path, (Path, str)):
        raw_bytes = Path(data_or_path).read_bytes()
    else:
        raw_bytes = data_or_path
    return hashlib.sha256(raw_bytes).hexdigest()


# 从原始文件SHA256派生稳定的16位小写十六进制资产ID
def derive_asset_id(data_or_path: bytes | Path | str) -> str:
    digest = compute_sha256(data_or_path)
    return digest[:16]


# 对图像执行EXIF方向校正、ICC/CMYK到sRGB转换并剥离元数据
def normalize_image(source: Image.Image) -> Image.Image:
    icc_bytes = source.info.get("icc_profile")
    transposed = ImageOps.exif_transpose(source)
    has_alpha = "A" in transposed.getbands() or "transparency" in transposed.info
    alpha_channel = transposed.convert("RGBA").getchannel("A") if has_alpha else None

    converted = transposed
    if icc_bytes:
        try:
            src_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_bytes))
            space = src_profile.profile.xcolor_space.strip()
            mode_map = {"RGB": "RGB", "CMYK": "CMYK", "GRAY": "L", "LAB": "LAB"}
            target_mode = mode_map.get(space, transposed.mode)
            color_img = transposed if transposed.mode == target_mode else transposed.convert(target_mode)
            srgb_profile = ImageCms.createProfile("sRGB")
            converted = ImageCms.profileToProfile(color_img, src_profile, srgb_profile, outputMode="RGB")
        except (ImageCms.PyCMSError, OSError, ValueError):
            converted = transposed.convert("RGB")
    elif converted.mode != "RGB":
        converted = converted.convert("RGB")

    if alpha_channel is not None:
        rgba_img = converted.convert("RGBA")
        rgba_img.putalpha(alpha_channel)
        white_bg = Image.new("RGBA", rgba_img.size, (255, 255, 255, 255))
        converted = Image.alpha_composite(white_bg, rgba_img).convert("RGB")

    clean_img = Image.new("RGB", converted.size)
    clean_img.putdata(list(converted.getdata())) if converted.size[0] * converted.size[1] <= 1024 else clean_img.paste(converted)
    return clean_img


# 生成ref2048和thumb512衍生图并返回原图与衍生图的元数据字典
def generate_derivatives(
    source_path: Path | str,
    repo_root: Path | str,
    original_rel_path: str | None = None,
) -> dict[str, Any]:
    src = Path(source_path)
    root = Path(repo_root)
    raw_bytes = src.read_bytes()
    orig_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    asset_id = orig_sha256[:16]

    with Image.open(io.BytesIO(raw_bytes)) as img:
        orig_format = img.format or "JPEG"
        orig_mode = img.mode
        orig_width, orig_height = img.size
        normalized = normalize_image(img)

    ref_dir = root / "assets" / "ref2048"
    thumb_dir = root / "assets" / "thumb512"
    ref_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir.mkdir(parents=True, exist_ok=True)

    ref_img = normalized.copy()
    ref_img.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
    ref_rel_path = f"assets/ref2048/{asset_id}.jpg"
    ref_abs_path = root / ref_rel_path
    ref_img.save(ref_abs_path, format="JPEG", quality=85)
    ref_bytes = ref_abs_path.read_bytes()
    ref_sha256 = hashlib.sha256(ref_bytes).hexdigest()

    thumb_img = normalized.copy()
    thumb_img.thumbnail((512, 512), Image.Resampling.LANCZOS)
    thumb_rel_path = f"assets/thumb512/{asset_id}.webp"
    thumb_abs_path = root / thumb_rel_path
    thumb_img.save(thumb_abs_path, format="WEBP", quality=80)
    thumb_bytes = thumb_abs_path.read_bytes()
    thumb_sha256 = hashlib.sha256(thumb_bytes).hexdigest()

    rel_path_str = original_rel_path if original_rel_path else src.name
    if rel_path_str.startswith("wiki/"):
        rel_path_str = rel_path_str[len("wiki/") :]

    return {
        "id": asset_id,
        "source": {
            "original_path": rel_path_str,
        },
        "original": {
            "path": rel_path_str,
            "sha256": orig_sha256,
            "bytes": len(raw_bytes),
            "width": orig_width,
            "height": orig_height,
            "format": orig_format,
            "color_mode": orig_mode,
        },
        "derivatives": {
            "ref2048": {
                "path": ref_rel_path,
                "sha256": ref_sha256,
                "size": len(ref_bytes),
                "bytes": len(ref_bytes),
                "width": ref_img.width,
                "height": ref_img.height,
            },
            "thumb512": {
                "path": thumb_rel_path,
                "sha256": thumb_sha256,
                "size": len(thumb_bytes),
                "bytes": len(thumb_bytes),
                "width": thumb_img.width,
                "height": thumb_img.height,
            },
        },
    }


# 为新入库图片创建待Gemini标注的草稿YAML记录
def create_draft_record(
    source_path: Path | str,
    repo_root: Path | str,
    original_rel_path: str | None = None,
    provenance_source: str = "unknown",
) -> Path:
    root = Path(repo_root)
    meta = generate_derivatives(source_path, root, original_rel_path=original_rel_path)
    asset_id = meta["id"]
    orig = meta["original"]
    min_side = min(orig["width"], orig["height"])
    is_low_res = min_side < 800

    issues: list[str] = ["pending_gemini_visual_labeling"]
    if is_low_res:
        issues.append(f"low_res: {orig['width']}x{orig['height']} (<800px short side)")

    record: dict[str, Any] = {
        "id": asset_id,
        "title_zh": f"待标注资产-{asset_id[:8]}",
        "source": meta["source"],
        "original": orig,
        "derivatives": meta["derivatives"],
        "library_type": "空间展陈",
        "studio_roles": ["参考"],
        "category_l1": "待分类",
        "category_l2": "待分类",
        "entity_type": "空间场景设计",
        "summary_zh": "待Gemini视觉标注流程填充描述。",
        "materials": ["待标注"],
        "colors": ["待标注"],
        "style": "待标注",
        "ai_design_usage": "待标注",
        "prompt_zh": "待标注",
        "tags": ["待标注"],
        "material_ids": [],
        "provenance": {
            "source": provenance_source,
            "url": None,
            "credit": None,
            "license": "unknown",
            "license_status": "unconfirmed",
        },
        "quality": {
            "min_side_px": min_side,
            "low_res": is_low_res,
            "ai_generated": False,
            "third_party_watermark": False,
            "text_overlay": False,
            "notes": "Draft record created by tools/ingest.py; awaiting visual inspection.",
        },
        "review": {
            "status": "draft",
            "reviewer": None,
            "reviewed_at": None,
            "issues": issues,
        },
    }

    records_dir = root / "records"
    records_dir.mkdir(parents=True, exist_ok=True)
    record_path = records_dir / f"{asset_id}.yaml"
    record_path.write_text(
        yaml.safe_dump(record, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )
    return record_path


# 解析命令行参数并执行单张或多张图片的草稿入库
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest new images into zybc-knowledge (generates ref2048/thumb512 and a draft record)."
    )
    parser.add_argument(
        "images",
        nargs="*",
        help="Path(s) to source image files to ingest.",
    )
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parent.parent),
        help="Path to the zybc-knowledge repository root.",
    )
    parser.add_argument(
        "--source",
        default="unknown",
        choices=["own_product", "own_project", "web", "museum", "ai_generated", "unknown"],
        help="Provenance source type for the ingested assets.",
    )
    parser.add_argument(
        "--print-gemini-schema",
        action="store_true",
        help="Print the Gemini structured output JSON schema and exit.",
    )
    args = parser.parse_args(argv)

    if args.print_gemini_schema:
        print(json.dumps(GEMINI_RESPONSE_SCHEMA, ensure_ascii=False, indent=2))
        return 0

    if not args.images:
        parser.error("Provide at least one image path or pass --print-gemini-schema.")

    repo_root = Path(args.repo_root).resolve()
    for img_arg in args.images:
        img_path = Path(img_arg).resolve()
        if not img_path.is_file():
            print(f"Error: File not found: {img_path}", file=sys.stderr)
            return 1
        rec_path = create_draft_record(
            img_path,
            repo_root,
            original_rel_path=img_path.name,
            provenance_source=args.source,
        )
        print(f"Created draft record: {rec_path.relative_to(repo_root)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
