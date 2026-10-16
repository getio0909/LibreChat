from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .config import StoneMaterial

# 标准图库九大分类与角色枚举定义
LIBRARY_TYPES: tuple[str, ...] = (
    "造像雕塑",
    "石刻纹样",
    "法器供器",
    "家具木作",
    "建筑景观",
    "空间展陈",
    "材质纹理",
    "工程图纸",
    "矿山车间",
)
STUDIO_ROLES: tuple[str, ...] = ("参考", "物件", "造型", "材质", "纹样", "风格")
ASSET_ID_RE = re.compile(r"^[0-9a-f]{16}$")
KB_ID_RE = re.compile(r"^kb_([0-9a-f]{16})$")
TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+")


# 构建石材名称或别名的正则匹配表达式（防止“敦煌壁画”误匹配为“敦煌壁”石材）
def _stone_alias_regex(alias: str) -> str:
    return re.escape(alias) + ("(?!画)" if alias == "敦煌壁" else "")


# 判断检索词元是否命中目标文本（防止“敦煌壁”词元误匹配“敦煌壁画”）
def _token_matches(token: str, text: str) -> bool:
    if token == "敦煌壁":
        return re.search(_stone_alias_regex(token), text) is not None
    return token in text


# 将长摘要压缩并截断至最多120个字符
def truncate_summary(text: str, max_chars: int = 120) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 1].rstrip() + "…"


# 内存中的单条知识库资产记录结构
@dataclass(frozen=True)
class KBRecord:
    id: str
    kb_id: str
    title_zh: str
    library_type: str
    studio_roles: tuple[str, ...]
    category_l1: str
    category_l2: str
    entity_type: str
    summary_zh: str
    short_summary: str
    materials: tuple[str, ...]
    material_ids: tuple[str, ...]
    expanded_materials: tuple[str, ...]
    colors: tuple[str, ...]
    style: str
    tags: tuple[str, ...]
    ai_design_usage: str
    licence: str
    licence_status: str
    review_status: str
    low_res: bool
    ai_generated: bool
    third_party_watermark: bool
    text_overlay: bool
    ref2048_path: Path
    thumb512_path: Path
    ref_width: int
    ref_height: int
    search_title: str
    search_primary: str
    search_secondary: str
    search_summary: str
    search_all: str
    is_catalog_sample: bool = False
    is_catalog_slab: bool = False
    thumb_sha256: str = ""

    # 返回导致默认排除的质量或审核标记名称元组
    @property
    def exclusion_flags(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.ai_generated:
            reasons.append("ai_generated")
        if self.third_party_watermark:
            reasons.append("third_party_watermark")
        if self.low_res:
            reasons.append("low_res")
        if self.review_status == "rejected":
            reasons.append("rejected")
        return tuple(reasons)

    # 判断该资产是否带有默认排除的质量或审核标记
    @property
    def is_flagged(self) -> bool:
        return bool(self.exclusion_flags)

    # 生成search_library工具返回的单条公开元数据字典
    def to_search_dict(self) -> dict[str, Any]:
        return {
            "id": self.kb_id,
            "title": self.title_zh,
            "library_type": self.library_type,
            "roles": list(self.studio_roles),
            "summary": self.short_summary,
            "materials": list(self.materials),
            "licence": self.licence,
            "licence_status": self.licence_status,
            "review_status": self.review_status,
            "flags": {
                "low_res": self.low_res,
                "ai_generated": self.ai_generated,
                "third_party_watermark": self.third_party_watermark,
                "text_overlay": self.text_overlay,
                "rejected": self.review_status == "rejected",
            },
        }


# 启动时构建的内存知识库索引与石材目录解析器
class KnowledgeBaseIndex:
    def __init__(
        self,
        kb_root: Path,
        records: dict[str, KBRecord],
        materials: list[StoneMaterial],
    ) -> None:
        self.kb_root = kb_root
        self.records = records
        self.materials = materials
        self._split_terms = self._build_split_terms(materials)

    # 构建用于拆分复合中文查询词的已知石材名、分类名与角色词表
    @staticmethod
    def _build_split_terms(materials: list[StoneMaterial]) -> list[str]:
        terms: set[str] = set(LIBRARY_TYPES) | set(STUDIO_ROLES)
        for mat in materials:
            for val in (mat.name, *mat.aliases, *mat.search_terms):
                if val:
                    terms.add(val.casefold())
        return sorted(terms, key=len, reverse=True)

    # 将用户查询字符串拆分为确定性检索词元列表
    def _tokenize_query(self, query: str) -> list[str]:
        lowered = query.strip().casefold()
        if not lowered:
            return []
        raw_tokens = TOKEN_RE.findall(lowered)
        if not raw_tokens:
            return []

        expanded: list[str] = []
        for tok in raw_tokens:
            remaining = tok
            sub_tokens: list[str] = []
            for known in self._split_terms:
                if not known or known == remaining:
                    continue
                pattern = _stone_alias_regex(known)
                if re.search(pattern, remaining):
                    sub_tokens.append(known)
                    remaining = re.sub(pattern, " ", remaining)
            if sub_tokens:
                leftovers = [p for p in remaining.split() if p]
                for item in (*sub_tokens, *leftovers):
                    if item not in expanded:
                        expanded.append(item)
            elif tok not in expanded:
                expanded.append(tok)
        return expanded

    # 按资产ID（支持16位hex或kb_<hex>前缀）获取知识库记录
    def get(self, asset_or_kb_id: str) -> KBRecord | None:
        cleaned = asset_or_kb_id.strip()
        if cleaned.startswith("kb_"):
            match = KB_ID_RE.fullmatch(cleaned)
            if match is None:
                return None
            cleaned = match.group(1)
        elif not ASSET_ID_RE.fullmatch(cleaned):
            return None
        return self.records.get(cleaned)

    # 执行确定性字段与关键词匹配检索，默认排除带质量或驳回标记的记录
    def search(
        self,
        query: str,
        *,
        library_type: str | None = None,
        role: str | None = None,
        limit: int = 6,
        include_flagged: bool = False,
    ) -> list[KBRecord]:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 12
        ):
            raise ValueError("limit must be an integer between 1 and 12")
        clean_type = (library_type or "").strip()
        if clean_type and clean_type not in LIBRARY_TYPES:
            raise ValueError(f"Invalid library_type: {clean_type}")
        clean_role = (role or "").strip()
        if clean_role and clean_role not in STUDIO_ROLES:
            raise ValueError(f"Invalid role: {clean_role}")

        clean_query = str(query or "").strip()
        tokens = self._tokenize_query(clean_query)
        if clean_query and not tokens:
            return []

        stone_terms = {
            term.casefold()
            for mat in self.materials
            for term in (mat.id, mat.name, *mat.aliases, *mat.search_terms)
            if term
        }
        has_stone_token = any(t in stone_terms for t in tokens)

        ranked: list[tuple[int, str, KBRecord]] = []
        for rec in self.records.values():
            if not include_flagged and rec.is_flagged:
                continue
            if clean_type and rec.library_type != clean_type:
                continue
            if clean_role and clean_role not in rec.studio_roles:
                continue
            if tokens and not all(_token_matches(t, rec.search_all) for t in tokens):
                continue

            score = 0
            for t in tokens:
                if _token_matches(t, rec.search_title):
                    score += 10
                if _token_matches(t, rec.search_primary):
                    score += 8
                if _token_matches(t, rec.search_secondary):
                    score += 4
                if _token_matches(t, rec.search_summary):
                    score += 2
            if rec.material_ids and (has_stone_token or not tokens):
                score += 20
            if rec.is_catalog_sample:
                score += 5
            elif rec.is_catalog_slab:
                score += 2

            ranked.append((score, rec.id, rec))

        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [item[2] for item in ranked[:limit]]

    # 返回所有已知石材目录名称及别名的可读汇总字符串
    def format_known_materials(self) -> str:
        parts: list[str] = []
        for mat in self.materials:
            if mat.aliases:
                parts.append(f"{mat.name} ({'/'.join(mat.aliases)})")
            else:
                parts.append(mat.name)
        return ", ".join(parts)

    # 将石材名称、别名或ID解析为石材目录条目，未知名称抛出包含已知石材列表的明确错误
    def resolve_material(self, name_or_alias: str) -> StoneMaterial:
        cleaned = str(name_or_alias or "").strip()
        if not cleaned:
            raise ValueError(
                f"Material name must not be empty. Known materials: {self.format_known_materials()}"
            )
        folded = cleaned.casefold()
        stripped = re.sub(
            r"(大理石系列|大理石|透光石|大板|石材|系列)$", "", folded
        ).strip()

        for mat in self.materials:
            candidates = [mat.id, mat.name, *mat.aliases]
            folded_candidates = [c.casefold() for c in candidates if c]
            if folded in folded_candidates or (
                stripped and stripped in folded_candidates
            ):
                return mat

        raise ValueError(
            f"Unknown material '{cleaned}'. Known materials: {self.format_known_materials()}"
        )


# 从/kb/catalog/materials.yaml加载石材目录，若不存在则回退至rules中的石材配置
def _load_catalog_materials(
    kb_root: Path, fallback_materials: list[StoneMaterial] | None = None
) -> list[StoneMaterial]:
    mat_file = kb_root / "catalog" / "materials.yaml"
    if mat_file.exists():
        if mat_file.is_symlink():
            raise ValueError("KB catalog/materials.yaml must not be a symlink")
        raw = yaml.safe_load(mat_file.read_text(encoding="utf-8")) or {}
        if isinstance(raw, dict) and isinstance(raw.get("materials"), list):
            return [
                StoneMaterial.model_validate(item)
                for item in raw["materials"]
                if isinstance(item, dict)
            ]
    return list(fallback_materials or [])


# 启动时扫描/kb/records与/kb/catalog构建内存知识库索引（不导入knowledge/tools）
def load_kb_index(
    kb_root: str | Path,
    fallback_materials: list[StoneMaterial] | None = None,
) -> KnowledgeBaseIndex:
    root = Path(kb_root)
    if root.is_symlink():
        raise ValueError("KB root must not be a symlink")
    if not root.exists():
        return KnowledgeBaseIndex(root, {}, list(fallback_materials or []))
    if not root.is_dir():
        raise ValueError("KB root must be a directory")

    materials = _load_catalog_materials(root, fallback_materials)
    materials_by_id: dict[str, StoneMaterial] = {m.id: m for m in materials}
    sample_ids_to_mat: dict[str, str] = {}
    slab_ids_to_mat: dict[str, str] = {}
    for mat in materials:
        for sid in mat.sample_asset_ids:
            sample_ids_to_mat[sid] = mat.id
        for aid in mat.all_slab_asset_ids:
            slab_ids_to_mat[aid] = mat.id

    records_dir = root / "records"
    records: dict[str, KBRecord] = {}
    if not records_dir.exists():
        return KnowledgeBaseIndex(root, records, materials)
    if records_dir.is_symlink() or not records_dir.is_dir():
        raise ValueError("KB records directory is invalid")

    for rec_path in sorted(records_dir.glob("*.yaml")):
        if rec_path.is_symlink() or not rec_path.is_file():
            continue
        rec_id = rec_path.stem
        if not ASSET_ID_RE.fullmatch(rec_id):
            continue
        try:
            data = yaml.safe_load(rec_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict) or data.get("id") != rec_id:
            continue

        title_zh = str(data.get("title_zh") or "").strip()
        library_type = str(data.get("library_type") or "").strip()
        studio_roles = tuple(
            str(r).strip() for r in (data.get("studio_roles") or []) if str(r).strip()
        )
        category_l1 = str(data.get("category_l1") or "").strip()
        category_l2 = str(data.get("category_l2") or "").strip()
        entity_type = str(data.get("entity_type") or "").strip()
        summary_zh = str(data.get("summary_zh") or "").strip()
        short_summary = truncate_summary(summary_zh, 120)
        raw_materials = tuple(
            str(m).strip() for m in (data.get("materials") or []) if str(m).strip()
        )
        raw_mat_ids = [
            str(mid).strip()
            for mid in (data.get("material_ids") or [])
            if str(mid).strip()
        ]
        if rec_id in sample_ids_to_mat and sample_ids_to_mat[rec_id] not in raw_mat_ids:
            raw_mat_ids.append(sample_ids_to_mat[rec_id])
        if rec_id in slab_ids_to_mat and slab_ids_to_mat[rec_id] not in raw_mat_ids:
            raw_mat_ids.append(slab_ids_to_mat[rec_id])

        colors = tuple(
            str(c).strip() for c in (data.get("colors") or []) if str(c).strip()
        )
        style_raw = data.get("style")
        if isinstance(style_raw, list):
            style = " / ".join(str(s).strip() for s in style_raw if str(s).strip())
        else:
            style = str(style_raw or "").strip()
        tags = tuple(str(t).strip() for t in (data.get("tags") or []) if str(t).strip())
        ai_design_usage = str(data.get("ai_design_usage") or "").strip()

        # 仅当记录未显式绑定石材ID且不属于目录样图/大板时，按石材名称或别名（排除“敦煌壁画”误匹配）推断关联石材
        if not raw_mat_ids:
            combined_mat_text = " ".join((title_zh, *raw_materials, *tags, category_l1))
            for mat in materials:
                if mat.id not in raw_mat_ids and any(
                    alias
                    and re.search(_stone_alias_regex(alias), combined_mat_text)
                    is not None
                    for alias in (mat.name, *mat.aliases)
                ):
                    raw_mat_ids.append(mat.id)

        material_ids = tuple(raw_mat_ids)
        expanded_set: list[str] = list(raw_materials)
        for mid in material_ids:
            m_obj = materials_by_id.get(mid)
            if m_obj is not None:
                for term in (
                    m_obj.id,
                    m_obj.name,
                    *m_obj.aliases,
                    *m_obj.search_terms,
                    m_obj.group,
                ):
                    if term and term not in expanded_set:
                        expanded_set.append(term)
        expanded_materials = tuple(expanded_set)

        prov = (
            data.get("provenance") if isinstance(data.get("provenance"), dict) else {}
        )
        licence = str(prov.get("license") or "unknown").strip()
        licence_status = str(prov.get("license_status") or "unconfirmed").strip()

        qual = data.get("quality") if isinstance(data.get("quality"), dict) else {}
        low_res = bool(qual.get("low_res", False))
        ai_generated = bool(qual.get("ai_generated", False))
        third_party_watermark = bool(qual.get("third_party_watermark", False))
        text_overlay = bool(qual.get("text_overlay", False))

        rev = data.get("review") if isinstance(data.get("review"), dict) else {}
        review_status = str(rev.get("status") or "draft").strip()

        derivs = (
            data.get("derivatives") if isinstance(data.get("derivatives"), dict) else {}
        )
        ref_meta = (
            derivs.get("ref2048") if isinstance(derivs.get("ref2048"), dict) else {}
        )
        thumb_meta = (
            derivs.get("thumb512") if isinstance(derivs.get("thumb512"), dict) else {}
        )
        ref_width = int(ref_meta.get("width") or 0)
        ref_height = int(ref_meta.get("height") or 0)

        ref2048_path = root / "assets" / "ref2048" / f"{rec_id}.jpg"
        thumb512_path = root / "assets" / "thumb512" / f"{rec_id}.webp"
        thumb_sha256 = str(thumb_meta.get("sha256") or "").strip().lower()
        if thumb512_path.is_file() and not thumb512_path.is_symlink():
            try:
                thumb_sha256 = hashlib.sha256(thumb512_path.read_bytes()).hexdigest()
            except OSError:
                pass

        search_title = title_zh.casefold()
        search_primary = " ".join(
            (*raw_materials, *expanded_materials, *tags)
        ).casefold()
        search_secondary = " ".join(
            (
                library_type,
                category_l1,
                category_l2,
                entity_type,
                style,
                *colors,
                *studio_roles,
            )
        ).casefold()
        search_summary = f"{summary_zh} {ai_design_usage}".casefold()
        search_all = " ".join(
            (
                rec_id,
                f"kb_{rec_id}",
                search_title,
                search_primary,
                search_secondary,
                search_summary,
            )
        )

        records[rec_id] = KBRecord(
            id=rec_id,
            kb_id=f"kb_{rec_id}",
            title_zh=title_zh,
            library_type=library_type,
            studio_roles=studio_roles,
            category_l1=category_l1,
            category_l2=category_l2,
            entity_type=entity_type,
            summary_zh=summary_zh,
            short_summary=short_summary,
            materials=raw_materials,
            material_ids=material_ids,
            expanded_materials=expanded_materials,
            colors=colors,
            style=style,
            tags=tags,
            ai_design_usage=ai_design_usage,
            licence=licence,
            licence_status=licence_status,
            review_status=review_status,
            low_res=low_res,
            ai_generated=ai_generated,
            third_party_watermark=third_party_watermark,
            text_overlay=text_overlay,
            ref2048_path=ref2048_path,
            thumb512_path=thumb512_path,
            ref_width=ref_width,
            ref_height=ref_height,
            search_title=search_title,
            search_primary=search_primary,
            search_secondary=search_secondary,
            search_summary=search_summary,
            search_all=search_all,
            is_catalog_sample=(rec_id in sample_ids_to_mat),
            is_catalog_slab=(rec_id in slab_ids_to_mat),
            thumb_sha256=thumb_sha256,
        )

    return KnowledgeBaseIndex(root, records, materials)
