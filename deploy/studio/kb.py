from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .config import StoneMaterial

# 标准图库九大分类、角色与文化地域枚举定义
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
LIBRARY_REGIONS: tuple[str, ...] = (
    "西方",
    "南亚东南亚",
    "中国北方",
    "中国南方",
    "中国藏区",
)
ASSET_ID_RE = re.compile(r"^[0-9a-f]{16}$")
KB_ID_RE = re.compile(r"^kb_([0-9a-f]{16})$")
TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+")
CJK_ONLY_RE = re.compile(r"^[\u4e00-\u9fff]+$")

# 检索查询中应剔除的常见口语前缀、后缀与停用词
QUERY_STOP_PHRASES: tuple[str, ...] = (
    "帮我找几张",
    "帮我找一些",
    "帮我找一张",
    "帮我找",
    "从资料库",
    "资料库里",
    "资料库",
    "知识库",
    "请提供",
    "找几张",
    "找一张",
    "找一些",
    "找几个",
    "来几张",
    "来一张",
    "搜一下",
    "查一下",
    "看几张",
    "参考图片",
    "参考图像",
    "参考素材",
    "参考案例",
    "参考图",
    "参考照",
    "风格参考",
    "造型参考",
    "材质参考",
    "纹样参考",
    "设计参考",
    "设计案例",
    "设计方案",
    "效果图",
    "高清图",
    "示意图",
    "样本图",
    "相关的",
    "类似的",
    "有关的",
)
QUERY_STOP_WORDS: frozenset[str] = frozenset(
    {
        *QUERY_STOP_PHRASES,
        "参考",
        "图片",
        "图像",
        "照片",
        "图库",
        "素材",
        "案例",
        "样图",
        "样本",
        "大图",
        "高清",
        "效果",
        "方案",
        "设计",
        "样式",
        "款式",
        "搜索",
        "查找",
        "查询",
        "检索",
        "看看",
        "提供",
        "相关",
        "类似",
        "有关",
        "关于",
        "的",
        "与",
        "和",
        "及",
        "或",
        "中",
        "里",
        "上",
        "下",
        "有",
        "带",
        "用",
        "做",
        "配",
        "看",
        "找",
        "张",
        "个",
        "种",
        "类",
        "图",
    }
)
QUERY_STOP_CHARS: str = "的与和及或中里上下有带用做配看找张个种类图"

# 佛教空间、石雕木作、建筑构件、器物与纹样领域核心词典（用于无空格中文复合词切分）
DOMAIN_TERMS: tuple[str, ...] = (
    "大雄宝殿",
    "天王殿",
    "观音殿",
    "地藏殿",
    "藏经阁",
    "千佛殿",
    "大佛殿",
    "万佛墙",
    "万佛柱",
    "千佛墙",
    "千佛龛",
    "千佛",
    "须弥座",
    "金刚座",
    "莲花座",
    "仰覆莲",
    "莲座",
    "石座",
    "台座",
    "底座",
    "基座",
    "台基",
    "佛龛",
    "石龛",
    "壁龛",
    "拱龛",
    "藻井",
    "穹顶",
    "平棊",
    "斗拱",
    "梁枋",
    "雀替",
    "柱础",
    "柱套",
    "抱鼓石",
    "门楣",
    "牌坊",
    "山门",
    "照壁",
    "影壁",
    "栏杆",
    "护栏",
    "栏板",
    "望柱",
    "御道",
    "丹陛",
    "石阶",
    "台阶",
    "踏跺",
    "拱桥",
    "水刀拼花",
    "拼花",
    "背景墙",
    "景墙",
    "幕墙",
    "连廊",
    "回廊",
    "格栅",
    "花窗",
    "镂空窗",
    "镂空屏",
    "屏风",
    "隔扇",
    "隔断",
    "吊顶",
    "天花",
    "地面",
    "铺装",
    "平面图",
    "施工图",
    "立面图",
    "节点图",
    "尺寸图",
    "剖面",
    "立面",
    "清水混凝土",
    "汉白玉",
    "大理石",
    "花岗岩",
    "青白玉",
    "青白石",
    "青黑石",
    "红砂岩",
    "黄砂岩",
    "绿砂岩",
    "石灰石",
    "太湖石",
    "透光石",
    "老榆木",
    "金丝楠",
    "胡桃木",
    "铜鎏金",
    "青石",
    "砂岩",
    "奢石",
    "玉石",
    "云石",
    "红玉",
    "墨玉",
    "啡网",
    "板岩",
    "石材",
    "原木",
    "楠木",
    "樟木",
    "红木",
    "乌木",
    "松木",
    "实木",
    "木质",
    "鎏金",
    "古铜",
    "青铜",
    "黄铜",
    "琉璃",
    "金箔",
    "贴金",
    "描金",
    "金漆",
    "朱漆",
    "彩绘",
    "重彩",
    "青灰",
    "深灰",
    "浅灰",
    "米黄",
    "暖金",
    "黑金",
    "红棕",
    "石雕",
    "木雕",
    "铜雕",
    "玉雕",
    "砖雕",
    "泥塑",
    "石刻",
    "高浮雕",
    "深浮雕",
    "浅浮雕",
    "浮雕",
    "透雕",
    "镂空雕",
    "镂空",
    "圆雕",
    "线刻",
    "阴刻",
    "阳刻",
    "雕刻",
    "雕花",
    "石作",
    "木作",
    "大板",
    "荒料",
    "矿山",
    "车间",
    "释迦牟尼",
    "阿弥陀佛",
    "药师佛",
    "三世佛",
    "西方三圣",
    "华严三圣",
    "千手观音",
    "观音菩萨",
    "十八罗汉",
    "四大天王",
    "尊胜宝塔",
    "观音",
    "文殊",
    "普贤",
    "地藏",
    "弥勒",
    "罗汉",
    "韦陀",
    "护法",
    "金刚",
    "飞天",
    "伎乐",
    "力士",
    "菩萨",
    "度母",
    "明王",
    "佛像",
    "大佛",
    "立佛",
    "坐佛",
    "卧佛",
    "佛首",
    "佛头",
    "造像",
    "雕塑",
    "雕像",
    "石像",
    "石狮",
    "瑞兽",
    "麒麟",
    "盘龙",
    "蟠龙",
    "狻猊",
    "佛塔",
    "石塔",
    "宝塔",
    "经幢",
    "香炉",
    "供桌",
    "供案",
    "供坛",
    "佛坛",
    "佛台",
    "条案",
    "经桌",
    "法座",
    "茶桌",
    "茶台",
    "茶室",
    "禅椅",
    "圈椅",
    "坐垫",
    "拜垫",
    "展柜",
    "展台",
    "宝盖",
    "华盖",
    "莲花灯",
    "宫灯",
    "吊灯",
    "法器",
    "供器",
    "家具",
    "敦煌壁画",
    "经变壁画",
    "壁画",
    "缠枝莲",
    "宝相花",
    "莲花纹",
    "莲瓣纹",
    "卷草纹",
    "拐子纹",
    "万字纹",
    "水波纹",
    "八吉祥",
    "莲瓣",
    "莲花",
    "荷花",
    "牡丹",
    "卷草",
    "卷云",
    "祥云",
    "云纹",
    "回纹",
    "宝瓶",
    "八宝",
    "法轮",
    "束腰",
    "花板",
    "新中式",
    "唐宋",
    "唐风",
    "宋式",
    "明清",
    "藏传",
    "藏式",
    "莫卧儿",
    "禅意",
    "传统",
    "现代",
)


# 构建石材名称或别名的正则匹配表达式（防止“敦煌壁画”误匹配为“敦煌壁”石材）
def _stone_alias_regex(alias: str) -> str:
    return re.escape(alias) + ("(?!画)" if alias == "敦煌壁" else "")


# 判断检索词元是否命中目标文本（防止“敦煌壁”词元误匹配“敦煌壁画”）
def _token_matches(token: str, text: str) -> bool:
    if token == "敦煌壁":
        return re.search(_stone_alias_regex(token), text) is not None
    return token in text


# 为长度>=2的中文片段生成重叠二元字符n-gram（跳过停用词与“敦煌壁”）
def _cjk_bigrams(text: str) -> list[str]:
    if len(text) < 2 or not CJK_ONLY_RE.fullmatch(text):
        return []
    grams: list[str] = []
    for idx in range(len(text) - 1):
        bg = text[idx : idx + 2]
        if bg in QUERY_STOP_WORDS or bg == "敦煌壁":
            continue
        if bg not in grams:
            grams.append(bg)
    return grams


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
    region: str
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
            "region": self.region,
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
        self._split_terms = self._build_split_terms(materials, records)

    # 构建用于拆分复合中文查询词的已知石材名、分类名、领域词表与语料短词词典
    @staticmethod
    def _build_split_terms(
        materials: list[StoneMaterial],
        records: dict[str, KBRecord] | None = None,
    ) -> list[str]:
        terms: set[str] = (
            set(LIBRARY_TYPES)
            | set(LIBRARY_REGIONS)
            | set(DOMAIN_TERMS)
            | (set(STUDIO_ROLES) - {"参考"})
        )
        for mat in materials:
            for val in (mat.name, *mat.aliases, *mat.search_terms):
                if val:
                    terms.add(val.casefold())
        if records:
            for rec in records.values():
                for candidate in (
                    rec.category_l1,
                    rec.category_l2,
                    rec.entity_type,
                    *rec.colors,
                ):
                    folded = candidate.strip().casefold()
                    if (
                        2 <= len(folded) <= 3
                        and CJK_ONLY_RE.fullmatch(folded)
                        and folded not in QUERY_STOP_WORDS
                    ):
                        terms.add(folded)
        return sorted(terms, key=len, reverse=True)

    # 使用正向最大匹配（FMM）与停用词表切分单段中文文本
    def _segment_cjk_run(self, run: str) -> tuple[list[str], list[str]]:
        vocab = sorted(
            set(self._split_terms) | set(QUERY_STOP_PHRASES) | set(QUERY_STOP_WORDS),
            key=len,
            reverse=True,
        )
        primary: list[str] = []
        ngrams: list[str] = []
        unmatched_buf: list[str] = []

        def _flush_unmatched() -> None:
            if not unmatched_buf:
                return
            chunk = "".join(unmatched_buf).strip(QUERY_STOP_CHARS)
            unmatched_buf.clear()
            if not chunk or chunk in QUERY_STOP_WORDS:
                return
            if chunk not in primary:
                primary.append(chunk)
            for bg in _cjk_bigrams(chunk):
                if bg not in ngrams and bg not in primary:
                    ngrams.append(bg)

        idx = 0
        n = len(run)
        while idx < n:
            matched_term: str | None = None
            for term in vocab:
                if not term or len(term) < 2:
                    continue
                if not run.startswith(term, idx):
                    continue
                if (
                    term == "敦煌壁"
                    and re.match(_stone_alias_regex(term), run[idx:]) is None
                ):
                    continue
                matched_term = term
                break

            if matched_term is not None:
                _flush_unmatched()
                if matched_term not in QUERY_STOP_WORDS:
                    if matched_term not in primary:
                        primary.append(matched_term)
                    if len(matched_term) >= 3 and matched_term != "敦煌壁":
                        for bg in _cjk_bigrams(matched_term):
                            if bg not in ngrams and bg not in primary:
                                ngrams.append(bg)
                idx += len(matched_term)
            else:
                unmatched_buf.append(run[idx])
                idx += 1

        _flush_unmatched()
        # 若切分后存在长度>=2的实词，则剔除单字残片并将原串二元n-gram纳入备用词元
        if any(len(t) >= 2 for t in primary):
            if any(len(t) == 1 for t in primary):
                for bg in _cjk_bigrams(run):
                    if bg not in ngrams and bg not in primary:
                        ngrams.append(bg)
            primary = [t for t in primary if len(t) >= 2]
        return primary, ngrams

    # 将用户查询字符串拆分为主词元与备用二元n-gram元组
    def _extract_query_plan(self, query: str) -> tuple[list[str], list[str]]:
        lowered = query.strip().casefold()
        if not lowered:
            return [], []
        raw_tokens = TOKEN_RE.findall(lowered)
        if not raw_tokens:
            return [], []

        primary_tokens: list[str] = []
        ngram_tokens: list[str] = []
        for tok in raw_tokens:
            if tok in QUERY_STOP_WORDS:
                continue
            if CJK_ONLY_RE.fullmatch(tok):
                seg_primary, seg_ngrams = self._segment_cjk_run(tok)
                for item in seg_primary:
                    if item not in primary_tokens:
                        primary_tokens.append(item)
                for bg in seg_ngrams:
                    if bg not in ngram_tokens and bg not in primary_tokens:
                        ngram_tokens.append(bg)
            else:
                if tok not in primary_tokens:
                    primary_tokens.append(tok)
        return primary_tokens, ngram_tokens

    # 将用户查询字符串拆分为确定性检索词元列表（剔除停用词并按词表切分中文复合词）
    def _tokenize_query(self, query: str) -> list[str]:
        primary_tokens, _ = self._extract_query_plan(query)
        return primary_tokens

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

    # 计算单个检索词元在记录各加权字段上的得分
    @staticmethod
    def _score_token_fields(token: str, rec: KBRecord, *, weight_scale: int = 2) -> int:
        pts = 0
        if _token_matches(token, rec.search_title):
            pts += 5 * weight_scale
        if _token_matches(token, rec.search_primary):
            pts += 4 * weight_scale
        if _token_matches(token, rec.search_secondary):
            pts += 2 * weight_scale
        if _token_matches(token, rec.search_summary):
            pts += 1 * weight_scale
        return pts

    # 执行确定性字段与关键词加权检索，默认排除带质量或驳回标记的记录
    def search(
        self,
        query: str,
        *,
        library_type: str | None = None,
        role: str | None = None,
        region: str | None = None,
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
        clean_region = (region or "").strip()
        if clean_region and clean_region not in LIBRARY_REGIONS:
            raise ValueError(f"Invalid region: {clean_region}")

        clean_query = str(query or "").strip()
        tokens, ngram_tokens = self._extract_query_plan(clean_query)
        if (
            clean_query
            and not tokens
            and not (clean_type or clean_role or clean_region)
        ):
            return []

        stone_terms = {
            term.casefold()
            for mat in self.materials
            for term in (mat.id, mat.name, *mat.aliases, *mat.search_terms)
            if term
        }
        has_stone_token = any(t in stone_terms for t in tokens)

        full_ranked: list[tuple[int, str, KBRecord]] = []
        partial_ranked: list[tuple[int, int, str, KBRecord]] = []

        for rec in self.records.values():
            if not include_flagged and rec.is_flagged:
                continue
            if clean_type and rec.library_type != clean_type:
                continue
            if clean_role and clean_role not in rec.studio_roles:
                continue
            if clean_region and rec.region != clean_region:
                continue

            if not tokens:
                base_score = 0
                if rec.material_ids:
                    base_score += 20
                if rec.is_catalog_sample:
                    base_score += 5
                elif rec.is_catalog_slab:
                    base_score += 2
                full_ranked.append((base_score, rec.id, rec))
                continue

            matched_tokens = [t for t in tokens if _token_matches(t, rec.search_all)]
            matched_count = len(matched_tokens)

            score = 0
            for t in matched_tokens:
                score += self._score_token_fields(t, rec, weight_scale=2)

            ngram_score = 0
            for bg in ngram_tokens:
                if any(bg in mt for mt in matched_tokens):
                    continue
                if _token_matches(bg, rec.search_all):
                    ngram_score += self._score_token_fields(bg, rec, weight_scale=1)

            if matched_count == 0 and ngram_score == 0:
                continue

            matched_stone = any(t in stone_terms for t in matched_tokens)
            if rec.material_ids and (
                matched_stone or (has_stone_token and matched_count == len(tokens))
            ):
                score += 20
            if rec.is_catalog_sample:
                score += 5
            elif rec.is_catalog_slab:
                score += 2

            if matched_count == len(tokens):
                full_ranked.append((score + ngram_score, rec.id, rec))
            else:
                partial_ranked.append((matched_count, score + ngram_score, rec.id, rec))

        full_ranked.sort(key=lambda item: (-item[0], item[1]))
        partial_ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))

        if full_ranked:
            results = [item[2] for item in full_ranked[:limit]]
            # 当查询包含>=3个词元且完全命中不足limit条时，用高覆盖度的部分命中补足
            if len(tokens) >= 3 and len(results) < limit and partial_ranked:
                seen_ids = {r.id for r in results}
                for matched_cnt, _sc, _rid, rec in partial_ranked:
                    if matched_cnt < 2 and results:
                        break
                    if rec.id not in seen_ids:
                        results.append(rec)
                        seen_ids.add(rec.id)
                        if len(results) >= limit:
                            break
            return results

        return [item[3] for item in partial_ranked[:limit]]

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
        region = str(data.get("region") or "未知").strip() or "未知"
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
        ref_width = int(ref_meta.get("width") or 0)
        ref_height = int(ref_meta.get("height") or 0)

        ref2048_path = root / "assets" / "ref2048" / f"{rec_id}.jpg"
        thumb512_path = root / "assets" / "thumb512" / f"{rec_id}.webp"

        search_title = title_zh.casefold()
        search_primary = " ".join(
            (*raw_materials, *expanded_materials, *tags)
        ).casefold()
        search_secondary = " ".join(
            (
                library_type,
                region,
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
            region=region,
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
        )

    return KnowledgeBaseIndex(root, records, materials)
