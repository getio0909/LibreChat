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
from .kb import KB_ID_RE, KnowledgeBaseIndex, load_kb_index

# 用户编号与图像编号的格式校验正则
USER_ID_RE = re.compile(r"^[a-f0-9]{24}$")
IMAGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
LIBRECHAT_FILE_RE = re.compile(
    r"^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(__|-)(.+)$"
)
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


# 解析LibreChat图片文件名中的UUID file_id与类型（仅接受<uuid>__或<uuid>-前缀）
def _parse_librechat_filename(filename: str) -> tuple[str, str] | None:
    lower = filename.lower()
    suffix = next((ext for ext in IMAGE_SUFFIXES if lower.endswith(ext)), None)
    if suffix is None:
        return None
    stem = filename[: -len(suffix)]
    match = LIBRECHAT_FILE_RE.match(stem)
    if match is None:
        return None
    file_id, sep, _ = match.groups()
    return file_id, ("upload" if sep == "__" else "generated")


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


# 扫描用户目录中的全部可用图像记录（可选包含uncertain状态任务）
def _collect_user_images(
    settings: StudioSettings, user_id: str, *, include_uncertain: bool = False
) -> list[ResolvedImage]:
    uid = validate_user_id(user_id)
    results: list[ResolvedImage] = []

    # 扫描LibreChat挂载目录中的上传图与生成图
    lc_dir = user_librechat_images_dir(settings, uid)
    if lc_dir.is_dir() and not lc_dir.is_symlink():
        for entry in lc_dir.iterdir():
            if entry.is_symlink() or not entry.is_file():
                continue
            parsed = _parse_librechat_filename(entry.name)
            if parsed is None:
                continue
            file_id, kind = parsed
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
        all_images = _collect_user_images(settings, uid)
        if not all_images:
            raise ValueError("No images found for current user")
        return all_images[0]

    if cleaned_id == "@latest_upload":
        uploads = [
            img for img in _collect_user_images(settings, uid) if img.kind == "upload"
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
            for entry in lc_dir.iterdir():
                if entry.is_symlink() or not entry.is_file():
                    continue
                parsed = _parse_librechat_filename(entry.name)
                if parsed is None:
                    continue
                file_id, kind = parsed
                if file_id != cleaned_id:
                    continue
                ensure_safe_subpath(entry, lc_dir)
                stat_info = entry.stat(follow_symlinks=False)
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
            if matches:
                matches.sort(key=lambda item: item.mtime_ns, reverse=True)
                return matches[0]

    raise ValueError(f"Image not found for current user: {cleaned_id}")


# 按时间倒序返回当前用户的最近图片列表（含uncertain状态警告项）
def list_user_images(
    settings: StudioSettings, user_id: str, limit: int = 20
) -> list[dict[str, Any]]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
        raise ValueError("limit must be an integer between 1 and 50")
    images = _collect_user_images(settings, user_id, include_uncertain=True)
    return [item.to_public_dict() for item in images[:limit]]
