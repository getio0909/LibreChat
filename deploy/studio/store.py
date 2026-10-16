import io
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from .config import StudioSettings
from .kb import KB_ID_RE, KBRecord, KnowledgeBaseIndex, load_kb_index

# 用户编号与图像编号的格式校验正则
USER_ID_RE = re.compile(r"^[a-f0-9]{24}$")
IMAGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
LIBRECHAT_FILE_RE = re.compile(
    r"^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(__|-)(.+)$"
)
# LibreChat持久化MCP工具输出图片的文件名格式：<file_id>-<toolName>_img_<nanoid>.<ext>
# 其中MCP工具的toolName为 <upstreamToolName>_mcp_<normalizedServerName>
MCP_DELIMITER = "_mcp_"
STUDIO_MCP_SERVER_NAME = "zyb_studio"
IMAGE_PRODUCING_TOOLS: frozenset[str] = frozenset(
    {
        "generate_image",
        "edit_image",
        "design_concepts",
        "compare_materials",
    }
)
LIBRECHAT_TOOL_OUTPUT_RE = re.compile(r"^([A-Za-z0-9_.-]+?)_img_([A-Za-z0-9_-]+)$")
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")
PLACEHOLDER_CONVERSATION_IDS = {
    "",
    "no_parent",
    "null",
    "undefined",
    "none",
    "{{librechat_body_conversationid}}",
}


# 解析后的用户或知识库图像记录结构
@dataclass(frozen=True)
class ResolvedImage:
    id: str
    kind: str
    path: Path
    created: str
    mtime_ns: int
    width: int
    height: int
    state: str = "saved"
    warning: bool = False
    warning_reasons: tuple[str, ...] = ()
    material_ids: tuple[str, ...] = ()

    # 若知识库资产带有质量或审核驳回标记，生成明确的英文警告说明
    def flag_warning_message(self) -> str | None:
        if self.kind == "library" and self.warning_reasons:
            joined = ", ".join(self.warning_reasons)
            return (
                f"Library asset {self.id} is flagged ({joined}); "
                "verify quality and rights before use."
            )
        return None

    # 转换为对外的无敏感信息字典结构
    def to_public_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "created": self.created,
            "width": self.width,
            "height": self.height,
            "size": f"{self.width}x{self.height}",
        }
        if self.state != "saved" or self.warning:
            payload["state"] = self.state
            payload["warning"] = True
        return payload


# 校验并返回合法的24位十六进制LibreChat用户编号
def validate_user_id(user_id: str | None) -> str:
    if not isinstance(user_id, str) or not USER_ID_RE.fullmatch(user_id):
        raise ValueError("Invalid user ID")
    return user_id


# 规范化可选会话编号，过滤占位符与超长输入
def normalize_conversation_id(conversation_id: str | None) -> str | None:
    if not isinstance(conversation_id, str):
        return None
    cleaned = conversation_id.strip()
    if (
        not cleaned
        or len(cleaned) > 128
        or cleaned.casefold() in PLACEHOLDER_CONVERSATION_IDS
        or "{{" in cleaned
        or "}}" in cleaned
        or "${" in cleaned
        or "\x00" in cleaned
    ):
        return None
    return cleaned


# 校验图像编号，拒绝路径分隔符与目录穿越
def validate_image_id(image_id: str) -> str:
    cleaned = image_id.strip() if isinstance(image_id, str) else ""
    if (
        not cleaned
        or "/" in cleaned
        or "\\" in cleaned
        or "\x00" in cleaned
        or ".." in cleaned
    ):
        raise ValueError("Invalid image ID")
    if cleaned in ("@latest", "@latest_upload"):
        return cleaned
    if not IMAGE_ID_RE.fullmatch(cleaned):
        raise ValueError("Invalid image ID format")
    return cleaned


# 检查路径及其父目录不包含符号链接并位于允许根目录内
def ensure_safe_subpath(path: Path, root: Path) -> Path:
    if root.is_symlink() or path.is_symlink():
        raise ValueError("Symlink paths are not allowed")
    resolved_root = root.resolve(strict=True)
    resolved_path = path.resolve(strict=True)
    if not resolved_path.is_relative_to(resolved_root):
        raise ValueError("Path escapes allowed user directory")
    current = path
    while current != root and current != current.parent:
        if current.is_symlink():
            raise ValueError("Symlink parent directory is not allowed")
        current = current.parent
    return resolved_path


# 使用O_NOFOLLOW安全读取文件字节
def read_bytes_no_follow(
    path: Path, root: Path, max_bytes: int = 64 * 1024 * 1024
) -> bytes:
    ensure_safe_subpath(path, root)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        stat_info = os.fstat(fd)
        if stat_info.st_size > max_bytes:
            raise ValueError("Image file exceeds size limit")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read(max_bytes + 1)
    finally:
        os.close(fd)


# 安全读取经EXIF方向校正后的图片宽高尺寸
def _read_image_dimensions(path: Path, root: Path) -> tuple[int, int]:
    raw = read_bytes_no_follow(path, root)
    with Image.open(io.BytesIO(raw)) as img:
        return ImageOps.exif_transpose(img).size


# 将纳秒时间戳格式化为UTC ISO8601字符串
def _format_mtime_ns(mtime_ns: int) -> str:
    dt = datetime.fromtimestamp(mtime_ns / 1_000_000_000, tz=timezone.utc)
    return dt.isoformat(timespec="seconds")


# 获取用户在LibreChat图片卷中的专属目录
def user_librechat_images_dir(settings: StudioSettings, user_id: str) -> Path:
    uid = validate_user_id(user_id)
    base = Path(settings.librechat_images_dir)
    if base.is_symlink():
        raise ValueError("LibreChat images root must not be a symlink")
    user_dir = base / uid
    if user_dir.is_symlink():
        raise ValueError("User LibreChat images directory must not be a symlink")
    return user_dir


# 获取并初始化用户在工作台存储卷中的专属目录
def user_studio_dir(
    settings: StudioSettings, user_id: str, *, create: bool = False
) -> Path:
    uid = validate_user_id(user_id)
    base = Path(settings.data_dir)
    if base.exists() and base.is_symlink():
        raise ValueError("Studio data root must not be a symlink")
    users_dir = base / "users"
    if users_dir.exists() and users_dir.is_symlink():
        raise ValueError("Studio users directory must not be a symlink")
    user_dir = users_dir / uid
    if user_dir.exists() and user_dir.is_symlink():
        raise ValueError("User studio directory must not be a symlink")
    if create:
        user_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    return user_dir


# 为单次生图分配唯一编号与专属目录
def allocate_studio_job(settings: StudioSettings, user_id: str) -> tuple[str, Path]:
    user_dir = user_studio_dir(settings, user_id, create=True)
    jobs_dir = user_dir / "jobs"
    if jobs_dir.exists() and jobs_dir.is_symlink():
        raise ValueError("User jobs directory must not be a symlink")
    jobs_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    image_id = f"img_{uuid.uuid4().hex}"
    job_dir = jobs_dir / image_id
    job_dir.mkdir(parents=False, mode=0o700, exist_ok=False)
    return image_id, job_dir


# 为单次三维模型生成分配唯一编号与专属目录
def allocate_model_job(settings: StudioSettings, user_id: str) -> tuple[str, Path]:
    user_dir = user_studio_dir(settings, user_id, create=True)
    models_dir = user_dir / "models"
    if models_dir.exists() and models_dir.is_symlink():
        raise ValueError("User models directory must not be a symlink")
    models_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    model_id = f"mdl_{uuid.uuid4().hex}"
    model_dir = models_dir / model_id
    model_dir.mkdir(parents=False, mode=0o700, exist_ok=False)
    return model_id, model_dir


# 解析LibreChat图片文件名中的UUID file_id、分隔符对应的类型与剩余主体
def _parse_librechat_filename_with_rest(filename: str) -> tuple[str, str, str] | None:
    lower = filename.lower()
    suffix = next((ext for ext in IMAGE_SUFFIXES if lower.endswith(ext)), None)
    if suffix is None:
        return None
    stem = filename[: -len(suffix)]
    match = LIBRECHAT_FILE_RE.match(stem)
    if match is None:
        return None
    file_id, sep, rest = match.groups()
    return file_id, ("upload" if sep == "__" else "generated"), rest


# 从LibreChat工具输出文件名主体（<toolName>_img_<nanoid>）中提取本工作台的MCP工具名
def _extract_librechat_tool_name(rest: str) -> str | None:
    match = LIBRECHAT_TOOL_OUTPUT_RE.fullmatch(rest)
    if match is None:
        return None
    raw_tool_name = match.group(1)
    mcp_suffix = f"{MCP_DELIMITER}{STUDIO_MCP_SERVER_NAME}"
    if raw_tool_name.endswith(mcp_suffix):
        tool_name = raw_tool_name[: -len(mcp_suffix)]
    elif MCP_DELIMITER in raw_tool_name:
        return None
    else:
        tool_name = raw_tool_name
    return tool_name or None


# 解析LibreChat图片文件名中的UUID file_id与类型（仅上传文件或四类产图工具输出计入）
def _parse_librechat_filename(filename: str) -> tuple[str, str] | None:
    parsed = _parse_librechat_filename_with_rest(filename)
    if parsed is None:
        return None
    file_id, kind, rest = parsed
    if (
        kind == "generated"
        and _extract_librechat_tool_name(rest) not in IMAGE_PRODUCING_TOOLS
    ):
        return None
    return file_id, kind


# 以0600权限按时间与返回顺序原子记录当前用户search_library输出的kb_<id>列表（绝不使用哈希）
def record_search_previews(
    settings: StudioSettings,
    user_id: str,
    records: list[KBRecord],
    kb_root: Path | None = None,
) -> None:
    del kb_root
    kb_ids = [rec.kb_id for rec in records if KB_ID_RE.fullmatch(rec.kb_id)]
    if not kb_ids:
        return
    studio_dir = user_studio_dir(settings, user_id, create=True)
    previews_file = studio_dir / "search_previews.json"
    existing_batches = _load_search_previews(settings, user_id)
    now_ns = time.time_ns()
    if existing_batches and now_ns <= existing_batches[-1]["recorded_at_ns"]:
        now_ns = existing_batches[-1]["recorded_at_ns"] + 1
    try:
        lc_dir = user_librechat_images_dir(settings, user_id)
        if lc_dir.is_dir() and not lc_dir.is_symlink():
            for cand in lc_dir.iterdir():
                if cand.is_symlink() or not cand.is_file():
                    continue
                parsed = _parse_librechat_filename_with_rest(cand.name)
                if (
                    parsed is not None
                    and parsed[1] == "generated"
                    and _extract_librechat_tool_name(parsed[2]) == "search_library"
                ):
                    st = cand.stat(follow_symlinks=False)
                    now_ns = max(now_ns, st.st_mtime_ns + 1, st.st_ctime_ns + 1)
    except (OSError, ValueError):
        pass
    new_batch: dict[str, Any] = {
        "recorded_at_ns": now_ns,
        "recorded_at": now_ns / 1_000_000_000,
        "kb_ids": kb_ids,
    }
    batches = [*existing_batches, new_batch][-50:]

    temporary = studio_dir / "search_previews.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(batches, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, previews_file)


# 读取并规范化当前用户已记录的search_library结果批次日志（按时间升序排列）
def _load_search_previews(
    settings: StudioSettings, user_id: str
) -> list[dict[str, Any]]:
    try:
        studio_dir = user_studio_dir(settings, user_id, create=False)
        previews_file = studio_dir / "search_previews.json"
        if not previews_file.is_file() or previews_file.is_symlink():
            return []
        raw = read_bytes_no_follow(previews_file, studio_dir)
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, list):
            return []
        batches: list[dict[str, Any]] = []
        legacy_by_ts: dict[int, list[str]] = {}
        for item in parsed:
            if not isinstance(item, dict):
                continue
            raw_kb_ids = item.get("kb_ids")
            raw_ns = item.get("recorded_at_ns")
            if isinstance(raw_ns, int) and not isinstance(raw_ns, bool):
                ts_ns = raw_ns
            else:
                ts_ns = int(float(item.get("recorded_at") or 0) * 1_000_000_000)
            if isinstance(raw_kb_ids, list):
                valid_ids = [
                    str(kid)
                    for kid in raw_kb_ids
                    if isinstance(kid, str) and KB_ID_RE.fullmatch(kid)
                ]
                if valid_ids:
                    batches.append(
                        {
                            "recorded_at_ns": ts_ns,
                            "recorded_at": ts_ns / 1_000_000_000,
                            "kb_ids": valid_ids,
                        }
                    )
            else:
                single_id = str(item.get("kb_id") or "")
                if KB_ID_RE.fullmatch(single_id):
                    legacy_by_ts.setdefault(ts_ns, []).append(single_id)
        for ts_ns, valid_ids in legacy_by_ts.items():
            batches.append(
                {
                    "recorded_at_ns": ts_ns,
                    "recorded_at": ts_ns / 1_000_000_000,
                    "kb_ids": valid_ids,
                }
            )
        batches.sort(key=lambda b: int(b["recorded_at_ns"]))
        return batches
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []


# 将LibreChat持久化的search_library工具输出图片按结果日志的时间与顺序映射回kb_<id>
def _resolve_search_preview_kb_id(
    settings: StudioSettings,
    user_id: str,
    entry: Path,
    lc_dir: Path,
    rest: str,
) -> str | None:
    explicit_kb = re.search(r"kb_([0-9a-f]{16})", rest, re.IGNORECASE)
    if explicit_kb is not None:
        return f"kb_{explicit_kb.group(1).lower()}"

    batches = _load_search_previews(settings, user_id)
    if not batches:
        return None

    try:
        target_resolved = entry.resolve(strict=True)
    except OSError:
        return None

    # 收集用户LibreChat目录中所有search_library工具输出文件并按时间与文件名排序
    search_files: list[tuple[int, int, str, Path]] = []
    for cand in lc_dir.iterdir():
        if cand.is_symlink() or not cand.is_file():
            continue
        parsed = _parse_librechat_filename_with_rest(cand.name)
        if parsed is None:
            continue
        _, cand_kind, cand_rest = parsed
        if (
            cand_kind != "generated"
            or _extract_librechat_tool_name(cand_rest) != "search_library"
        ):
            continue
        try:
            ensure_safe_subpath(cand, lc_dir)
            st = cand.stat(follow_symlinks=False)
            cand_resolved = cand.resolve(strict=True)
        except (OSError, ValueError):
            continue
        search_files.append((st.st_mtime_ns, st.st_ctime_ns, cand.name, cand_resolved))

    if not search_files:
        return None

    search_files.sort(key=lambda item: (item[0], item[1], item[2]))

    # 将每个search_library输出文件按时间戳归入最近一次不晚于它的检索批次
    files_by_batch: dict[int, list[Path]] = {idx: [] for idx in range(len(batches))}
    for mtime_ns, ctime_ns, _, cand_path in search_files:
        effective_ns = max(mtime_ns, ctime_ns)
        chosen_idx = 0
        for idx in range(len(batches) - 1, -1, -1):
            if int(batches[idx]["recorded_at_ns"]) <= effective_ns:
                chosen_idx = idx
                break
        files_by_batch[chosen_idx].append(cand_path)

    for idx, batch_files in files_by_batch.items():
        if target_resolved not in batch_files:
            continue
        pos = batch_files.index(target_resolved)
        batch_kb_ids: list[str] = batches[idx]["kb_ids"]
        if pos < len(batch_kb_ids):
            return batch_kb_ids[pos]
        # 若多个批次的文件因人工修改mtime落入同一批次，按从该批次起的扁平顺序回退映射
        flattened_from_idx = [kid for b in batches[idx:] for kid in b["kb_ids"]]
        if pos < len(flattened_from_idx):
            return flattened_from_idx[pos]
        return None

    return None


# 以0600权限原子写入receipt.json文件
def _write_receipt_atomic(directory: Path, values: dict[str, Any]) -> None:
    target = directory / "receipt.json"
    temporary = directory / "receipt.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(values, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)


# 启动时扫描所有用户的图像与三维模型目录，将残留的未完成凭据标记为uncertain
def reconcile_pending_receipts(settings: StudioSettings) -> int:
    base = Path(settings.data_dir)
    if not base.exists() or base.is_symlink() or not base.is_dir():
        return 0
    users_dir = base / "users"
    if not users_dir.exists() or users_dir.is_symlink() or not users_dir.is_dir():
        return 0
    reconciled_count = 0
    now = time.time()
    for user_dir in users_dir.iterdir():
        if (
            user_dir.is_symlink()
            or not user_dir.is_dir()
            or not USER_ID_RE.fullmatch(user_dir.name)
        ):
            continue
        for subdir_name, prefix, pending_states in (
            ("jobs", "img_", ("pending",)),
            ("models", "mdl_", ("pending", "running")),
        ):
            category_dir = user_dir / subdir_name
            if (
                not category_dir.exists()
                or category_dir.is_symlink()
                or not category_dir.is_dir()
            ):
                continue
            for item_dir in category_dir.iterdir():
                if (
                    item_dir.is_symlink()
                    or not item_dir.is_dir()
                    or not item_dir.name.startswith(prefix)
                ):
                    continue
                receipt_file = item_dir / "receipt.json"
                if receipt_file.is_symlink() or not receipt_file.is_file():
                    continue
                try:
                    ensure_safe_subpath(receipt_file, user_dir)
                    receipt_data = json.loads(
                        read_bytes_no_follow(receipt_file, user_dir).decode("utf-8")
                    )
                    if (
                        not isinstance(receipt_data, dict)
                        or receipt_data.get("state") not in pending_states
                    ):
                        continue
                    receipt_data["state"] = "uncertain"
                    receipt_data["finished"] = now
                    _write_receipt_atomic(item_dir, receipt_data)
                    reconciled_count += 1
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    continue
    return reconciled_count


# 扫描用户目录中的全部可用图像记录（仅计入上传图与四类产图工具输出，可选包含uncertain状态任务）
def _collect_user_images(
    settings: StudioSettings,
    user_id: str,
    *,
    include_uncertain: bool = False,
    kb_index: KnowledgeBaseIndex | None = None,
) -> list[ResolvedImage]:
    del kb_index
    uid = validate_user_id(user_id)
    results: list[ResolvedImage] = []

    # 扫描LibreChat挂载目录中的上传图与四类产图工具输出（排除search_library、3D工具及其他工具输出）
    lc_dir = user_librechat_images_dir(settings, uid)
    if lc_dir.is_dir() and not lc_dir.is_symlink():
        for entry in lc_dir.iterdir():
            if entry.is_symlink() or not entry.is_file():
                continue
            parsed = _parse_librechat_filename_with_rest(entry.name)
            if parsed is None:
                continue
            file_id, kind, rest = parsed
            if (
                kind == "generated"
                and _extract_librechat_tool_name(rest) not in IMAGE_PRODUCING_TOOLS
            ):
                continue
            try:
                stat_info = entry.stat(follow_symlinks=False)
                width, height = _read_image_dimensions(entry, lc_dir)
            except (OSError, ValueError):
                continue
            results.append(
                ResolvedImage(
                    id=file_id,
                    kind=kind,
                    path=entry.resolve(strict=True),
                    created=_format_mtime_ns(stat_info.st_mtime_ns),
                    mtime_ns=stat_info.st_mtime_ns,
                    width=width,
                    height=height,
                )
            )

    # 扫描工作台数据目录中的已保存原图与可选uncertain任务
    studio_dir = user_studio_dir(settings, uid, create=False)
    jobs_dir = studio_dir / "jobs"
    if jobs_dir.is_dir() and not jobs_dir.is_symlink():
        for job_dir in jobs_dir.iterdir():
            if (
                job_dir.is_symlink()
                or not job_dir.is_dir()
                or not job_dir.name.startswith("img_")
            ):
                continue
            receipt_file = job_dir / "receipt.json"
            if receipt_file.is_symlink() or not receipt_file.is_file():
                continue
            try:
                receipt_data = json.loads(
                    read_bytes_no_follow(receipt_file, studio_dir).decode("utf-8")
                )
                if not isinstance(receipt_data, dict):
                    continue
                state = receipt_data.get("state")
                if state == "saved":
                    original_name = receipt_data.get("file")
                    if original_name not in (
                        "original.jpg",
                        "original.png",
                        "original.webp",
                    ):
                        continue
                    original_path = job_dir / original_name
                    if original_path.is_symlink() or not original_path.is_file():
                        continue
                    stat_info = original_path.stat(follow_symlinks=False)
                    width, height = _read_image_dimensions(original_path, studio_dir)
                    results.append(
                        ResolvedImage(
                            id=job_dir.name,
                            kind="studio",
                            path=original_path.resolve(strict=True),
                            created=_format_mtime_ns(stat_info.st_mtime_ns),
                            mtime_ns=stat_info.st_mtime_ns,
                            width=width,
                            height=height,
                        )
                    )
                elif include_uncertain and state == "uncertain":
                    stat_info = receipt_file.stat(follow_symlinks=False)
                    width = int(receipt_data.get("width") or 0)
                    height = int(receipt_data.get("height") or 0)
                    req_size = receipt_data.get("requested_size")
                    if (width <= 0 or height <= 0) and isinstance(req_size, str):
                        parts = req_size.split("x")
                        if len(parts) == 2 and all(p.isdigit() for p in parts):
                            width, height = int(parts[0]), int(parts[1])
                    results.append(
                        ResolvedImage(
                            id=job_dir.name,
                            kind="studio",
                            path=receipt_file.resolve(strict=True),
                            created=_format_mtime_ns(stat_info.st_mtime_ns),
                            mtime_ns=stat_info.st_mtime_ns,
                            width=width,
                            height=height,
                            state="uncertain",
                            warning=True,
                        )
                    )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue

    results.sort(key=lambda item: (item.mtime_ns, item.id), reverse=True)
    return results


# 在当前用户隔离目录或只读知识库目录内解析指定图片编号
def resolve_image(
    settings: StudioSettings,
    user_id: str,
    image_id: str,
    *,
    kb_index: KnowledgeBaseIndex | None = None,
) -> ResolvedImage:
    uid = validate_user_id(user_id)
    cleaned_id = validate_image_id(image_id)

    if cleaned_id == "@latest":
        all_images = _collect_user_images(settings, uid, kb_index=kb_index)
        if not all_images:
            raise ValueError("No images found for current user")
        return all_images[0]

    if cleaned_id == "@latest_upload":
        uploads = [
            img
            for img in _collect_user_images(settings, uid, kb_index=kb_index)
            if img.kind == "upload"
        ]
        if not uploads:
            raise ValueError("No uploaded images found for current user")
        return uploads[0]

    # 匹配只读知识库资产编号 kb_<16位小写十六进制>
    if cleaned_id.startswith("kb_"):
        kb_match = KB_ID_RE.fullmatch(cleaned_id)
        if kb_match is None:
            raise ValueError(f"Invalid library asset ID format: {cleaned_id}")
        asset_id = kb_match.group(1)
        kb_root = Path(settings.kb_root)
        if kb_root.is_symlink() or not kb_root.is_dir():
            raise ValueError(f"Library asset not found: {cleaned_id}")
        active_kb = kb_index if kb_index is not None else load_kb_index(kb_root)
        kb_rec = active_kb.get(asset_id)
        if kb_rec is None:
            raise ValueError(f"Library asset not found: {cleaned_id}")
        ref_path = kb_root / "assets" / "ref2048" / f"{asset_id}.jpg"
        if ref_path.is_symlink() or not ref_path.is_file():
            raise ValueError(f"Library asset file not found: {cleaned_id}")
        ensure_safe_subpath(ref_path, kb_root)
        stat_info = ref_path.stat(follow_symlinks=False)
        width, height = _read_image_dimensions(ref_path, kb_root)
        return ResolvedImage(
            id=cleaned_id,
            kind="library",
            path=ref_path.resolve(strict=True),
            created=_format_mtime_ns(stat_info.st_mtime_ns),
            mtime_ns=stat_info.st_mtime_ns,
            width=width,
            height=height,
            warning=kb_rec.is_flagged,
            warning_reasons=kb_rec.exclusion_flags,
            material_ids=kb_rec.material_ids,
        )

    # 优先匹配工作台原图编号 img_<hex>
    if cleaned_id.startswith("img_"):
        studio_dir = user_studio_dir(settings, uid, create=False)
        job_dir = studio_dir / "jobs" / cleaned_id
        if job_dir.is_dir() and not job_dir.is_symlink():
            receipt_file = job_dir / "receipt.json"
            if receipt_file.is_file() and not receipt_file.is_symlink():
                receipt_data = json.loads(
                    read_bytes_no_follow(receipt_file, studio_dir).decode("utf-8")
                )
                original_name = (
                    receipt_data.get("file") if isinstance(receipt_data, dict) else None
                )
                if (
                    isinstance(receipt_data, dict)
                    and receipt_data.get("state") == "saved"
                    and original_name
                    in ("original.jpg", "original.png", "original.webp")
                ):
                    original_path = job_dir / original_name
                    ensure_safe_subpath(original_path, studio_dir)
                    stat_info = original_path.stat(follow_symlinks=False)
                    width, height = _read_image_dimensions(original_path, studio_dir)
                    return ResolvedImage(
                        id=cleaned_id,
                        kind="studio",
                        path=original_path.resolve(strict=True),
                        created=_format_mtime_ns(stat_info.st_mtime_ns),
                        mtime_ns=stat_info.st_mtime_ns,
                        width=width,
                        height=height,
                    )

    # 在用户LibreChat图片目录内按UUID <file_id>__ 或 <file_id>- 前缀匹配
    if UUID_RE.fullmatch(cleaned_id):
        lc_dir = user_librechat_images_dir(settings, uid)
        if lc_dir.is_dir() and not lc_dir.is_symlink():
            matches: list[ResolvedImage] = []
            preview_kb_ids: list[tuple[int, str]] = []
            saw_unmapped_preview = False
            for entry in lc_dir.iterdir():
                if entry.is_symlink() or not entry.is_file():
                    continue
                parsed = _parse_librechat_filename_with_rest(entry.name)
                if parsed is None:
                    continue
                file_id, kind, rest = parsed
                if file_id != cleaned_id:
                    continue
                ensure_safe_subpath(entry, lc_dir)
                stat_info = entry.stat(follow_symlinks=False)
                if kind == "generated":
                    tool_name = _extract_librechat_tool_name(rest)
                    if tool_name == "search_library" or re.search(
                        r"kb_([0-9a-f]{16})", rest, re.IGNORECASE
                    ):
                        canonical_kb_id = _resolve_search_preview_kb_id(
                            settings, uid, entry, lc_dir, rest
                        )
                        if canonical_kb_id is not None:
                            preview_kb_ids.append(
                                (stat_info.st_mtime_ns, canonical_kb_id)
                            )
                        else:
                            saw_unmapped_preview = True
                        continue
                    if tool_name not in IMAGE_PRODUCING_TOOLS:
                        continue
                width, height = _read_image_dimensions(entry, lc_dir)
                matches.append(
                    ResolvedImage(
                        id=cleaned_id,
                        kind=kind,
                        path=entry.resolve(strict=True),
                        created=_format_mtime_ns(stat_info.st_mtime_ns),
                        mtime_ns=stat_info.st_mtime_ns,
                        width=width,
                        height=height,
                    )
                )
            if preview_kb_ids:
                preview_kb_ids.sort(key=lambda item: item[0], reverse=True)
                return resolve_image(
                    settings, uid, preview_kb_ids[0][1], kb_index=kb_index
                )
            if saw_unmapped_preview and not matches:
                raise ValueError(
                    f"Search preview {cleaned_id} cannot be used as a standalone image; use its kb_<id> from search_library"
                )
            if matches:
                matches.sort(key=lambda item: item.mtime_ns, reverse=True)
                return matches[0]

    raise ValueError(f"Image not found for current user: {cleaned_id}")


# 按时间倒序返回当前用户的最近图片列表（含uncertain状态警告项）
def list_user_images(
    settings: StudioSettings,
    user_id: str,
    limit: int = 20,
    *,
    kb_index: KnowledgeBaseIndex | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
        raise ValueError("limit must be an integer between 1 and 50")
    images = _collect_user_images(
        settings, user_id, include_uncertain=True, kb_index=kb_index
    )
    return [item.to_public_dict() for item in images[:limit]]
