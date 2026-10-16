import asyncio
import base64
import contextlib
import hmac
import json
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from .config import (
    ConcurrencyLimiter,
    StudioRules,
    StudioSettings,
    load_rules,
    load_settings,
)
from .images import (
    MAX_EDIT_REFERENCES,
    MAX_REFERENCES,
    NATIVE,
    REFERENCE_USES,
    GenerationUncertain,
    build_edit_prompt,
    build_generate_prompt,
    build_stone_replacements,
    encode_display_image,
    generate_seedream,
    neutralize_stone_names,
    normalize_reference,
    normalize_reference_with_size,
)
from .kb import LIBRARY_TYPES, KnowledgeBaseIndex, load_kb_index
from .planner import plan_concepts
from .store import (
    ResolvedImage,
    allocate_studio_job,
    list_user_images,
    normalize_conversation_id,
    read_bytes_no_follow,
    reconcile_pending_receipts,
    resolve_image,
    user_librechat_images_dir,
    user_studio_dir,
    validate_user_id,
)


# 单次MCP请求的认证上下文
@dataclass(frozen=True)
class RequestContext:
    user_id: str
    conversation_id: str | None = None


# 保存当前异步任务关联的用户身份上下文变量
_request_ctx_var: ContextVar[RequestContext | None] = ContextVar(
    "studio_request_context", default=None
)


# 六个固定MCP工具的扁平JSON Schema定义（不含$ref、oneOf、anyOf、prefixItems）
REFERENCE_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "description": "图片编号（file_id、img_<hex>、kb_<id> 或 @latest）",
        },
        "use": {
            "type": "string",
            "enum": list(REFERENCE_USES),
            "description": "参考用途",
        },
        "note": {
            "type": "string",
            "description": "补充取用说明",
        },
    },
    "required": ["id", "use"],
}

STUDIO_TOOLS: list[types.Tool] = [
    types.Tool(
        name="design_concepts",
        description="根据设计需求规划多个差异化中文概念方案，并可并发生成各方案的概念效果图。",
        inputSchema={
            "type": "object",
            "properties": {
                "brief": {
                    "type": "string",
                    "description": "设计需求与背景描述",
                },
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 3,
                    "default": 3,
                    "description": "概念方案数量（1-3）",
                },
                "with_images": {
                    "type": "boolean",
                    "default": True,
                    "description": "是否同时为每个方案生成概念效果图",
                },
                "aspect": {
                    "type": "string",
                    "enum": ["1:1", "4:3", "3:4", "16:9", "9:16"],
                    "default": "4:3",
                    "description": "画面比例",
                },
                "size": {
                    "type": "string",
                    "enum": ["1K", "2K"],
                    "default": "2K",
                    "description": "输出尺寸档位",
                },
                "reference_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 10,
                    "default": [],
                    "description": "参考图片编号列表（支持 file_id、img_<hex>、kb_<id> 或 @latest，最多10张）",
                },
            },
            "required": ["brief"],
        },
    ),
    types.Tool(
        name="generate_image",
        description="根据设计描述与可选参考图片生成单张概念效果图。",
        inputSchema={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "画面设计描述",
                },
                "aspect": {
                    "type": "string",
                    "enum": ["1:1", "4:3", "3:4", "16:9", "9:16"],
                    "default": "4:3",
                    "description": "画面比例",
                },
                "size": {
                    "type": "string",
                    "enum": ["1K", "2K"],
                    "default": "2K",
                    "description": "输出尺寸档位",
                },
                "references": {
                    "type": "array",
                    "items": REFERENCE_ITEM_SCHEMA,
                    "maxItems": 10,
                    "default": [],
                    "description": "参考图片列表（最多10张）",
                },
            },
            "required": ["prompt"],
        },
    ),
    types.Tool(
        name="edit_image",
        description="以选定图片为底图并保持底图比例进行整图修改，可附带最多9张补充参考图片。",
        inputSchema={
            "type": "object",
            "properties": {
                "base_id": {
                    "type": "string",
                    "description": "底图编号（file_id、img_<hex>、kb_<id> 或 @latest）",
                },
                "instruction": {
                    "type": "string",
                    "description": "修改说明",
                },
                "references": {
                    "type": "array",
                    "items": REFERENCE_ITEM_SCHEMA,
                    "maxItems": 9,
                    "default": [],
                    "description": "补充参考图片列表（最多9张）",
                },
                "size": {
                    "type": "string",
                    "enum": ["1K", "2K"],
                    "default": "2K",
                    "description": "输出尺寸档位",
                },
            },
            "required": ["base_id", "instruction"],
        },
    ),
    types.Tool(
        name="list_images",
        description="按时间倒序列出当前用户最近上传和生成的图片。",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 20,
                    "description": "返回的最大图片数量（1-50）",
                },
            },
            "required": [],
        },
    ),
    types.Tool(
        name="search_library",
        description="检索知识库中的设计参考、石材大板、造像法器、家具与空间案例，返回条目摘要与缩略图。",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索关键词（标题、石材名及别名、品类、风格、颜色或标签）",
                },
                "library_type": {
                    "type": "string",
                    "enum": list(LIBRARY_TYPES),
                    "description": "图库分类筛选",
                },
                "role": {
                    "type": "string",
                    "enum": list(REFERENCE_USES),
                    "description": "参考用途角色筛选",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 12,
                    "default": 6,
                    "description": "返回的最大条目数量（1-12）",
                },
                "include_flagged": {
                    "type": "boolean",
                    "default": False,
                    "description": "是否包含低分辨率、AI生成、第三方水印或已驳回标记的记录",
                },
            },
            "required": ["query"],
        },
    ),
    types.Tool(
        name="compare_materials",
        description="基于同一张底图并发生成多款石材（1-3款）替换指定部位的对比效果图，各款独立基于原底图生成且互不串联。",
        inputSchema={
            "type": "object",
            "properties": {
                "base_id": {
                    "type": "string",
                    "description": "底图编号（file_id、img_<hex>、kb_<id> 或 @latest）",
                },
                "target": {
                    "type": "string",
                    "description": "需要替换石材的具体部位（如：佛像背后的墙板、供桌台面）",
                },
                "materials": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 3,
                    "description": "要对比的石材名称或别名列表（1-3款）",
                },
            },
            "required": ["base_id", "target", "materials"],
        },
    ),
]


# 汇总显式传入的带标记知识库资产警告信息（按编号去重）
def _collect_flag_warnings(items: list[ResolvedImage]) -> list[str]:
    warnings: list[str] = []
    seen: set[str] = set()
    for item in items:
        msg = item.flag_warning_message()
        if msg and item.id not in seen:
            seen.add(item.id)
            warnings.append(msg)
    return warnings


# 获取当前工具调用关联的已认证用户上下文
def _get_active_context(mcp_server: Server) -> RequestContext:
    try:
        req_ctx = mcp_server.request_context
        req = getattr(req_ctx, "request", None)
        if req is not None and isinstance(getattr(req, "scope", None), dict):
            scoped = req.scope.get("studio_context")
            if isinstance(scoped, RequestContext):
                return scoped
    except LookupError:
        pass
    ctx = _request_ctx_var.get()
    if ctx is None:
        raise RuntimeError("Missing authenticated user context")
    return ctx


# 构建并绑定MCP底层Server与六个设计工具处理器
def build_mcp_server(
    settings: StudioSettings,
    rules: StudioRules,
    limiter: ConcurrencyLimiter,
    *,
    kb_index: KnowledgeBaseIndex | None = None,
    ark_transport: httpx.AsyncBaseTransport | None = None,
    gemini_transport: httpx.AsyncBaseTransport | None = None,
) -> Server:
    mcp_server = Server("zyb_studio")
    active_kb = (
        kb_index
        if kb_index is not None
        else load_kb_index(settings.kb_root, fallback_materials=rules.materials)
    )
    active_rules = (
        rules.model_copy(update={"materials": active_kb.materials})
        if active_kb.materials
        else rules
    )

    # 注册tools/list处理器，返回固定扁平Schema的六个工具
    @mcp_server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        return STUDIO_TOOLS

    # 注册tools/call处理器，按用户隔离执行设计工具
    @mcp_server.call_tool()
    async def handle_call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> types.CallToolResult:
        args = arguments or {}
        try:
            ctx = _get_active_context(mcp_server)
            user_id = ctx.user_id
            kb_root = Path(settings.kb_root)
            allowed_roots = (
                user_librechat_images_dir(settings, user_id),
                user_studio_dir(settings, user_id, create=True),
                kb_root,
            )

            if name == "list_images":
                limit = int(args.get("limit", 20))
                images = list_user_images(settings, user_id, limit=limit)
                warnings = [
                    f"Image job {item['id']} is in uncertain state (the request may have been billed)."
                    for item in images
                    if item.get("warning")
                ]
                payload = {
                    "images": images,
                    "count": len(images),
                    "warnings": warnings,
                }
                return types.CallToolResult(
                    content=[
                        types.TextContent(
                            type="text", text=json.dumps(payload, ensure_ascii=False)
                        )
                    ],
                    isError=False,
                )

            if name == "search_library":
                if "query" not in args or not isinstance(args.get("query"), str):
                    raise ValueError("query is required and must be a string")
                query = str(args.get("query") or "")
                library_type = (
                    str(args["library_type"])
                    if args.get("library_type") is not None
                    else None
                )
                role = str(args["role"]) if args.get("role") is not None else None
                raw_limit = args.get("limit", 6)
                if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
                    raise ValueError("limit must be an integer between 1 and 12")
                include_flagged = bool(args.get("include_flagged", False))

                matched = active_kb.search(
                    query,
                    library_type=library_type,
                    role=role,
                    limit=raw_limit,
                    include_flagged=include_flagged,
                )
                result_items: list[dict[str, Any]] = []
                thumb_blocks: list[types.ImageContent] = []
                for rec in matched:
                    result_items.append(rec.to_search_dict())
                    if (
                        rec.thumb512_path.is_file()
                        and not rec.thumb512_path.is_symlink()
                    ):
                        thumb_bytes = read_bytes_no_follow(rec.thumb512_path, kb_root)
                        thumb_blocks.append(
                            types.ImageContent(
                                type="image",
                                data=base64.b64encode(thumb_bytes).decode("ascii"),
                                mimeType="image/webp",
                            )
                        )

                search_payload = {
                    "results": result_items,
                    "items": result_items,
                    "count": len(result_items),
                    "query": query.strip(),
                }
                return types.CallToolResult(
                    content=[
                        types.TextContent(
                            type="text",
                            text=json.dumps(search_payload, ensure_ascii=False),
                        ),
                        *thumb_blocks,
                    ],
                    isError=False,
                )

            if name == "generate_image":
                prompt = str(args.get("prompt") or "").strip()
                if not prompt:
                    raise ValueError("prompt must be a non-empty string")
                aspect = str(args.get("aspect", "4:3"))
                size = str(args.get("size", "2K"))
                if size not in NATIVE or aspect not in NATIVE[size]:
                    raise ValueError("Invalid size or aspect parameter")
                raw_refs = args.get("references") or []
                if not isinstance(raw_refs, list) or len(raw_refs) > MAX_REFERENCES:
                    raise ValueError(f"At most {MAX_REFERENCES} references are allowed")

                resolved_objs: list[ResolvedImage] = []
                resolved_refs: list[dict[str, Any]] = []
                for item in raw_refs:
                    if not isinstance(item, dict):
                        raise TypeError("Each reference must be an object")
                    resolved = resolve_image(
                        settings,
                        user_id,
                        str(item.get("id") or ""),
                        kb_index=active_kb,
                    )
                    resolved_objs.append(resolved)
                    resolved_refs.append(
                        {
                            "id": resolved.id,
                            "path": resolved.path,
                            "use": str(item.get("use") or "参考"),
                            "note": str(item.get("note") or ""),
                            "material_ids": resolved.material_ids,
                        }
                    )

                full_prompt, unique_paths = build_generate_prompt(
                    prompt, resolved_refs, active_rules
                )
                ref_uris = [normalize_reference(p, allowed_roots) for p in unique_paths]

                async with limiter.acquire(user_id):
                    img_id, job_dir = allocate_studio_job(settings, user_id)
                    gen_result = await generate_seedream(
                        settings,
                        full_prompt,
                        ref_uris,
                        size=size,
                        aspect=aspect,
                        destination=job_dir,
                        transport=ark_transport,
                    )

                display_bytes, display_mime = encode_display_image(
                    gen_result["raw_bytes"]
                )
                b64_display = base64.b64encode(display_bytes).decode("ascii")
                summary = {
                    "id": img_id,
                    "kind": "studio",
                    "width": gen_result["width"],
                    "height": gen_result["height"],
                    "size": f"{gen_result['width']}x{gen_result['height']}",
                    "warnings": _collect_flag_warnings(resolved_objs),
                }
                return types.CallToolResult(
                    content=[
                        types.TextContent(
                            type="text", text=json.dumps(summary, ensure_ascii=False)
                        ),
                        types.ImageContent(
                            type="image", data=b64_display, mimeType=display_mime
                        ),
                    ],
                    isError=False,
                )

            if name == "edit_image":
                base_id = str(args.get("base_id") or "").strip()
                instruction = str(args.get("instruction") or "").strip()
                if not base_id or not instruction:
                    raise ValueError("base_id and instruction are required")
                size = str(args.get("size", "2K"))
                if size not in NATIVE:
                    raise ValueError("Invalid size parameter")
                raw_refs = args.get("references") or []
                if (
                    not isinstance(raw_refs, list)
                    or len(raw_refs) > MAX_EDIT_REFERENCES
                ):
                    raise ValueError(
                        f"At most {MAX_EDIT_REFERENCES} additional references are allowed"
                    )

                base_resolved = resolve_image(
                    settings, user_id, base_id, kb_index=active_kb
                )
                base_uri, base_size = normalize_reference_with_size(
                    base_resolved.path, allowed_roots
                )

                resolved_objs = [base_resolved]
                resolved_refs = []
                for item in raw_refs:
                    if not isinstance(item, dict):
                        raise TypeError("Each reference must be an object")
                    resolved = resolve_image(
                        settings,
                        user_id,
                        str(item.get("id") or ""),
                        kb_index=active_kb,
                    )
                    resolved_objs.append(resolved)
                    resolved_refs.append(
                        {
                            "id": resolved.id,
                            "path": resolved.path,
                            "use": str(item.get("use") or "参考"),
                            "note": str(item.get("note") or ""),
                            "material_ids": resolved.material_ids,
                        }
                    )

                full_prompt, unique_paths = build_edit_prompt(
                    instruction, resolved_refs, active_rules
                )
                extra_uris = [
                    normalize_reference(p, allowed_roots) for p in unique_paths
                ]
                all_uris = [base_uri, *extra_uris]
                if len(all_uris) > MAX_REFERENCES:
                    raise ValueError(
                        f"Total images including base exceed limit of {MAX_REFERENCES}"
                    )

                async with limiter.acquire(user_id):
                    img_id, job_dir = allocate_studio_job(settings, user_id)
                    gen_result = await generate_seedream(
                        settings,
                        full_prompt,
                        all_uris,
                        size=size,
                        aspect="source",
                        source_size=base_size,
                        destination=job_dir,
                        transport=ark_transport,
                    )

                display_bytes, display_mime = encode_display_image(
                    gen_result["raw_bytes"]
                )
                b64_display = base64.b64encode(display_bytes).decode("ascii")
                summary = {
                    "id": img_id,
                    "base_id": base_resolved.id,
                    "kind": "studio",
                    "width": gen_result["width"],
                    "height": gen_result["height"],
                    "size": f"{gen_result['width']}x{gen_result['height']}",
                    "warnings": _collect_flag_warnings(resolved_objs),
                }
                return types.CallToolResult(
                    content=[
                        types.TextContent(
                            type="text", text=json.dumps(summary, ensure_ascii=False)
                        ),
                        types.ImageContent(
                            type="image", data=b64_display, mimeType=display_mime
                        ),
                    ],
                    isError=False,
                )

            if name == "compare_materials":
                base_id = str(args.get("base_id") or "").strip()
                target = str(args.get("target") or "").strip()
                if not base_id or not target or "\x00" in target or len(target) > 120:
                    raise ValueError("base_id and target are required")
                raw_materials = args.get("materials")
                if (
                    not isinstance(raw_materials, list)
                    or not 1 <= len(raw_materials) <= 3
                ):
                    raise ValueError(
                        "materials must be a list of 1 to 3 material names"
                    )
                size = str(args.get("size", "2K"))
                if size not in NATIVE:
                    raise ValueError("Invalid size parameter")

                # 先解析所有请求的石材名称/别名与样图，未知名称直接抛出含已知石材列表的明确错误
                resolved_materials = []
                all_resolved_assets: list[ResolvedImage] = []
                for raw_mat_name in raw_materials:
                    if not isinstance(raw_mat_name, str) or not raw_mat_name.strip():
                        raise ValueError(
                            f"Material name must be a non-empty string. Known materials: {active_kb.format_known_materials()}"
                        )
                    mat_obj = active_kb.resolve_material(raw_mat_name)
                    if not mat_obj.sample_asset_ids:
                        raise ValueError(
                            f"Material '{mat_obj.name}' has no sample assets configured"
                        )
                    sample_images = [
                        resolve_image(
                            settings, user_id, f"kb_{sid}", kb_index=active_kb
                        )
                        for sid in mat_obj.sample_asset_ids
                    ]
                    all_resolved_assets.extend(sample_images)
                    resolved_materials.append(
                        (raw_mat_name.strip(), mat_obj, sample_images)
                    )

                base_resolved = resolve_image(
                    settings, user_id, base_id, kb_index=active_kb
                )
                all_resolved_assets.insert(0, base_resolved)
                base_uri, base_size = normalize_reference_with_size(
                    base_resolved.path, allowed_roots
                )

                # 对target中可能出现的任何石材商业名称做中性化处理，再生成Mac标准替换说明
                generic_stone_map = {
                    alias: "本次材质样图所示石材"
                    for prod in active_rules.materials
                    for alias in (prod.name, *prod.aliases)
                    if alias
                }
                clean_target = neutralize_stone_names(target, generic_stone_map)
                instruction = (
                    f"将图1的{clean_target}替换为本次材质样图所示石材，其他部分不变。"
                )

                async def _render_material_variant(
                    req_name: str,
                    mat_obj: Any,
                    sample_images: list[ResolvedImage],
                ) -> tuple[dict[str, Any] | None, str | None, bool]:
                    variant_refs = [
                        {
                            "id": sample_img.id,
                            "path": sample_img.path,
                            "use": "材质",
                            "note": mat_obj.name,
                            "material_id": mat_obj.id,
                            "material_ids": sample_img.material_ids,
                        }
                        for sample_img in sample_images
                    ]
                    full_prompt, unique_paths = build_edit_prompt(
                        instruction, variant_refs, active_rules
                    )
                    sample_uris = [
                        normalize_reference(p, allowed_roots) for p in unique_paths
                    ]
                    all_uris = [base_uri, *sample_uris]
                    try:
                        async with limiter.acquire(user_id):
                            img_id, job_dir = allocate_studio_job(settings, user_id)
                            result = await generate_seedream(
                                settings,
                                full_prompt,
                                all_uris,
                                size=size,
                                aspect="source",
                                source_size=base_size,
                                destination=job_dir,
                                transport=ark_transport,
                            )
                        return {"id": img_id, **result}, None, False
                    except GenerationUncertain as exc:
                        return None, str(exc), True
                    except (ValueError, TypeError, RuntimeError, OSError) as exc:
                        return None, str(exc), False

                rendered_variants = await asyncio.gather(
                    *(
                        _render_material_variant(req_name, mat_obj, sample_images)
                        for req_name, mat_obj, sample_images in resolved_materials
                    )
                )

                variants_out: list[dict[str, Any]] = []
                image_blocks: list[types.ImageContent] = []
                warnings: list[str] = _collect_flag_warnings(all_resolved_assets)
                any_uncertain = False

                for (req_name, mat_obj, sample_images), (
                    img_res,
                    err_text,
                    is_uncertain,
                ) in zip(resolved_materials, rendered_variants):
                    sample_kb_ids = [s.id for s in sample_images]
                    variant_entry: dict[str, Any] = {
                        "material": mat_obj.name,
                        "material_id": mat_obj.id,
                        "requested_material": req_name,
                        "reference_ids": sample_kb_ids,
                    }
                    if img_res is not None:
                        variant_entry["id"] = img_res["id"]
                        variant_entry["image_id"] = img_res["id"]
                        variant_entry["width"] = img_res["width"]
                        variant_entry["height"] = img_res["height"]
                        variant_entry["size"] = (
                            f"{img_res['width']}x{img_res['height']}"
                        )
                        disp_bytes, disp_mime = encode_display_image(
                            img_res["raw_bytes"]
                        )
                        image_blocks.append(
                            types.ImageContent(
                                type="image",
                                data=base64.b64encode(disp_bytes).decode("ascii"),
                                mimeType=disp_mime,
                            )
                        )
                    elif err_text:
                        variant_entry["error"] = err_text
                        if is_uncertain:
                            variant_entry["state"] = "uncertain"
                            any_uncertain = True
                        warnings.append(f"{mat_obj.name}: {err_text}")
                    variants_out.append(variant_entry)

                compare_payload: dict[str, Any] = {
                    "base_id": base_resolved.id,
                    "target": target,
                    "variants": variants_out,
                    "comparisons": variants_out,
                    "count": len(image_blocks),
                    "warnings": warnings,
                }
                if any_uncertain:
                    compare_payload["state"] = "uncertain"
                is_err = len(image_blocks) == 0
                return types.CallToolResult(
                    content=[
                        types.TextContent(
                            type="text",
                            text=json.dumps(compare_payload, ensure_ascii=False),
                        ),
                        *image_blocks,
                    ],
                    isError=is_err,
                )

            if name == "design_concepts":
                brief = str(args.get("brief") or "").strip()
                if not brief:
                    raise ValueError("brief must be a non-empty string")
                count = int(args.get("count", 3))
                with_images = bool(args.get("with_images", True))
                aspect = str(args.get("aspect", "4:3"))
                size = str(args.get("size", "2K"))
                if size not in NATIVE or aspect not in NATIVE[size]:
                    raise ValueError("Invalid size or aspect parameter")
                raw_ref_ids = args.get("reference_ids") or []
                if (
                    not isinstance(raw_ref_ids, list)
                    or len(raw_ref_ids) > MAX_REFERENCES
                ):
                    raise ValueError(
                        f"At most {MAX_REFERENCES} reference_ids are allowed"
                    )

                resolved_ref_items = [
                    resolve_image(settings, user_id, str(rid), kb_index=active_kb)
                    for rid in raw_ref_ids
                ]
                unique_ref_paths: list[Any] = []
                canonical_ref_ids: list[str] = []
                for item in resolved_ref_items:
                    canonical_ref_ids.append(item.id)
                    if item.path not in unique_ref_paths:
                        unique_ref_paths.append(item.path)
                ref_uris = [
                    normalize_reference(p, allowed_roots) for p in unique_ref_paths
                ]

                async with limiter.acquire(user_id):
                    planned = await plan_concepts(
                        settings,
                        active_rules,
                        brief=brief,
                        count=count,
                        reference_ids=canonical_ref_ids,
                        reference_data_uris=ref_uris,
                        transport=gemini_transport,
                    )

                concept_refs = [
                    {
                        "id": item.id,
                        "path": item.path,
                        "use": "参考",
                        "note": item.path.stem,
                        "material_ids": item.material_ids,
                    }
                    for item in resolved_ref_items
                ]
                stone_map = build_stone_replacements(
                    active_rules, unique_ref_paths, concept_refs, has_base=False
                )

                public_concepts: list[dict[str, Any]] = []
                image_blocks = []
                warnings = _collect_flag_warnings(resolved_ref_items)
                render_errors: list[str] = []

                if with_images:
                    # 按每用户并发上限并发生成各概念方案的效果图，并附带原始brief作为硬性design_constraints
                    clean_brief = neutralize_stone_names(brief, stone_map)
                    constraints_line = json.dumps(
                        {"design_constraints": clean_brief}, ensure_ascii=False
                    )

                    async def _render_concept(
                        concept_item: dict[str, str],
                    ) -> tuple[dict[str, Any] | None, str | None]:
                        raw_img_prompt = neutralize_stone_names(
                            concept_item["image_prompt"], stone_map
                        )
                        full_prompt = active_rules.render_prefix + raw_img_prompt
                        if ref_uris:
                            full_prompt = (
                                f"{full_prompt}\n{active_rules.reference_rules}"
                            )
                        full_prompt = f"{full_prompt}\n{constraints_line}"
                        try:
                            async with limiter.acquire(user_id):
                                img_id, job_dir = allocate_studio_job(settings, user_id)
                                result = await generate_seedream(
                                    settings,
                                    full_prompt,
                                    ref_uris,
                                    size=size,
                                    aspect=aspect,
                                    destination=job_dir,
                                    transport=ark_transport,
                                )
                            return {"id": img_id, **result}, None
                        except (ValueError, TypeError, RuntimeError, OSError) as exc:
                            return None, str(exc)

                    rendered = await asyncio.gather(
                        *(_render_concept(c) for c in planned)
                    )
                    for concept_item, (img_res, err_text) in zip(planned, rendered):
                        entry: dict[str, Any] = {
                            "title": concept_item["title"],
                            "positioning": concept_item["positioning"],
                            "description": concept_item["description"],
                            "materials": concept_item["materials"],
                            "key_details": concept_item["key_details"],
                        }
                        if img_res is not None:
                            entry["image_id"] = img_res["id"]
                            entry["width"] = img_res["width"]
                            entry["height"] = img_res["height"]
                            entry["size"] = f"{img_res['width']}x{img_res['height']}"
                            disp_bytes, disp_mime = encode_display_image(
                                img_res["raw_bytes"]
                            )
                            image_blocks.append(
                                types.ImageContent(
                                    type="image",
                                    data=base64.b64encode(disp_bytes).decode("ascii"),
                                    mimeType=disp_mime,
                                )
                            )
                        elif err_text:
                            render_errors.append(err_text)
                            warnings.append(err_text)
                        public_concepts.append(entry)
                else:
                    for concept_item in planned:
                        public_concepts.append(
                            {
                                "title": concept_item["title"],
                                "positioning": concept_item["positioning"],
                                "description": concept_item["description"],
                                "materials": concept_item["materials"],
                                "key_details": concept_item["key_details"],
                            }
                        )

                summary_payload = {
                    "concepts": public_concepts,
                    "count": len(public_concepts),
                    "warnings": warnings,
                }
                is_err = bool(with_images and not image_blocks and render_errors)
                return types.CallToolResult(
                    content=[
                        types.TextContent(
                            type="text",
                            text=json.dumps(summary_payload, ensure_ascii=False),
                        ),
                        *image_blocks,
                    ],
                    isError=is_err,
                )

            raise ValueError(f"Unknown tool: {name}")
        except GenerationUncertain as exc:
            err_payload = {"error": str(exc), "state": "uncertain"}
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text", text=json.dumps(err_payload, ensure_ascii=False)
                    )
                ],
                isError=True,
            )
        except (ValueError, TypeError, RuntimeError, OSError) as exc:
            err_payload = {"error": str(exc)}
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text", text=json.dumps(err_payload, ensure_ascii=False)
                    )
                ],
                isError=True,
            )

    return mcp_server


# 创建包含/health与受保护无状态/mcp端点的Starlette ASGI应用
def create_app(
    settings: StudioSettings | None = None,
    rules: StudioRules | None = None,
    *,
    ark_transport: httpx.AsyncBaseTransport | None = None,
    gemini_transport: httpx.AsyncBaseTransport | None = None,
) -> Starlette:
    active_settings = settings or load_settings()
    active_rules = rules or load_rules()
    kb_index = load_kb_index(
        active_settings.kb_root, fallback_materials=active_rules.materials
    )
    reconcile_pending_receipts(active_settings)
    limiter = ConcurrencyLimiter(
        max_per_user=active_settings.max_parallel_per_user,
        global_max=active_settings.global_max_parallel,
    )
    mcp_server = build_mcp_server(
        active_settings,
        active_rules,
        limiter,
        kb_index=kb_index,
        ark_transport=ark_transport,
        gemini_transport=gemini_transport,
    )
    security_settings = TransportSecuritySettings(enable_dns_rebinding_protection=False)

    # 创建无状态Streamable HTTP会话管理器实例
    def _new_session_manager() -> StreamableHTTPSessionManager:
        return StreamableHTTPSessionManager(
            app=mcp_server,
            event_store=None,
            json_response=True,
            stateless=True,
            security_settings=security_settings,
        )

    # 管理应用生命周期内的无状态MCP会话管理器
    @contextlib.asynccontextmanager
    async def lifespan(app_instance: Starlette) -> AsyncIterator[None]:
        reconcile_pending_receipts(active_settings)
        manager = _new_session_manager()
        app_instance.state.session_manager = manager
        async with manager.run():
            try:
                yield
            finally:
                app_instance.state.session_manager = None

    # 健康检查端点处理函数
    async def health_endpoint(request: Request) -> PlainTextResponse:
        return PlainTextResponse("OK", status_code=200)

    # 校验X-Studio-Token与X-LibreChat-User-ID后转发至无状态Streamable HTTP处理器
    class _MCPASGIHandler:
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            request = Request(scope, receive=receive)
            provided_token = request.headers.get("x-studio-token")
            expected_token = active_settings.shared_token
            if not isinstance(provided_token, str) or not hmac.compare_digest(
                provided_token.encode("utf-8"),
                expected_token.encode("utf-8"),
            ):
                response = JSONResponse({"error": "Forbidden"}, status_code=403)
                await response(scope, receive, send)
                return

            raw_user_id = request.headers.get("x-librechat-user-id")
            try:
                user_id = validate_user_id(raw_user_id)
            except ValueError:
                response = JSONResponse({"error": "Forbidden"}, status_code=403)
                await response(scope, receive, send)
                return

            conversation_id = normalize_conversation_id(
                request.headers.get("x-librechat-conversation-id")
            )
            req_context = RequestContext(
                user_id=user_id, conversation_id=conversation_id
            )

            # 若客户端未显式携带application/json Accept头则自动补齐以兼容测试客户端
            raw_headers = list(scope.get("headers") or [])
            accept_val = request.headers.get("accept", "")
            if "application/json" not in accept_val.lower():
                raw_headers = [(k, v) for k, v in raw_headers if k.lower() != b"accept"]
                raw_headers.append((b"accept", b"application/json, text/event-stream"))
                scope = {**scope, "headers": raw_headers}

            scope["studio_context"] = req_context
            token = _request_ctx_var.set(req_context)
            try:
                manager: StreamableHTTPSessionManager | None = getattr(
                    app.state, "session_manager", None
                )
                if (
                    manager is not None
                    and getattr(manager, "_task_group", None) is not None
                ):
                    await manager.handle_request(scope, receive, send)
                else:
                    temp_manager = _new_session_manager()
                    async with temp_manager.run():
                        await temp_manager.handle_request(scope, receive, send)
            finally:
                _request_ctx_var.reset(token)

    app = Starlette(
        debug=False,
        routes=[
            Route("/health", endpoint=health_endpoint, methods=["GET"]),
            Route(
                "/mcp", endpoint=_MCPASGIHandler(), methods=["POST", "GET", "DELETE"]
            ),
        ],
        lifespan=lifespan,
    )
    app.state.settings = active_settings
    app.state.rules = active_rules
    app.state.kb_index = kb_index
    app.state.limiter = limiter
    app.state.session_manager = None
    return app
