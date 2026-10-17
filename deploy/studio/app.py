import asyncio
import base64
import contextlib
import hashlib
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
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from .config import (
    ConcurrencyLimiter,
    StudioRules,
    StudioSettings,
    load_rules,
    load_settings,
)
from .comparisons import (
    comparison_guard,
    comparison_job_dir,
    comparison_job_id,
    comparison_key,
    saved_comparison_result,
)
from .images import (
    MAX_EDIT_REFERENCES,
    MAX_REFERENCES,
    NATIVE,
    REFERENCE_USES,
    GenerationUncertain,
    build_edit_prompt,
    build_local_edit_prompt,
    comparison_target,
    encode_display_image,
    generate_image_with_provider,
    neutralize_stone_names,
    normalize_reference,
    normalize_reference_with_size,
    prepare_seed3d_image,
)
from .kb import LIBRARY_REGIONS, LIBRARY_TYPES, KnowledgeBaseIndex, load_kb_index
from .models3d import (
    DETAIL_SPECS,
    VISUAL_MODEL_DISCLAIMER,
    ModelJobManager,
    build_model_viewer_html,
    build_signed_file_url,
    format_utc_timestamp,
    load_saved_model_bytes,
    parse_byte_range,
    sign_file_token,
    verify_file_token,
)
from .regions import (
    EditorTokenReplayError,
    OnnxSamSegmenter,
    build_compare_viewer_html,
    build_selection_editor_html,
    finish_region_edit,
    is_editor_nonce_consumed,
    is_editor_token,
    is_image_token,
    load_confirmed_selection,
    local_context,
    object_placement,
    save_confirmed_selection,
    sign_editor_token,
    sign_image_token,
    verify_editor_token,
    verify_image_token,
)
from .segment_worker import SegmentationUnavailable, SupervisedSegmenter
from .store import (
    ResolvedImage,
    allocate_studio_job,
    bind_studio_principal,
    ensure_safe_subpath,
    normalize_conversation_id,
    read_bytes_no_follow,
    reconcile_pending_receipts,
    record_search_previews,
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


# 专精业务MCP工具的扁平JSON Schema定义（不含$ref、oneOf、anyOf、prefixItems）
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
        name="select_region",
        description="打开内联局部选区编辑器，支持框选、多边形套索、画笔涂抹加减选（及离线点选分割就绪时的点选识别），确认选区后生成绑定的选区编号（selection_id）。",
        inputSchema={
            "type": "object",
            "properties": {
                "image_id": {
                    "type": "string",
                    "description": "待框选局部区域的底图编号（file_id、img_<hex>、kb_<id> 或 @latest）",
                },
            },
            "required": ["image_id"],
        },
    ),
    types.Tool(
        name="edit_region",
        description="基于已确认的选区（selection_id）执行局部重绘并严格保证选区外每一像素字节与原图完全一致，返回前后对比预览与结果图；若传入可恢复任务的 job_id 则直接复用已保存的局部裁切图完成回贴而不重复计费。",
        inputSchema={
            "type": "object",
            "properties": {
                "base_id": {
                    "type": "string",
                    "description": "底图编号（file_id、img_<hex>、kb_<id> 或 @latest）",
                },
                "selection_id": {
                    "type": "string",
                    "description": "已确认的选区编号（sel_<hex> 或 @latest）",
                },
                "instruction": {
                    "type": "string",
                    "description": "局部修改说明（如需将参考图物件原样放入选区可注明“原样贴入”）",
                },
                "job_id": {
                    "type": "string",
                    "description": "可选：已保存局部裁切图的可恢复任务编号（img_<hex>），传入后直接完成本地回贴而不再次发起计费调用",
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
                    "description": "局部裁切重绘尺寸档位",
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
                "region": {
                    "type": "string",
                    "enum": list(LIBRARY_REGIONS),
                    "description": "文化地域筛选",
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
                "idempotency_key": {
                    "type": "string",
                    "description": "仅在明确要求对相同底图、部位和石材重新生成一组结果时填写新的请求编号；普通调用和重试请省略",
                },
            },
            "required": ["base_id", "target", "materials"],
        },
    ),
    types.Tool(
        name="create_3d_model",
        description="根据选定的设计图、上传照片或图库资产生成交互式三维视觉预览模型（GLB），仅供视觉预览参考，非工程制造模型。",
        inputSchema={
            "type": "object",
            "properties": {
                "image_id": {
                    "type": "string",
                    "description": "输入图片编号（file_id、img_<hex>、kb_<id> 或 @latest）",
                },
                "detail": {
                    "type": "string",
                    "enum": ["standard", "high"],
                    "default": "standard",
                    "description": "模型精细度档位（standard 约10万面，high 约50万面）",
                },
                "note": {
                    "type": "string",
                    "description": "可选补充说明",
                },
            },
            "required": ["image_id"],
        },
    ),
    types.Tool(
        name="check_3d_model",
        description="查询或恢复后台进行中的三维模型生成任务状态，完成后返回交互式三维视觉预览与下载链接。",
        inputSchema={
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "三维模型任务编号（create_3d_model 返回的 job_id 或 model_id）",
                },
            },
            "required": ["job_id"],
        },
    ),
]


# 默认暂停对外提供的三维模型生成工具名称集合（仅当enable_3d开启时启用）
THREE_D_TOOL_NAMES: frozenset[str] = frozenset({"create_3d_model", "check_3d_model"})

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


# 构建并绑定MCP底层Server与设计工具处理器
def build_mcp_server(
    settings: StudioSettings,
    rules: StudioRules,
    limiter: ConcurrencyLimiter,
    *,
    kb_index: KnowledgeBaseIndex | None = None,
    model_manager: ModelJobManager | None = None,
    segmenter: OnnxSamSegmenter | SupervisedSegmenter | None = None,
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
    active_model_manager = model_manager or ModelJobManager(
        settings,
        limiter,
        ark_transport=ark_transport,
    )
    active_segmenter = segmenter if segmenter is not None else SupervisedSegmenter()

    # 注册tools/list处理器，返回固定扁平Schema的工具列表（默认隐藏3D工具，仅当enable_3d开启时提供）
    @mcp_server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        if settings.enable_3d:
            return STUDIO_TOOLS
        return [tool for tool in STUDIO_TOOLS if tool.name not in THREE_D_TOOL_NAMES]

    # 组装三维模型生成完成或进行中的MCP返回结构（含JSON摘要、Three.js UI资源与源图像）
    def _format_3d_model_tool_result(
        user_id: str,
        outcome: dict[str, Any],
        source_resolved: ResolvedImage | None,
        allowed_roots: tuple[Path, ...],
    ) -> types.CallToolResult:
        warnings = (
            _collect_flag_warnings([source_resolved])
            if source_resolved is not None
            else []
        )
        if outcome.get("state") == "running":
            billing_warning = (
                "3D model generation is still running in the background; the request may have been billed. "
                "Do not resubmit create_3d_model; call check_3d_model with job_id to retrieve the result."
            )
            running_payload: dict[str, Any] = {
                "job_id": outcome["job_id"],
                "model_id": outcome["model_id"],
                "source_image_id": outcome["source_image_id"],
                "detail": outcome["detail"],
                "faces": outcome["faces"],
                "state": "running",
                "message": billing_warning,
                "disclaimer": VISUAL_MODEL_DISCLAIMER,
                "warnings": [*warnings, billing_warning],
            }
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json.dumps(running_payload, ensure_ascii=False),
                    )
                ],
                isError=False,
            )

        model_id = str(outcome["model_id"])
        faces = int(outcome["faces"])
        file_size = int(outcome["bytes"])
        token, exp_ts = sign_file_token(settings, user_id, model_id)
        signed_url = build_signed_file_url(settings, token)
        expires_at = format_utc_timestamp(exp_ts)
        download_markdown = f"[下载 GLB 模型 ({model_id}.glb)]({signed_url})"

        summary_payload: dict[str, Any] = {
            "model_id": model_id,
            "job_id": model_id,
            "state": "saved",
            "source_image_id": outcome["source_image_id"],
            "detail": outcome["detail"],
            "faces": faces,
            "file_size": file_size,
            "bytes": file_size,
            "download_url": signed_url,
            "download_markdown": download_markdown,
            "message": f"3D visual preview model is ready. Include this download link in your chat response: {download_markdown}",
            "expires_at": expires_at,
            "disclaimer": VISUAL_MODEL_DISCLAIMER,
            "warnings": warnings,
        }
        viewer_html = build_model_viewer_html(
            model_id=model_id,
            signed_url=signed_url,
            expires_at=expires_at,
            faces=faces,
            file_size=file_size,
        )
        content_blocks: list[
            types.TextContent | types.ImageContent | types.EmbeddedResource
        ] = [
            types.TextContent(
                type="text",
                text=json.dumps(summary_payload, ensure_ascii=False),
            ),
            types.EmbeddedResource(
                type="resource",
                resource=types.TextResourceContents(
                    uri=f"ui://zyb_studio/model/{model_id}",
                    mimeType="text/html",
                    text=viewer_html,
                ),
            ),
        ]
        if source_resolved is not None:
            source_root = next(
                (
                    root
                    for root in allowed_roots
                    if root.exists()
                    and not root.is_symlink()
                    and source_resolved.path.is_relative_to(root.resolve(strict=True))
                ),
                None,
            )
            if source_root is not None:
                raw_source = read_bytes_no_follow(source_resolved.path, source_root)
                disp_bytes, disp_mime = encode_display_image(raw_source)
                content_blocks.append(
                    types.ImageContent(
                        type="image",
                        data=base64.b64encode(disp_bytes).decode("ascii"),
                        mimeType=disp_mime,
                    )
                )
        return types.CallToolResult(content=content_blocks, isError=False)

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

            if name in THREE_D_TOOL_NAMES and not settings.enable_3d:
                raise ValueError(
                    f"Tool '{name}' is disabled because 3D model generation is on hold"
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
                region = str(args["region"]) if args.get("region") is not None else None
                raw_limit = args.get("limit", 6)
                if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
                    raise ValueError("limit must be an integer between 1 and 12")
                include_flagged = bool(args.get("include_flagged", False))

                matched = active_kb.search(
                    query,
                    library_type=library_type,
                    role=role,
                    region=region,
                    limit=raw_limit,
                    include_flagged=include_flagged,
                )
                result_items: list[dict[str, Any]] = []
                thumb_blocks: list[types.ImageContent] = []
                thumbnail_entries: list[dict[str, Any]] = []
                emitted_records = []
                for rec in matched:
                    item_dict = rec.to_search_dict()
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
                        emitted_records.append(rec)
                        thumb_idx = len(thumb_blocks)
                        thumb_label = f"图{thumb_idx}: {rec.kb_id} ({rec.title_zh})"
                        item_dict["thumbnail_index"] = thumb_idx
                        item_dict["thumbnail_label"] = thumb_label
                        thumbnail_entries.append(
                            {
                                "index": thumb_idx,
                                "id": rec.kb_id,
                                "title": rec.title_zh,
                                "label": thumb_label,
                            }
                        )
                    result_items.append(item_dict)
                record_search_previews(settings, user_id, emitted_records)

                # 在工具返回的JSON文本中显式标注每张缩略图序号对应的kb_<id>
                search_payload = {
                    "results": result_items,
                    "items": result_items,
                    "count": len(result_items),
                    "query": query.strip(),
                    "thumbnails": thumbnail_entries,
                    "thumbnail_labels": [entry["label"] for entry in thumbnail_entries],
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

            if name == "compare_materials":
                base_id = str(args.get("base_id") or "").strip()
                if not base_id:
                    raise ValueError("base_id is required")
                target = comparison_target(args.get("target"), active_rules)
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

                request_key = args.get("idempotency_key", "")
                if not isinstance(request_key, str) or len(request_key) > 128:
                    raise ValueError("idempotency_key must be a string of at most 128 characters")

                # 先解析所有请求的石材名称/别名与无标记样图，拒绝自动选用带标记样图；若某石材无可用样图则报错
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
                        img
                        for sid in mat_obj.sample_asset_ids
                        if not (
                            img := resolve_image(
                                settings, user_id, f"kb_{sid}", kb_index=active_kb
                            )
                        ).warning
                    ]
                    if not sample_images:
                        raise ValueError(
                            f"Material '{mat_obj.name}' has no usable unflagged sample assets"
                        )
                    all_resolved_assets.extend(sample_images)
                    resolved_materials.append(
                        (raw_mat_name.strip(), mat_obj, sample_images)
                    )
                if len({mat_obj.id for _, mat_obj, _ in resolved_materials}) != len(
                    resolved_materials
                ):
                    raise ValueError("materials must refer to distinct catalog materials")

                base_resolved = resolve_image(
                    settings, user_id, base_id, kb_index=active_kb
                )
                all_resolved_assets.insert(0, base_resolved)
                base_uri, base_size = normalize_reference_with_size(
                    base_resolved.path, allowed_roots
                )
                base_sha256 = hashlib.sha256(base_resolved.path.read_bytes()).hexdigest()
                variant_keys = [
                    comparison_key(
                        base_id=base_resolved.id,
                        base_sha256=base_sha256,
                        target=target,
                        material_id=mat_obj.id,
                        sample_fingerprints=[
                            f"{sample.id}:{hashlib.sha256(sample.path.read_bytes()).hexdigest()}"
                            for sample in sample_images
                        ],
                        size=size,
                        request_key=request_key,
                    )
                    for _, mat_obj, sample_images in resolved_materials
                ]

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
                    img_id: str,
                    mat_obj: Any,
                    sample_images: list[ResolvedImage],
                ) -> tuple[dict[str, Any] | None, str | None, bool]:
                    previous = saved_comparison_result(settings, user_id, img_id)
                    if previous is not None:
                        return previous
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
                            job_dir = comparison_job_dir(settings, user_id, img_id)
                            result = await generate_image_with_provider(
                                settings,
                                full_prompt,
                                all_uris,
                                size=size,
                                aspect="source",
                                source_size=base_size,
                                destination=job_dir,
                                ark_transport=ark_transport,
                                gemini_transport=gemini_transport,
                            )
                        return {"id": img_id, **result}, None, False
                    except GenerationUncertain as exc:
                        return None, str(exc), True
                    except (ValueError, TypeError, RuntimeError, OSError) as exc:
                        return None, str(exc), False

                # 交叠批次按相同顺序加锁，重复材质共用一次生成结果。
                async with contextlib.AsyncExitStack() as locks:
                    for variant_key in sorted(set(variant_keys)):
                        await locks.enter_async_context(
                            comparison_guard(settings, user_id, variant_key)
                        )
                    unique_variants = {
                        variant_key: (mat_obj, sample_images)
                        for variant_key, (_, mat_obj, sample_images) in zip(
                            variant_keys, resolved_materials
                        )
                    }
                    async with asyncio.TaskGroup() as tasks:
                        result_tasks = {
                            variant_key: tasks.create_task(
                                _render_material_variant(
                                    comparison_job_id(variant_key), mat_obj, sample_images
                                )
                            )
                            for variant_key, (mat_obj, sample_images) in unique_variants.items()
                        }
                    result_by_key = {
                        variant_key: task.result()
                        for variant_key, task in result_tasks.items()
                    }
                    rendered_variants = [result_by_key[key] for key in variant_keys]

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
                            img_res["raw_bytes"], max_bytes=settings.comparison_preview_max_bytes
                        )
                        image_blocks.append(
                            types.ImageContent(
                                type="image",
                                data=base64.b64encode(disp_bytes).decode("ascii"),
                                mimeType=disp_mime,
                            )
                        )
                        variant_entry["preview_image_index"] = len(image_blocks)
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
                    "idempotency_key": request_key,
                    "image_context": "Preview images follow the successful variants in order. Inspect each image before describing its colors or edited parts; the named material is not visual evidence.",
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

            if name == "select_region":
                image_id = str(args.get("image_id") or "").strip()
                if not image_id:
                    raise ValueError("image_id is required")
                resolved = resolve_image(
                    settings, user_id, image_id, kb_index=active_kb
                )
                base_sha256 = hashlib.sha256(resolved.path.read_bytes()).hexdigest()
                preview_token, preview_exp_ts = sign_image_token(
                    settings, user_id, resolved.id
                )
                preview_url = build_signed_file_url(settings, preview_token)
                preview_expires_at = format_utc_timestamp(preview_exp_ts)
                editor_token, exp_ts = sign_editor_token(
                    settings, user_id, resolved.id, base_sha256
                )
                editor_url = build_signed_file_url(settings, editor_token)
                expires_at = format_utc_timestamp(exp_ts)
                seg_ready = active_segmenter.ready()
                editor_html = build_selection_editor_html(
                    image_id=resolved.id,
                    signed_url=editor_url,
                    expires_at=expires_at,
                    preview_url=preview_url,
                    preview_expires_at=preview_expires_at,
                    width=resolved.width,
                    height=resolved.height,
                    segment_ready=seg_ready,
                )
                summary = {
                    "image_id": resolved.id,
                    "width": resolved.width,
                    "height": resolved.height,
                    "size": f"{resolved.width}x{resolved.height}",
                    "segment_ready": seg_ready,
                    "segmentation_mode": (
                        "sam2_hiera_tiny" if seg_ready else "manual_tools_only"
                    ),
                    "preview_url": preview_url,
                    "preview_expires_at": preview_expires_at,
                    "expires_at": expires_at,
                    "message": (
                        "Inline selection editor is ready. Draw or refine the selection in the UI "
                        "and click '确认选区' to generate a selection_id before calling edit_region."
                    ),
                    "warnings": _collect_flag_warnings([resolved]),
                }
                return types.CallToolResult(
                    content=[
                        types.TextContent(
                            type="text",
                            text=json.dumps(summary, ensure_ascii=False),
                        ),
                        types.EmbeddedResource(
                            type="resource",
                            resource=types.TextResourceContents(
                                uri=f"ui://zyb_studio/selection/{resolved.id}",
                                mimeType="text/html",
                                text=editor_html,
                            ),
                        ),
                    ],
                    isError=False,
                )

            if name in ("edit_region", "finish_region_edit"):
                recovery_job_id = str(args.get("job_id") or "").strip()
                if name == "finish_region_edit" or recovery_job_id:
                    if not recovery_job_id:
                        raise ValueError("job_id is required")
                    rec_result = finish_region_edit(settings, user_id, recovery_job_id)
                    img_id = str(rec_result["id"])
                    rec_base_id = str(rec_result["base_id"])
                    rec_sel_id = str(rec_result["selection_id"])
                    # MCP内联图像块使用受控展示编码以避免大图超出10MiB传输上限；规范无损PNG通过Studio文件目录与认证接口落盘为LibreChat File
                    disp_bytes, disp_mime = encode_display_image(
                        rec_result["raw_bytes"]
                    )
                    b64_display = base64.b64encode(disp_bytes).decode("ascii")
                    before_token, before_exp = sign_image_token(
                        settings, user_id, rec_base_id
                    )
                    after_token, after_exp = sign_image_token(settings, user_id, img_id)
                    before_url = build_signed_file_url(settings, before_token)
                    after_url = build_signed_file_url(settings, after_token)
                    expires_at = format_utc_timestamp(min(before_exp, after_exp))
                    compare_html = build_compare_viewer_html(
                        image_id=img_id,
                        base_id=rec_base_id,
                        selection_id=rec_sel_id,
                        before_url=before_url,
                        after_url=after_url,
                        expires_at=expires_at,
                        width=int(rec_result["width"]),
                        height=int(rec_result["height"]),
                        selected_pixels=int(rec_result["selected_pixels"]),
                    )
                    summary = {
                        "id": img_id,
                        "image_id": img_id,
                        "job_id": img_id,
                        "base_id": rec_base_id,
                        "selection_id": rec_sel_id,
                        "kind": "studio",
                        "state": "saved",
                        "recovered": True,
                        "width": rec_result["width"],
                        "height": rec_result["height"],
                        "size": f"{rec_result['width']}x{rec_result['height']}",
                        "sha256": rec_result["sha256"],
                        "selected_pixels": rec_result["selected_pixels"],
                        "outside_changed_pixels": 0,
                        "before_url": before_url,
                        "after_url": after_url,
                        "expires_at": expires_at,
                        "warnings": [],
                    }
                    return types.CallToolResult(
                        content=[
                            types.TextContent(
                                type="text",
                                text=json.dumps(summary, ensure_ascii=False),
                            ),
                            types.EmbeddedResource(
                                type="resource",
                                resource=types.TextResourceContents(
                                    uri=f"ui://zyb_studio/compare/{img_id}",
                                    mimeType="text/html",
                                    text=compare_html,
                                ),
                            ),
                            types.ImageContent(
                                type="image", data=b64_display, mimeType=disp_mime
                            ),
                        ],
                        isError=False,
                    )

                base_id = str(args.get("base_id") or "").strip()
                selection_id = str(args.get("selection_id") or "").strip()
                instruction = str(args.get("instruction") or "").strip()
                if not base_id or not selection_id or not instruction:
                    raise ValueError(
                        "base_id, selection_id, and instruction are required"
                    )
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
                # 强制核对当前用户的已确认选区及其底图/掩码SHA256凭据
                sel_record = load_confirmed_selection(
                    settings,
                    user_id,
                    selection_id,
                    base_image_id=base_resolved.id,
                    base_path=base_resolved.path,
                )
                confirmed_sel_id = str(sel_record["selection_id"])
                confirmed_selection = sel_record["selection"]
                confirmed_proof = sel_record["proof"]

                resolved_objs = [base_resolved]
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

                if resolved_refs and (
                    "原样贴入" in instruction
                    or any(
                        "原样贴入" in str(r.get("note") or "") for r in resolved_refs
                    )
                ):
                    ctx_info = object_placement(
                        base_resolved.path,
                        confirmed_selection,
                        resolved_refs[0]["path"],
                    )
                    placement_note: str | None = str(ctx_info.get("instruction") or "")
                else:
                    ctx_info = local_context(base_resolved.path, confirmed_selection)
                    placement_note = None

                full_prompt, unique_paths = build_local_edit_prompt(
                    instruction,
                    resolved_refs,
                    active_rules,
                    region_bbox=ctx_info["region"],
                    placement_instruction=placement_note,
                )
                extra_uris = [
                    normalize_reference(p, allowed_roots) for p in unique_paths
                ]
                all_uris = [ctx_info["data_uri"], *extra_uris]
                if len(all_uris) > MAX_REFERENCES:
                    raise ValueError(
                        f"Total images including base crop exceed limit of {MAX_REFERENCES}"
                    )

                async with limiter.acquire(user_id):
                    img_id, job_dir = allocate_studio_job(settings, user_id)
                    gen_result = await generate_image_with_provider(
                        settings,
                        full_prompt,
                        all_uris,
                        size=size,
                        aspect="source",
                        source_size=ctx_info["size"],
                        local_composite={
                            "base_id": base_resolved.id,
                            "selection_id": confirmed_sel_id,
                            "base_path": base_resolved.path,
                            "selection": confirmed_selection,
                            "crop": ctx_info["crop"],
                            "proof": confirmed_proof,
                        },
                        destination=job_dir,
                        ark_transport=ark_transport,
                        gemini_transport=gemini_transport,
                    )

                # MCP内联图像块使用受控展示编码以避免大图超出10MiB传输上限；规范无损PNG通过Studio文件目录与认证接口落盘为LibreChat File
                disp_bytes, disp_mime = encode_display_image(gen_result["raw_bytes"])
                b64_display = base64.b64encode(disp_bytes).decode("ascii")

                before_token, before_exp = sign_image_token(
                    settings, user_id, base_resolved.id
                )
                after_token, after_exp = sign_image_token(settings, user_id, img_id)
                before_url = build_signed_file_url(settings, before_token)
                after_url = build_signed_file_url(settings, after_token)
                expires_at = format_utc_timestamp(min(before_exp, after_exp))

                compare_html = build_compare_viewer_html(
                    image_id=img_id,
                    base_id=base_resolved.id,
                    selection_id=confirmed_sel_id,
                    before_url=before_url,
                    after_url=after_url,
                    expires_at=expires_at,
                    width=gen_result["width"],
                    height=gen_result["height"],
                    selected_pixels=int(gen_result["selected_pixels"]),
                )
                summary = {
                    "id": img_id,
                    "image_id": img_id,
                    "base_id": base_resolved.id,
                    "selection_id": confirmed_sel_id,
                    "kind": "studio",
                    "width": gen_result["width"],
                    "height": gen_result["height"],
                    "size": f"{gen_result['width']}x{gen_result['height']}",
                    "sha256": gen_result["sha256"],
                    "selected_pixels": gen_result["selected_pixels"],
                    "outside_changed_pixels": 0,
                    "before_url": before_url,
                    "after_url": after_url,
                    "expires_at": expires_at,
                    "warnings": _collect_flag_warnings(resolved_objs),
                }
                return types.CallToolResult(
                    content=[
                        types.TextContent(
                            type="text",
                            text=json.dumps(summary, ensure_ascii=False),
                        ),
                        types.EmbeddedResource(
                            type="resource",
                            resource=types.TextResourceContents(
                                uri=f"ui://zyb_studio/compare/{img_id}",
                                mimeType="text/html",
                                text=compare_html,
                            ),
                        ),
                        types.ImageContent(
                            type="image", data=b64_display, mimeType=disp_mime
                        ),
                    ],
                    isError=False,
                )

            if name == "create_3d_model":
                image_id = str(args.get("image_id") or "").strip()
                if not image_id:
                    raise ValueError("image_id is required")
                detail = str(args.get("detail") or "standard").strip()
                if detail not in DETAIL_SPECS:
                    raise ValueError("detail must be 'standard' or 'high'")
                raw_note = args.get("note")
                if raw_note is not None and not isinstance(raw_note, str):
                    raise TypeError("note must be a string when provided")

                source_resolved = resolve_image(
                    settings, user_id, image_id, kb_index=active_kb
                )
                image_data_uri = prepare_seed3d_image(
                    source_resolved.path, allowed_roots
                )
                outcome = await active_model_manager.create_model(
                    user_id=user_id,
                    source_image_id=source_resolved.id,
                    image_data_uri=image_data_uri,
                    detail=detail,
                    note=raw_note,
                )
                return _format_3d_model_tool_result(
                    user_id, outcome, source_resolved, allowed_roots
                )

            if name == "check_3d_model":
                job_id = str(args.get("job_id") or "").strip()
                if not job_id:
                    raise ValueError("job_id is required")
                outcome = await active_model_manager.check_model(
                    user_id=user_id, job_id=job_id
                )
                source_resolved_check: ResolvedImage | None = None
                src_id = str(outcome.get("source_image_id") or "").strip()
                if src_id:
                    with contextlib.suppress(ValueError, OSError):
                        source_resolved_check = resolve_image(
                            settings, user_id, src_id, kb_index=active_kb
                        )
                return _format_3d_model_tool_result(
                    user_id, outcome, source_resolved_check, allowed_roots
                )

            raise ValueError(f"Unknown tool: {name}")
        except GenerationUncertain as exc:
            err_payload: dict[str, Any] = {
                "error": str(exc),
                "state": exc.state,
                "recoverable": exc.state == "recoverable",
                "billing_warning": "The request may have been billed.",
            }
            if exc.job_id:
                err_payload["job_id"] = exc.job_id
                err_payload["model_id"] = exc.job_id
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
            exc_state = getattr(exc, "state", None)
            if isinstance(exc_state, str) and exc_state:
                err_payload["state"] = exc_state
            exc_billed = getattr(exc, "billed", None)
            if isinstance(exc_billed, bool):
                err_payload["billed"] = exc_billed
            exc_job_id = getattr(exc, "job_id", None)
            if isinstance(exc_job_id, str) and exc_job_id:
                err_payload["job_id"] = exc_job_id
                err_payload["model_id"] = exc_job_id
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text", text=json.dumps(err_payload, ensure_ascii=False)
                    )
                ],
                isError=True,
            )

    return mcp_server


# 创建包含/health、签名/files/<token>与受保护无状态/mcp端点的Starlette ASGI应用
def create_app(
    settings: StudioSettings | None = None,
    rules: StudioRules | None = None,
    *,
    segmenter: OnnxSamSegmenter | SupervisedSegmenter | None = None,
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
    model_manager = ModelJobManager(
        active_settings,
        limiter,
        ark_transport=ark_transport,
    )
    active_segmenter = segmenter if segmenter is not None else SupervisedSegmenter()
    mcp_server = build_mcp_server(
        active_settings,
        active_rules,
        limiter,
        kb_index=kb_index,
        model_manager=model_manager,
        segmenter=active_segmenter,
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

    # 管理应用生命周期内的无状态MCP会话管理器与后台3D任务清理
    @contextlib.asynccontextmanager
    async def lifespan(app_instance: Starlette) -> AsyncIterator[None]:
        reconcile_pending_receipts(active_settings)
        manager = _new_session_manager()
        app_instance.state.session_manager = manager
        async with manager.run():
            try:
                if segmenter is None:
                    await asyncio.to_thread(active_segmenter.start)
                yield
            finally:
                await model_manager.shutdown()
                if segmenter is None:
                    await asyncio.to_thread(active_segmenter.close)
                app_instance.state.session_manager = None

    # 健康检查端点处理函数
    async def health_endpoint(request: Request) -> PlainTextResponse:
        return PlainTextResponse("OK", status_code=200)

    # 仅在签名文件路由上附加的跨域与安全响应头
    def _signed_file_cors_headers(
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, HEAD, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Range, If-None-Match, If-Modified-Since",
            "Access-Control-Expose-Headers": "Accept-Ranges, Content-Disposition, Content-Length, Content-Range, Content-Type",
            "X-Content-Type-Options": "nosniff",
        }
        if extra:
            headers.update(extra)
        return headers

    # 拒绝目录列举请求并统一返回404
    async def files_index_not_found(request: Request) -> Response:
        return PlainTextResponse(
            "Not Found", status_code=404, headers=_signed_file_cors_headers()
        )

    # 局部选区POST请求体最大允许字节数（512KiB）与超限异常类型
    max_region_post_body_bytes = 512 * 1024

    class _PayloadTooLargeError(Exception):
        pass

    # 先检查Content-Length再按流分块读取POST请求体，一旦超出512KiB立即中止读取并拒绝
    async def _read_bounded_body(
        request: Request, max_bytes: int = max_region_post_body_bytes
    ) -> bytes:
        raw_cl = request.headers.get("content-length")
        if raw_cl is not None:
            try:
                parsed_cl = int(raw_cl.strip())
            except ValueError as exc:
                raise ValueError("Invalid Content-Length header") from exc
            if parsed_cl < 0:
                raise ValueError("Invalid Content-Length header")
            if parsed_cl > max_bytes:
                raise _PayloadTooLargeError()
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > max_bytes:
                raise _PayloadTooLargeError()
            chunks.append(chunk)
        return b"".join(chunks)

    # 读取经过白名单根目录校验的用户或知识库图片字节与MIME类型
    def _load_safe_image_bytes(
        user_id: str, resolved: ResolvedImage
    ) -> tuple[bytes, str] | None:
        allowed_roots = (
            user_librechat_images_dir(active_settings, user_id),
            user_studio_dir(active_settings, user_id, create=False),
            Path(active_settings.kb_root),
        )
        matched_root: Path | None = None
        for root in allowed_roots:
            if not root.exists() or root.is_symlink():
                continue
            try:
                ensure_safe_subpath(resolved.path, root)
                matched_root = root
                break
            except (OSError, ValueError):
                continue
        if matched_root is None:
            return None
        try:
            img_bytes = read_bytes_no_follow(resolved.path, matched_root)
        except (OSError, ValueError):
            return None
        suffix = resolved.path.suffix.lower()
        media_type = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }.get(suffix, "application/octet-stream")
        return img_bytes, media_type

    # 校验HMAC签名令牌并提供3D模型GLB下载、只读图片预览（img:）及选区编辑器交互（edt:）服务
    async def signed_file_endpoint(request: Request) -> Response:
        token = str(request.path_params.get("token") or "")
        if not token or "/" in token or "\\" in token or ".." in token:
            return PlainTextResponse(
                "Not Found", status_code=404, headers=_signed_file_cors_headers()
            )
        if request.method == "OPTIONS":
            return Response(b"", status_code=204, headers=_signed_file_cors_headers())

        # 允许携带内部X-Studio-Token与X-LibreChat-User-ID的后端服务按用户权限读取kb_或img_原图以桥接LibreChat File记录
        if (
            not is_image_token(token)
            and not is_editor_token(token)
            and token.startswith(("kb_", "img_"))
            and request.method in ("GET", "HEAD")
        ):
            provided_token = request.headers.get("x-studio-token")
            expected_token = active_settings.shared_token
            if isinstance(provided_token, str) and hmac.compare_digest(
                provided_token.encode("utf-8"),
                expected_token.encode("utf-8"),
            ):
                try:
                    auth_user_id = validate_user_id(
                        request.headers.get("x-librechat-user-id")
                    )
                    raw_user_id = validate_user_id(
                        request.headers.get("x-librechat-raw-user-id")
                        or auth_user_id
                    )
                    bind_studio_principal(
                        active_settings, auth_user_id, raw_user_id
                    )
                    resolved_internal = resolve_image(
                        active_settings, auth_user_id, token, kb_index=kb_index
                    )
                    loaded_internal = _load_safe_image_bytes(
                        auth_user_id, resolved_internal
                    )
                except (ValueError, RuntimeError, OSError):
                    loaded_internal = None
                if loaded_internal is not None:
                    img_bytes, media_type = loaded_internal
                    return Response(
                        b"" if request.method == "HEAD" else img_bytes,
                        status_code=200,
                        media_type=media_type,
                        headers={
                            "Cache-Control": "private, no-store",
                            "Content-Length": str(len(img_bytes)),
                            "X-Content-Type-Options": "nosniff",
                        },
                    )
                return PlainTextResponse(
                    "Not Found", status_code=404, headers=_signed_file_cors_headers()
                )

        max_renewal_age = max(
            int(getattr(active_settings, "preview_token_ttl_seconds", 604800)) * 4,
            30 * 86400,
        )

        def _extract_token_from_url_or_raw(raw_val: object) -> str:
            cleaned = str(raw_val or "").strip()
            if not cleaned:
                return ""
            for marker in ("/studio-files/", "/files/"):
                if marker in cleaned:
                    tail = cleaned.rsplit(marker, 1)[-1]
                    return tail.split("?", 1)[0].split("#", 1)[0].strip()
            return cleaned

        def _format_renewed_url(
            new_token: str, reference_url: object = None
        ) -> str:
            ref_str = str(reference_url or "").strip()
            if ref_str.startswith(("http://", "https://")):
                for marker in ("/studio-files/", "/files/"):
                    if marker in ref_str:
                        prefix = ref_str.rsplit(marker, 1)[0]
                        public_base = active_settings.public_base_url.rstrip("/")
                        req_origin = f"{request.url.scheme}://{request.url.netloc}"
                        if prefix in (public_base, req_origin):
                            return f"{prefix}{marker}{new_token}"
            if request.url.hostname in (
                "127.0.0.1",
                "localhost",
                "testserver",
            ) and request.url.path.startswith("/files/"):
                return f"{request.url.scheme}://{request.url.netloc}/files/{new_token}"
            return build_signed_file_url(active_settings, new_token)

        if is_image_token(token):
            if request.method == "POST":
                try:
                    raw_body = await _read_bounded_body(
                        request, max_region_post_body_bytes
                    )
                    payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
                except (_PayloadTooLargeError, ValueError, UnicodeDecodeError):
                    payload = {}
                if isinstance(payload, dict) and payload.get("action") == "renew":
                    try:
                        user_id, image_id, _ = verify_image_token(
                            active_settings,
                            token,
                            allow_expired=True,
                            max_expired_age_seconds=max_renewal_age,
                        )
                        resolved = resolve_image(
                            active_settings, user_id, image_id, kb_index=kb_index
                        )
                    except (ValueError, RuntimeError, OSError):
                        return PlainTextResponse(
                            "Not Found",
                            status_code=404,
                            headers=_signed_file_cors_headers(),
                        )
                    if _load_safe_image_bytes(user_id, resolved) is None:
                        return PlainTextResponse(
                            "Not Found",
                            status_code=404,
                            headers=_signed_file_cors_headers(),
                        )
                    before_ref = payload.get("before_url") or payload.get("beforeUrl")
                    after_ref = payload.get("after_url") or payload.get("afterUrl")
                    if before_ref and after_ref:
                        try:
                            before_tok = _extract_token_from_url_or_raw(before_ref)
                            after_tok = _extract_token_from_url_or_raw(after_ref)
                            b_uid, b_img_id, _ = verify_image_token(
                                active_settings,
                                before_tok,
                                allow_expired=True,
                                max_expired_age_seconds=max_renewal_age,
                            )
                            a_uid, a_img_id, _ = verify_image_token(
                                active_settings,
                                after_tok,
                                allow_expired=True,
                                max_expired_age_seconds=max_renewal_age,
                            )
                            if b_uid != user_id or a_uid != user_id:
                                raise ValueError("Comparison token user mismatch")
                            b_resolved = resolve_image(
                                active_settings, user_id, b_img_id, kb_index=kb_index
                            )
                            a_resolved = resolve_image(
                                active_settings, user_id, a_img_id, kb_index=kb_index
                            )
                            if (
                                _load_safe_image_bytes(user_id, b_resolved) is None
                                or _load_safe_image_bytes(user_id, a_resolved) is None
                            ):
                                raise ValueError("Comparison image missing")
                        except (ValueError, RuntimeError, OSError):
                            return PlainTextResponse(
                                "Not Found",
                                status_code=404,
                                headers=_signed_file_cors_headers(),
                            )
                        new_before_tok, _ = sign_image_token(
                            active_settings, user_id, b_resolved.id
                        )
                        new_after_tok, after_exp = sign_image_token(
                            active_settings, user_id, a_resolved.id
                        )
                        return JSONResponse(
                            {
                                "status": "renewed",
                                "resource_uri": f"ui://zyb_studio/compare/{a_resolved.id}",
                                "image_id": a_resolved.id,
                                "before_url": _format_renewed_url(
                                    new_before_tok, before_ref
                                ),
                                "after_url": _format_renewed_url(
                                    new_after_tok, after_ref
                                ),
                                "expires_at": format_utc_timestamp(after_exp),
                            },
                            status_code=200,
                            headers=_signed_file_cors_headers(),
                        )
                    new_preview_tok, preview_exp = sign_image_token(
                        active_settings, user_id, resolved.id
                    )
                    preview_ref = payload.get("preview_url") or payload.get("previewUrl")
                    return JSONResponse(
                        {
                            "status": "renewed",
                            "image_id": resolved.id,
                            "preview_url": _format_renewed_url(
                                new_preview_tok, preview_ref
                            ),
                            "preview_expires_at": format_utc_timestamp(preview_exp),
                            "expires_at": format_utc_timestamp(preview_exp),
                        },
                        status_code=200,
                        headers=_signed_file_cors_headers(),
                    )
                try:
                    user_id, image_id, _ = verify_image_token(active_settings, token)
                    resolve_image(
                        active_settings, user_id, image_id, kb_index=kb_index
                    )
                except (ValueError, RuntimeError, OSError):
                    return PlainTextResponse(
                        "Not Found",
                        status_code=404,
                        headers=_signed_file_cors_headers(),
                    )
                return JSONResponse(
                    {
                        "error": "Image preview token is read-only and does not permit POST operations"
                    },
                    status_code=403,
                    headers=_signed_file_cors_headers(),
                )

            try:
                user_id, image_id, _ = verify_image_token(active_settings, token)
                resolved = resolve_image(
                    active_settings, user_id, image_id, kb_index=kb_index
                )
            except (ValueError, RuntimeError, OSError):
                return PlainTextResponse(
                    "Not Found", status_code=404, headers=_signed_file_cors_headers()
                )

            if request.method in ("GET", "HEAD"):
                loaded = _load_safe_image_bytes(user_id, resolved)
                if loaded is None:
                    return PlainTextResponse(
                        "Not Found",
                        status_code=404,
                        headers=_signed_file_cors_headers(),
                    )
                img_bytes, media_type = loaded
                img_headers = _signed_file_cors_headers(
                    {
                        "Cache-Control": "private, max-age=3600",
                        "Content-Length": str(len(img_bytes)),
                    }
                )
                return Response(
                    b"" if request.method == "HEAD" else img_bytes,
                    status_code=200,
                    media_type=media_type,
                    headers=img_headers,
                )

            return PlainTextResponse(
                "Method Not Allowed",
                status_code=405,
                headers=_signed_file_cors_headers(),
            )

        if is_editor_token(token):
            post_payload: dict[str, Any] | None = None
            is_renew_post = False
            if request.method == "POST":
                try:
                    raw_body = await _read_bounded_body(
                        request, max_region_post_body_bytes
                    )
                except _PayloadTooLargeError:
                    return JSONResponse(
                        {"error": "Request body exceeds maximum allowed size"},
                        status_code=413,
                        headers=_signed_file_cors_headers(),
                    )
                except ValueError as exc:
                    return JSONResponse(
                        {"error": str(exc)},
                        status_code=400,
                        headers=_signed_file_cors_headers(),
                    )
                try:
                    parsed_body = json.loads(raw_body.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    return JSONResponse(
                        {"error": "Invalid JSON request body"},
                        status_code=400,
                        headers=_signed_file_cors_headers(),
                    )
                if not isinstance(parsed_body, dict):
                    return JSONResponse(
                        {"error": "Request body must be a JSON object"},
                        status_code=400,
                        headers=_signed_file_cors_headers(),
                    )
                post_payload = parsed_body
                is_renew_post = post_payload.get("action") == "renew"

            try:
                claims = verify_editor_token(
                    active_settings,
                    token,
                    allow_expired=is_renew_post,
                    max_expired_age_seconds=max_renewal_age if is_renew_post else None,
                )
                user_id = str(claims["user_id"])
                image_id = str(claims["image_id"])
                expected_sha = str(claims["image_sha256"])
                editor_nonce = str(claims["nonce"])
                resolved = resolve_image(
                    active_settings, user_id, image_id, kb_index=kb_index
                )
            except (ValueError, RuntimeError, OSError):
                return PlainTextResponse(
                    "Not Found", status_code=404, headers=_signed_file_cors_headers()
                )

            loaded = _load_safe_image_bytes(user_id, resolved)
            if loaded is None:
                return PlainTextResponse(
                    "Not Found",
                    status_code=404,
                    headers=_signed_file_cors_headers(),
                )
            img_bytes, media_type = loaded
            if hashlib.sha256(img_bytes).hexdigest() != expected_sha:
                return JSONResponse(
                    {
                        "error": "Base image content changed since selection editor token was issued"
                    },
                    status_code=409,
                    headers=_signed_file_cors_headers(),
                )

            if request.method in ("GET", "HEAD"):
                img_headers = _signed_file_cors_headers(
                    {
                        "Cache-Control": "private, no-store",
                        "Content-Length": str(len(img_bytes)),
                    }
                )
                return Response(
                    b"" if request.method == "HEAD" else img_bytes,
                    status_code=200,
                    media_type=media_type,
                    headers=img_headers,
                )

            if request.method == "POST" and post_payload is not None:
                payload = post_payload
                action = payload.get("action")
                if action == "renew":
                    if is_editor_nonce_consumed(
                        active_settings, user_id, editor_nonce
                    ):
                        return JSONResponse(
                            {
                                "error": "Selection editor token has already been used to confirm a selection"
                            },
                            status_code=409,
                            headers=_signed_file_cors_headers(),
                        )
                    new_preview_tok, preview_exp = sign_image_token(
                        active_settings, user_id, resolved.id
                    )
                    new_editor_tok, editor_exp = sign_editor_token(
                        active_settings,
                        user_id,
                        resolved.id,
                        expected_sha,
                        nonce=editor_nonce,
                    )
                    editor_ref = payload.get("editor_url") or payload.get("editorUrl")
                    preview_ref = (
                        payload.get("preview_url")
                        or payload.get("previewUrl")
                        or editor_ref
                    )
                    fresh_preview_url = _format_renewed_url(
                        new_preview_tok, preview_ref
                    )
                    fresh_editor_url = _format_renewed_url(
                        new_editor_tok, editor_ref
                    )
                    return JSONResponse(
                        {
                            "status": "renewed",
                            "resource_uri": f"ui://zyb_studio/selection/{resolved.id}",
                            "image_id": resolved.id,
                            "preview_url": fresh_preview_url,
                            "preview_expires_at": format_utc_timestamp(preview_exp),
                            "editor_url": fresh_editor_url,
                            "signed_url": fresh_editor_url,
                            "expires_at": format_utc_timestamp(editor_exp),
                        },
                        status_code=200,
                        headers=_signed_file_cors_headers(),
                    )
                if action == "segment":
                    try:
                        seg_out = await asyncio.to_thread(
                            active_segmenter.segment,
                            resolved.path,
                            payload.get("points"),
                            payload.get("existing"),
                            str(payload.get("operation") or "add"),
                        )
                        return JSONResponse(
                            seg_out,
                            status_code=200,
                            headers=_signed_file_cors_headers(),
                        )
                    except SegmentationUnavailable:
                        return JSONResponse(
                            {"error": "Segmentation is unavailable"},
                            status_code=503,
                            headers=_signed_file_cors_headers(),
                        )
                    except (ValueError, RuntimeError, OSError) as exc:
                        return JSONResponse(
                            {"error": str(exc)},
                            status_code=400,
                            headers=_signed_file_cors_headers(),
                        )
                if action == "confirm":
                    try:
                        rec = save_confirmed_selection(
                            active_settings,
                            user_id,
                            resolved.id,
                            resolved.path,
                            payload.get("selection"),
                            expected_image_sha256=expected_sha,
                            editor_nonce=editor_nonce,
                        )
                        return JSONResponse(
                            {
                                "selection_id": rec["selection_id"],
                                "image_id": rec["image_id"],
                                "proof": rec["proof"],
                            },
                            status_code=200,
                            headers=_signed_file_cors_headers(),
                        )
                    except EditorTokenReplayError as exc:
                        return JSONResponse(
                            {"error": str(exc)},
                            status_code=409,
                            headers=_signed_file_cors_headers(),
                        )
                    except (ValueError, RuntimeError, OSError) as exc:
                        return JSONResponse(
                            {"error": str(exc)},
                            status_code=400,
                            headers=_signed_file_cors_headers(),
                        )
                return JSONResponse(
                    {"error": "Unsupported region action"},
                    status_code=400,
                    headers=_signed_file_cors_headers(),
                )

            return PlainTextResponse(
                "Method Not Allowed",
                status_code=405,
                headers=_signed_file_cors_headers(),
            )

        if request.method == "POST":
            try:
                raw_body = await _read_bounded_body(
                    request, max_region_post_body_bytes
                )
                payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except (_PayloadTooLargeError, ValueError, UnicodeDecodeError):
                payload = {}
            if isinstance(payload, dict) and payload.get("action") == "renew":
                try:
                    user_id, model_id, _ = verify_file_token(
                        active_settings,
                        token,
                        allow_expired=True,
                        max_expired_age_seconds=max_renewal_age,
                    )
                    load_saved_model_bytes(active_settings, user_id, model_id)
                except (ValueError, RuntimeError, OSError):
                    return PlainTextResponse(
                        "Not Found",
                        status_code=404,
                        headers=_signed_file_cors_headers(),
                    )
                new_model_tok, model_exp = sign_file_token(
                    active_settings, user_id, model_id
                )
                model_ref = payload.get("model_url") or payload.get("modelUrl")
                fresh_model_url = _format_renewed_url(new_model_tok, model_ref)
                return JSONResponse(
                    {
                        "status": "renewed",
                        "resource_uri": f"ui://zyb_studio/model/{model_id}",
                        "model_id": model_id,
                        "model_url": fresh_model_url,
                        "signed_url": fresh_model_url,
                        "expires_at": format_utc_timestamp(model_exp),
                    },
                    status_code=200,
                    headers=_signed_file_cors_headers(),
                )
            return PlainTextResponse(
                "Method Not Allowed",
                status_code=405,
                headers=_signed_file_cors_headers(),
            )

        if request.method not in ("GET", "HEAD"):
            return PlainTextResponse(
                "Method Not Allowed",
                status_code=405,
                headers=_signed_file_cors_headers(),
            )

        try:
            user_id, model_id, _ = verify_file_token(active_settings, token)
            glb_bytes, _ = load_saved_model_bytes(active_settings, user_id, model_id)
        except (ValueError, RuntimeError, OSError):
            return PlainTextResponse(
                "Not Found", status_code=404, headers=_signed_file_cors_headers()
            )

        total_size = len(glb_bytes)
        base_headers = _signed_file_cors_headers(
            {
                "Accept-Ranges": "bytes",
                "Cache-Control": "private, max-age=3600",
                "Content-Disposition": f'attachment; filename="{model_id}.glb"',
            }
        )
        range_header = request.headers.get("range")
        if range_header:
            try:
                start, end = parse_byte_range(range_header, total_size)
            except ValueError:
                err_headers = _signed_file_cors_headers(
                    {
                        "Accept-Ranges": "bytes",
                        "Content-Range": f"bytes */{total_size}",
                    }
                )
                return PlainTextResponse(
                    "Range Not Satisfiable", status_code=416, headers=err_headers
                )
            chunk = glb_bytes[start : end + 1]
            part_headers = {
                **base_headers,
                "Content-Range": f"bytes {start}-{end}/{total_size}",
                "Content-Length": str(len(chunk)),
            }
            return Response(
                b"" if request.method == "HEAD" else chunk,
                status_code=206,
                media_type="model/gltf-binary",
                headers=part_headers,
            )

        full_headers = {
            **base_headers,
            "Content-Length": str(total_size),
        }
        return Response(
            b"" if request.method == "HEAD" else glb_bytes,
            status_code=200,
            media_type="model/gltf-binary",
            headers=full_headers,
        )

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
                original_user_id = validate_user_id(
                    request.headers.get("x-librechat-raw-user-id") or user_id
                )
                bind_studio_principal(
                    active_settings, user_id, original_user_id
                )
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
                "/files",
                endpoint=files_index_not_found,
                methods=["GET", "HEAD", "OPTIONS"],
            ),
            Route(
                "/files/",
                endpoint=files_index_not_found,
                methods=["GET", "HEAD", "OPTIONS"],
            ),
            Route(
                "/files/{token:path}",
                endpoint=signed_file_endpoint,
                methods=["GET", "HEAD", "POST", "OPTIONS"],
            ),
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
    app.state.model_manager = model_manager
    app.state.segmenter = active_segmenter
    app.state.session_manager = None
    return app
