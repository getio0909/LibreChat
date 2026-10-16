import asyncio
import base64
import contextlib
import hmac
import json
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass
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
    encode_display_image,
    generate_seedream,
    neutralize_stone_names,
    normalize_reference,
)
from .planner import plan_concepts
from .store import (
    allocate_studio_job,
    list_user_images,
    normalize_conversation_id,
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


# 四个固定MCP工具的扁平JSON Schema定义（不含$ref、oneOf、anyOf、prefixItems）
REFERENCE_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "description": "图片编号（file_id、img_<hex> 或 @latest）",
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
                    "description": "参考图片编号列表（最多10张）",
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
                    "description": "底图编号（file_id、img_<hex> 或 @latest）",
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
]


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


# 构建并绑定MCP底层Server与四个设计工具处理器
def build_mcp_server(
    settings: StudioSettings,
    rules: StudioRules,
    limiter: ConcurrencyLimiter,
    *,
    ark_transport: httpx.AsyncBaseTransport | None = None,
    gemini_transport: httpx.AsyncBaseTransport | None = None,
) -> Server:
    mcp_server = Server("zyb_studio")

    # 注册tools/list处理器，返回固定扁平Schema的四个工具
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
            allowed_roots = (
                user_librechat_images_dir(settings, user_id),
                user_studio_dir(settings, user_id, create=True),
            )

            if name == "list_images":
                limit = int(args.get("limit", 20))
                images = list_user_images(settings, user_id, limit=limit)
                payload = {"images": images, "count": len(images), "warnings": []}
                return types.CallToolResult(
                    content=[
                        types.TextContent(
                            type="text", text=json.dumps(payload, ensure_ascii=False)
                        )
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

                resolved_refs: list[dict[str, Any]] = []
                for item in raw_refs:
                    if not isinstance(item, dict):
                        raise TypeError("Each reference must be an object")
                    resolved = resolve_image(
                        settings, user_id, str(item.get("id") or "")
                    )
                    resolved_refs.append(
                        {
                            "id": resolved.id,
                            "path": resolved.path,
                            "use": str(item.get("use") or "参考"),
                            "note": str(item.get("note") or ""),
                        }
                    )

                full_prompt, unique_paths = build_generate_prompt(
                    prompt, resolved_refs, rules
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
                    "warnings": [],
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

                base_resolved = resolve_image(settings, user_id, base_id)
                base_uri = normalize_reference(base_resolved.path, allowed_roots)

                resolved_refs = []
                for item in raw_refs:
                    if not isinstance(item, dict):
                        raise TypeError("Each reference must be an object")
                    resolved = resolve_image(
                        settings, user_id, str(item.get("id") or "")
                    )
                    resolved_refs.append(
                        {
                            "id": resolved.id,
                            "path": resolved.path,
                            "use": str(item.get("use") or "参考"),
                            "note": str(item.get("note") or ""),
                        }
                    )

                full_prompt, unique_paths = build_edit_prompt(
                    instruction, resolved_refs, rules
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
                        source_size=(base_resolved.width, base_resolved.height),
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
                    "warnings": [],
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
                    resolve_image(settings, user_id, str(rid)) for rid in raw_ref_ids
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

                planned = await plan_concepts(
                    settings,
                    rules,
                    brief=brief,
                    count=count,
                    reference_ids=canonical_ref_ids,
                    reference_data_uris=ref_uris,
                    transport=gemini_transport,
                )

                stone_map = {
                    alias: "图1所示石材"
                    for product in rules.materials
                    for alias in [product.name, *product.aliases]
                    if alias
                }

                public_concepts: list[dict[str, Any]] = []
                image_blocks: list[types.ImageContent] = []
                warnings: list[str] = []

                if with_images:
                    # 按每用户并发上限并发生成各概念方案的效果图
                    async def _render_concept(
                        concept_item: dict[str, str],
                    ) -> tuple[dict[str, Any] | None, str | None]:
                        raw_img_prompt = neutralize_stone_names(
                            concept_item["image_prompt"], stone_map
                        )
                        full_prompt = rules.render_prefix + raw_img_prompt
                        if ref_uris:
                            full_prompt = f"{full_prompt}\n{rules.reference_rules}"
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
                is_err = bool(with_images and not image_blocks and warnings)
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
    limiter = ConcurrencyLimiter(
        max_per_user=active_settings.max_parallel_per_user,
        global_max=active_settings.global_max_parallel,
    )
    mcp_server = build_mcp_server(
        active_settings,
        active_rules,
        limiter,
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
    app.state.limiter = limiter
    app.state.session_manager = None
    return app
