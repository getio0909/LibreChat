import asyncio
import base64
import contextlib
import hashlib
import hmac
import html
import io
import json
import os
import re
import struct
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .config import ConcurrencyLease, ConcurrencyLimiter, StudioSettings
from .images import GenerationUncertain
from .store import (
    allocate_model_job,
    ensure_safe_subpath,
    read_bytes_no_follow,
    user_studio_dir,
    validate_user_id,
)

# 三维模型编号、签名令牌与控制参数常量定义
MODEL_ID_RE = re.compile(r"^mdl_[0-9a-f]{32}$")
FILE_TOKEN_RE = re.compile(r"^([A-Za-z0-9_-]{16,256})\.([0-9a-f]{64})$")
FLAG_INJECTION_RE = re.compile(r"--[A-Za-z0-9_-]+")
DETAIL_SPECS: dict[str, tuple[str, int]] = {
    "standard": ("low", 100_000),
    "high": ("medium", 500_000),
}
MAX_GLB_BYTES = 200 * 1024 * 1024
GLB_MAGIC = b"glTF"
ZIP_MAGIC = b"PK\x03\x04"
VISUAL_MODEL_DISCLAIMER = "仅供视觉预览参考（visual model only），非工程制造或施工下料模型（not a manufacturing model）。"

# 三维模型任务状态机合法状态与允许的状态迁移表（saved为不可逆终态）
MODEL_JOB_STATES = frozenset({"pending", "running", "uncertain", "failed", "saved"})
ALLOWED_MODEL_STATE_TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({"pending", "running", "uncertain", "failed", "saved"}),
    "pending": frozenset({"pending", "running", "uncertain", "failed", "saved"}),
    "running": frozenset({"running", "uncertain", "failed", "saved"}),
    "uncertain": frozenset({"uncertain", "running", "failed", "saved"}),
    "failed": frozenset({"failed"}),
    "saved": frozenset({"saved"}),
}


# 校验三维模型任务编号格式（mdl_<32位小写十六进制>）
def validate_model_id(model_id: str | None) -> str:
    cleaned = model_id.strip() if isinstance(model_id, str) else ""
    if not MODEL_ID_RE.fullmatch(cleaned):
        raise ValueError("Invalid 3D model ID")
    return cleaned


# 清理可选补充说明中的控制字符与潜在命令行标志参数
def sanitize_model_note(note: str | None) -> str:
    if not isinstance(note, str):
        return ""
    cleaned = note.replace("\x00", " ").replace("\r", " ").replace("\n", " ")
    cleaned = FLAG_INJECTION_RE.sub(" ", cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned[:240]


# 构造豆包Seed3D异步创建任务请求体（隔离协议字段适配器）
def build_seed3d_create_payload(
    model: str,
    image_data_uri: str,
    detail: str = "standard",
    note: str | None = None,
) -> tuple[dict[str, Any], str, int]:
    if detail not in DETAIL_SPECS:
        raise ValueError("detail must be 'standard' or 'high'")
    if not isinstance(image_data_uri, str) or not image_data_uri.startswith(
        "data:image/"
    ):
        raise ValueError("image_data_uri must be a normalized data:image/* URI")
    subdivision_level, faces = DETAIL_SPECS[detail]
    clean_note = sanitize_model_note(note)
    flags_text = f"--subdivisionlevel {subdivision_level} --fileformat glb"
    text_content = f"{clean_note} {flags_text}".strip() if clean_note else flags_text
    payload: dict[str, Any] = {
        "model": model,
        "content": [
            {
                "type": "text",
                "text": text_content,
            },
            {
                "type": "image_url",
                "image_url": {
                    "url": image_data_uri,
                },
            },
        ],
    }
    return payload, subdivision_level, faces


# 解析豆包Seed3D异步创建任务响应中的任务编号（隔离响应字段适配器）
def parse_seed3d_create_response(data: Any) -> str:
    payload = data if isinstance(data, dict) else {}
    task_id = payload.get("id")
    if not isinstance(task_id, str) or not task_id.strip():
        nested = payload.get("data")
        if isinstance(nested, dict):
            task_id = nested.get("id") or nested.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise RuntimeError(
            "Upstream 3D task creation response is missing required task id field"
        )
    cleaned = task_id.strip()
    if "/" in cleaned or "\\" in cleaned or ".." in cleaned or len(cleaned) > 128:
        raise RuntimeError(
            "Upstream 3D task creation response returned invalid task id"
        )
    return cleaned


# 解析豆包Seed3D查询任务状态响应结构（隔离响应字段适配器）
def parse_seed3d_query_response(data: Any) -> dict[str, Any]:
    payload = data if isinstance(data, dict) else {}
    raw_status = payload.get("status")
    if not isinstance(raw_status, str) or not raw_status.strip():
        nested = payload.get("data")
        if isinstance(nested, dict):
            raw_status = nested.get("status")
            payload = nested
    if not isinstance(raw_status, str) or not raw_status.strip():
        raise RuntimeError(
            "Upstream 3D task query response is missing required status field"
        )
    data = payload

    normalized_status = raw_status.strip().lower()
    status_aliases = {
        "success": "succeeded",
        "completed": "succeeded",
        "canceled": "cancelled",
        "pending": "queued",
        "processing": "running",
        "in_progress": "running",
    }
    status = status_aliases.get(normalized_status, normalized_status)
    if status not in (
        "queued",
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "expired",
    ):
        raise RuntimeError(f"Unsupported upstream 3D task status: {status!r}")

    result: dict[str, Any] = {"status": status}
    if status == "succeeded":
        content_obj = data.get("content")
        file_url = (
            content_obj.get("file_url") or content_obj.get("url")
            if isinstance(content_obj, dict)
            else None
        )
        if not isinstance(file_url, str) or not file_url.strip():
            file_url = data.get("file_url")
        if not isinstance(file_url, str) or not file_url.strip().startswith(
            ("https://", "http://")
        ):
            raise RuntimeError(
                "Upstream 3D task succeeded response is missing valid content.file_url"
            )
        result["file_url"] = file_url.strip()
    elif status in ("failed", "cancelled", "expired"):
        err_obj = data.get("error")
        err_code = (
            str(err_obj.get("code") or status) if isinstance(err_obj, dict) else status
        )
        result["error_code"] = err_code[:64]
    return result


# 校验GLB二进制头部魔数、版本号与字节长度
def validate_glb_bytes(glb_bytes: bytes) -> bytes:
    if len(glb_bytes) < 20:
        raise RuntimeError("Invalid GLB binary: file is shorter than 20-byte header")
    if len(glb_bytes) > MAX_GLB_BYTES:
        raise RuntimeError("GLB binary exceeds maximum allowed size")
    magic, version, declared_length = struct.unpack("<4sII", glb_bytes[:12])
    if magic != GLB_MAGIC or version != 2:
        raise RuntimeError("Invalid GLB binary: expected glTF 2.0 header")
    if declared_length > len(glb_bytes) or declared_length < 20:
        raise RuntimeError("Invalid GLB binary: header length mismatch")
    return glb_bytes


# 从上游返回的直接GLB字节或ZIP归档包中提取原始GLB二进制内容
def extract_glb_bytes(raw_bytes: bytes) -> bytes:
    if not raw_bytes:
        raise RuntimeError("Upstream 3D artifact payload is empty")
    if len(raw_bytes) > MAX_GLB_BYTES:
        raise RuntimeError("Upstream 3D artifact payload exceeds size limit")
    if raw_bytes.startswith(GLB_MAGIC):
        return validate_glb_bytes(raw_bytes)
    if raw_bytes.startswith(ZIP_MAGIC):
        try:
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as archive:
                glb_infos = [
                    info
                    for info in archive.infolist()
                    if not info.is_dir()
                    and info.filename.lower().endswith(".glb")
                    and ".." not in info.filename
                    and not info.filename.startswith(("/", "\\"))
                ]
                if not glb_infos:
                    raise RuntimeError(
                        "Upstream 3D ZIP archive does not contain a .glb file"
                    )
                target_info = glb_infos[0]
                if target_info.file_size <= 0 or target_info.file_size > MAX_GLB_BYTES:
                    raise RuntimeError(
                        "GLB entry in upstream ZIP archive has invalid size"
                    )
                extracted = archive.read(target_info)
        except zipfile.BadZipFile as exc:
            raise RuntimeError(
                "Upstream 3D artifact is a corrupted ZIP archive"
            ) from exc
        return validate_glb_bytes(extracted)
    raise RuntimeError(
        "Upstream 3D artifact is neither a valid GLB binary nor a ZIP archive containing a GLB file"
    )


# 读取目录中已存在的receipt.json字典（若不存在或不可读则返回None）
def _read_existing_receipt_dict(model_dir: Path) -> dict[str, Any] | None:
    target = model_dir / "receipt.json"
    try:
        if not target.exists() or target.is_symlink() or not target.is_file():
            return None
        parsed = json.loads(target.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else None
    except (OSError, ValueError, TypeError):
        return None


# 以0600权限原子写入三维模型凭据receipt.json（保护saved终态不可被覆盖且保留provider_task_id）
def write_model_receipt(model_dir: Path, values: dict[str, Any]) -> None:
    next_state = values.get("state")
    if next_state not in MODEL_JOB_STATES:
        raise ValueError(f"Invalid 3D model receipt state: {next_state!r}")

    existing = _read_existing_receipt_dict(model_dir)
    if existing is not None:
        existing_state = existing.get("state")
        if existing_state == "saved" and next_state != "saved":
            values.clear()
            values.update(existing)
            return
        allowed = ALLOWED_MODEL_STATE_TRANSITIONS.get(
            existing_state if isinstance(existing_state, str) else None,
            MODEL_JOB_STATES,
        )
        if next_state not in allowed:
            if existing_state == "saved":
                values.clear()
                values.update(existing)
                return
            raise ValueError(
                f"Disallowed 3D model state transition: {existing_state!r} -> {next_state!r}"
            )
        existing_task_id = existing.get("provider_task_id") or existing.get("task_id")
        if (
            isinstance(existing_task_id, str)
            and existing_task_id.strip()
            and not (
                isinstance(values.get("provider_task_id"), str)
                and str(values.get("provider_task_id")).strip()
            )
        ):
            values["provider_task_id"] = existing_task_id.strip()

    target = model_dir / "receipt.json"
    temporary = model_dir / "receipt.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(values, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)


# 按单任务状态机规则更新三维模型receipt（若已处于saved终态则拒绝覆盖并返回False）
def _transition_model_receipt(
    model_dir: Path,
    receipt: dict[str, Any],
    next_state: str,
    *,
    best_effort: bool = False,
    **updates: Any,
) -> bool:
    existing = _read_existing_receipt_dict(model_dir)
    if (
        existing is not None
        and existing.get("state") == "saved"
        and next_state != "saved"
    ):
        receipt.clear()
        receipt.update(existing)
        return False
    if receipt.get("state") == "saved" and next_state != "saved":
        return False

    preserved_task_id = (
        updates.get("provider_task_id")
        or receipt.get("provider_task_id")
        or receipt.get("task_id")
        or (
            (existing.get("provider_task_id") or existing.get("task_id"))
            if existing is not None
            else None
        )
    )
    receipt.update(updates)
    receipt["state"] = next_state
    if isinstance(preserved_task_id, str) and preserved_task_id.strip():
        receipt["provider_task_id"] = preserved_task_id.strip()

    if best_effort:
        with contextlib.suppress(OSError, ValueError):
            write_model_receipt(model_dir, receipt)
    else:
        write_model_receipt(model_dir, receipt)
    return receipt.get("state") == next_state


# 以0600权限原子写入原始model.glb文件
def write_glb_atomic(model_dir: Path, glb_bytes: bytes) -> Path:
    validate_glb_bytes(glb_bytes)
    target = model_dir / "model.glb"
    temporary = model_dir / "model.glb.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(glb_bytes)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    return target


# 安全读取指定用户的三维模型任务凭据与目录路径
def read_model_receipt(
    settings: StudioSettings, user_id: str, model_id: str
) -> tuple[Path, dict[str, Any]]:
    uid = validate_user_id(user_id)
    mid = validate_model_id(model_id)
    studio_dir = user_studio_dir(settings, uid, create=False)
    if not studio_dir.exists() or studio_dir.is_symlink() or not studio_dir.is_dir():
        raise ValueError(f"3D model job not found for current user: {mid}")
    model_dir = studio_dir / "models" / mid
    if not model_dir.exists() or model_dir.is_symlink() or not model_dir.is_dir():
        raise ValueError(f"3D model job not found for current user: {mid}")
    receipt_path = model_dir / "receipt.json"
    if (
        not receipt_path.exists()
        or receipt_path.is_symlink()
        or not receipt_path.is_file()
    ):
        raise ValueError(f"3D model job not found for current user: {mid}")
    ensure_safe_subpath(receipt_path, studio_dir)
    raw = read_bytes_no_follow(receipt_path, studio_dir, max_bytes=1024 * 1024)
    receipt = json.loads(raw.decode("utf-8"))
    if type(receipt) is not dict:
        raise ValueError(f"Invalid 3D model receipt for: {mid}")
    if receipt.get("user_id") and receipt.get("user_id") != uid:
        raise ValueError(f"3D model job not found for current user: {mid}")
    return model_dir, receipt


# 安全读取已完成的三维模型GLB字节与凭据信息
def load_saved_model_bytes(
    settings: StudioSettings, user_id: str, model_id: str
) -> tuple[bytes, dict[str, Any]]:
    uid = validate_user_id(user_id)
    mid = validate_model_id(model_id)
    studio_dir = user_studio_dir(settings, uid, create=False)
    model_dir, receipt = read_model_receipt(settings, uid, mid)
    if receipt.get("state") != "saved" or receipt.get("file") != "model.glb":
        raise ValueError(f"3D model file is not available: {mid}")
    glb_path = model_dir / "model.glb"
    if not glb_path.exists() or glb_path.is_symlink() or not glb_path.is_file():
        raise ValueError(f"3D model file not found: {mid}")
    ensure_safe_subpath(glb_path, studio_dir)
    glb_bytes = read_bytes_no_follow(glb_path, studio_dir, max_bytes=MAX_GLB_BYTES)
    validate_glb_bytes(glb_bytes)
    return glb_bytes, receipt


# 使用HMAC-SHA256对用户编号、模型编号与过期时间生成签名访问令牌
def sign_file_token(
    settings: StudioSettings,
    user_id: str,
    model_id: str,
    *,
    expires_at: int | None = None,
    ttl_seconds: int | None = None,
) -> tuple[str, int]:
    uid = validate_user_id(user_id)
    mid = validate_model_id(model_id)
    if expires_at is not None:
        exp = int(expires_at)
    else:
        fields_set = getattr(settings, "model_fields_set", set())
        if ttl_seconds is not None:
            raw_ttl = int(ttl_seconds)
        elif "preview_token_ttl_seconds" in fields_set:
            raw_ttl = int(settings.preview_token_ttl_seconds)
        elif "file_token_ttl_seconds" in fields_set:
            raw_ttl = int(settings.file_token_ttl_seconds)
        else:
            raw_ttl = max(
                int(settings.file_token_ttl_seconds),
                int(
                    getattr(
                        settings,
                        "preview_token_ttl_seconds",
                        settings.file_token_ttl_seconds,
                    )
                ),
            )
        exp = int(time.time()) + max(60, raw_ttl)
    payload_raw = f"{uid}.{mid}.{exp}".encode("ascii")
    payload_b64 = base64.urlsafe_b64encode(payload_raw).decode("ascii").rstrip("=")
    key_bytes = bytes.fromhex(settings.file_signing_key)
    signature = hmac.new(
        key_bytes, payload_b64.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"{payload_b64}.{signature}", exp


# 校验HMAC-SHA256签名访问令牌并返回（user_id, model_id, expires_at）
def verify_file_token(
    settings: StudioSettings,
    token: str,
    *,
    now: float | None = None,
    allow_expired: bool = False,
    max_expired_age_seconds: int | None = None,
) -> tuple[str, str, int]:
    cleaned_token = token if isinstance(token, str) else ""
    match = FILE_TOKEN_RE.fullmatch(cleaned_token)
    if match is None:
        raise ValueError("Malformed token")
    payload_b64, provided_sig = match.groups()
    key_bytes = bytes.fromhex(settings.file_signing_key)
    expected_sig = hmac.new(
        key_bytes, payload_b64.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(
        provided_sig.encode("ascii"), expected_sig.encode("ascii")
    ):
        raise ValueError("Token signature mismatch")
    padding = "=" * (-len(payload_b64) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload_b64 + padding).decode("ascii")
    except Exception as exc:
        raise ValueError("Invalid token payload encoding") from exc
    parts = decoded.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid token payload structure")
    raw_uid, raw_mid, raw_exp = parts
    uid = validate_user_id(raw_uid)
    mid = validate_model_id(raw_mid)
    if not raw_exp.isdigit() or len(raw_exp) > 12:
        raise ValueError("Invalid token expiration")
    exp = int(raw_exp)
    current_ts = time.time() if now is None else float(now)
    if current_ts >= exp:
        if not allow_expired:
            raise ValueError("Token has expired")
        if (
            max_expired_age_seconds is not None
            and (current_ts - exp) > max_expired_age_seconds
        ):
            raise ValueError("Token has expired beyond renewal window")
    return uid, mid, exp


# 拼接对外公开的签名模型文件下载与预览URL
def build_signed_file_url(settings: StudioSettings, token: str) -> str:
    base = settings.public_base_url.rstrip("/")
    return f"{base}/studio-files/{token}"


# 生成面向设计师的简短中文三维模型下载文件名（对齐设计图-<8位十六进制>命名规范，不暴露内部mdl_前缀与32位完整哈希）
def format_model_download_filename(model_id: str) -> str:
    mid = validate_model_id(model_id)
    short_hex = mid.removeprefix("mdl_")[:8]
    return f"三维模型-{short_hex}.glb"


# 生成面向设计师的Markdown三维模型下载链接文本
def format_model_download_markdown(model_id: str, signed_url: str) -> str:
    display_filename = format_model_download_filename(model_id)
    return f"[下载 GLB 模型 ({display_filename})]({signed_url})"


# 生成遵循RFC 5987 / RFC 6266的三维模型下载Content-Disposition响应头（复用LibreChat getContentDisposition编码策略）
def format_model_content_disposition(model_id: str) -> str:
    mid = validate_model_id(model_id)
    short_hex = mid.removeprefix("mdl_")[:8]
    display_filename = f"三维模型-{short_hex}.glb"
    encoded_filename = quote(display_filename, safe="-_.")
    return f"attachment; filename=\"model-{short_hex}.glb\"; filename*=UTF-8''{encoded_filename}"


# 将Unix秒级时间戳格式化为ISO8601 UTC字符串
def format_utc_timestamp(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# 将ISO8601或UTC时间字符串转换为面向设计师的本地可读时间（不含原始T/Z后缀）
def format_display_timestamp(expires_at: str) -> str:
    cleaned = (expires_at or "").strip()
    if not cleaned:
        return ""
    try:
        normalized = cleaned[:-1] + "+00:00" if cleaned.endswith("Z") else cleaned
        dt = datetime.fromisoformat(normalized)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return cleaned.replace("T", " ").rstrip("Z")


# 解析HTTP Range请求头（支持bytes=start-end、bytes=start-、bytes=-suffix）
def parse_byte_range(range_header: str, total_size: int) -> tuple[int, int]:
    if total_size <= 0:
        raise ValueError("Empty file does not support byte ranges")
    cleaned = range_header.strip()
    if not cleaned.startswith("bytes="):
        raise ValueError("Unsupported range unit")
    spec = cleaned[len("bytes=") :].strip()
    if "," in spec or "-" not in spec:
        raise ValueError("Multiple or malformed byte ranges are not supported")
    start_str, end_str = spec.split("-", 1)
    start_str = start_str.strip()
    end_str = end_str.strip()
    if start_str == "":
        if not end_str.isdigit():
            raise ValueError("Invalid suffix byte range")
        suffix_len = int(end_str)
        if suffix_len <= 0:
            raise ValueError("Zero suffix byte range is not satisfiable")
        start = max(0, total_size - suffix_len)
        end = total_size - 1
        return start, end
    if not start_str.isdigit():
        raise ValueError("Invalid range start offset")
    start = int(start_str)
    if start >= total_size:
        raise ValueError("Range start offset exceeds file size")
    if end_str == "":
        return start, total_size - 1
    if not end_str.isdigit():
        raise ValueError("Invalid range end offset")
    end = int(end_str)
    if end < start:
        raise ValueError("Range end offset precedes start offset")
    return start, min(end, total_size - 1)


# 生成基于固定版本Three.js CDN的自适应内联三维GLB模型预览HTML资源（不含沙箱无效的下载锚点）
def build_model_viewer_html(
    *,
    model_id: str,
    signed_url: str,
    expires_at: str,
    faces: int,
    file_size: int,
) -> str:
    safe_model_id = html.escape(model_id, quote=True)
    safe_model_id_js = json.dumps(model_id)
    safe_url_js = json.dumps(signed_url)
    safe_expires = html.escape(expires_at, quote=True)
    human_expires = html.escape(format_display_timestamp(expires_at), quote=True)
    size_mb = f"{file_size / (1024 * 1024):.2f} MB"
    faces_label = f"约 {faces:,} 面"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1" />
<title>三维视觉预览（{faces_label}）</title>
<style>
  :root {{
    color-scheme: light dark;
    --bg-top: #f6f4ef;
    --bg-bottom: #e8e4dc;
    --panel-bg: rgba(255, 255, 255, 0.88);
    --panel-border: rgba(36, 32, 26, 0.14);
    --text-main: #231f1a;
    --text-muted: #655e54;
    --accent: #7a5c38;
    --accent-hover: #5e4528;
    --btn-bg: rgba(255, 255, 255, 0.92);
    --danger: #b42318;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg-top: #23211d;
      --bg-bottom: #151412;
      --panel-bg: rgba(32, 30, 27, 0.88);
      --panel-border: rgba(255, 255, 255, 0.14);
      --text-main: #f3efe8;
      --text-muted: #aba398;
      --accent: #c59b6d;
      --accent-hover: #d7b286;
      --btn-bg: rgba(44, 41, 37, 0.92);
      --danger: #f97066;
    }}
  }}
  * {{
    box-sizing: border-box;
    margin: 0;
    padding: 0;
  }}
  html, body {{
    width: 100%;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    background: transparent;
    color: var(--text-main);
    overflow: hidden;
  }}
  .viewer-shell {{
    position: relative;
    width: 100%;
    height: 440px;
    border-radius: 12px;
    border: 1px solid var(--panel-border);
    background: radial-gradient(circle at 50% 35%, var(--bg-top), var(--bg-bottom));
    overflow: hidden;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    user-select: none;
  }}
  @media (max-width: 480px) {{
    .viewer-shell {{
      height: 360px;
      border-radius: 10px;
    }}
  }}
  #canvas-container {{
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    touch-action: none;
  }}
  canvas {{
    display: block;
    width: 100%;
    height: 100%;
    outline: none;
  }}
  .top-bar, .bottom-bar {{
    position: relative;
    z-index: 2;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 10px 12px;
    pointer-events: none;
  }}
  .badge {{
    pointer-events: auto;
    background: var(--panel-bg);
    border: 1px solid var(--panel-border);
    backdrop-filter: blur(8px);
    padding: 5px 10px;
    border-radius: 999px;
    font-size: 12px;
    line-height: 1.3;
    color: var(--text-main);
    display: inline-flex;
    align-items: center;
    gap: 6px;
    max-width: 100%;
  }}
  .badge-muted {{
    color: var(--text-muted);
    font-size: 11px;
  }}
  .controls-group {{
    pointer-events: auto;
    display: inline-flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
  }}
  .btn {{
    appearance: none;
    border: 1px solid var(--panel-border);
    background: var(--btn-bg);
    color: var(--text-main);
    border-radius: 8px;
    padding: 6px 11px;
    font-size: 12px;
    font-weight: 500;
    line-height: 1.2;
    cursor: pointer;
    text-decoration: none;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 4px;
    min-height: 32px;
    transition: border-color 0.15s ease, transform 0.1s ease;
  }}
  .btn:hover {{
    border-color: var(--accent);
  }}
  .btn:active {{
    transform: translateY(1px);
  }}
  .btn-primary {{
    background: var(--accent);
    color: #ffffff;
    border-color: transparent;
  }}
  .btn-primary:hover {{
    background: var(--accent-hover);
  }}
  .overlay {{
    position: absolute;
    inset: 0;
    z-index: 3;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 10px;
    padding: 20px;
    text-align: center;
    background: var(--panel-bg);
    backdrop-filter: blur(6px);
  }}
  .overlay[hidden] {{
    display: none !important;
  }}
  .progress-track {{
    width: min(220px, 75vw);
    height: 6px;
    border-radius: 999px;
    background: rgba(122, 92, 56, 0.2);
    overflow: hidden;
  }}
  .progress-fill {{
    height: 100%;
    width: 12%;
    background: var(--accent);
    border-radius: 999px;
    transition: width 0.15s ease;
  }}
  .status-text {{
    font-size: 13px;
    color: var(--text-main);
  }}
  .error-text {{
    font-size: 13px;
    color: var(--danger);
    max-width: 320px;
    line-height: 1.4;
  }}
</style>
<script type="importmap">
{{
  "imports": {{
    "three": "https://cdn.jsdelivr.net/npm/three@0.170.0/build/three.module.js",
    "three/addons/": "https://cdn.jsdelivr.net/npm/three@0.170.0/examples/jsm/"
  }}
}}
</script>
</head>
<body>
  <div class="viewer-shell" id="viewer-shell" data-model-id="{safe_model_id}" role="region" aria-label="三维模型交互预览">
    <div id="canvas-container"></div>

    <div class="top-bar">
      <div class="badge">
        <span>三维视觉模型</span>
        <span class="badge-muted">{faces_label} · {size_mb}</span>
      </div>
      <div class="controls-group">
        <button type="button" class="btn" id="reset-btn" aria-label="重置相机视角">重置视角</button>
      </div>
    </div>

    <div class="overlay" id="loading-state" role="status" aria-live="polite">
      <div class="status-text" id="progress-label">正在加载三维模型...</div>
      <div class="progress-track">
        <div class="progress-fill" id="progress-fill"></div>
      </div>
    </div>

    <div class="overlay" id="error-state" role="alert" hidden>
      <div class="error-text" id="error-message">三维模型加载失败，请点击重试或刷新预览链接。</div>
      <div class="controls-group">
        <button type="button" class="btn" id="retry-btn">重新加载</button>
        <button type="button" class="btn btn-primary" id="renew-btn">刷新预览链接</button>
      </div>
    </div>

    <div class="bottom-bar">
      <div class="badge badge-muted">
        拖拽旋转 · 滚轮/双指缩放 · 右键平移 · 仅供视觉预览（非工程制造模型）
      </div>
      <div class="badge badge-muted" id="model-expires-badge" data-expires="{safe_expires}" title="预览链接本地有效时间">
        有效期至 {human_expires}
      </div>
    </div>
  </div>

  <script type="module">
    const INITIAL_MODEL_URL = {safe_url_js};
    const MODEL_URL = INITIAL_MODEL_URL;
    let currentModelUrl = INITIAL_MODEL_URL;
    const MODEL_ID = {safe_model_id_js};
    const RESOURCE_URI = 'ui://zyb_studio/model/' + MODEL_ID;
    const SESSION_NONCE = (() => {{
      const buf = new Uint32Array(4);
      if (window.crypto && window.crypto.getRandomValues) {{
        window.crypto.getRandomValues(buf);
        return Array.from(buf, (n) => n.toString(16).padStart(8, '0')).join('');
      }}
      return String(Date.now()) + '-' + Math.random().toString(16).slice(2);
    }})();
    let reqCounter = 0;
    let pendingRenewRequestId = null;
    let renderDataReceived = false;

    const shell = document.getElementById('viewer-shell');
    const container = document.getElementById('canvas-container');
    const loadingEl = document.getElementById('loading-state');
    const progressFill = document.getElementById('progress-fill');
    const progressLabel = document.getElementById('progress-label');
    const errorEl = document.getElementById('error-state');
    const errorMsgEl = document.getElementById('error-message');
    const resetBtn = document.getElementById('reset-btn');
    const retryBtn = document.getElementById('retry-btn');
    const renewBtn = document.getElementById('renew-btn');
    const expiresBadge = document.getElementById('model-expires-badge');

    function nextRequestId(prefix) {{
      reqCounter += 1;
      return prefix + '-' + SESSION_NONCE + '-' + reqCounter;
    }}

    function isValidRenewedModelUrl(candidateUrl) {{
      if (typeof candidateUrl !== 'string' || !candidateUrl) return false;
      try {{
        const cand = new URL(candidateUrl, window.location.href);
        if (cand.protocol !== 'http:' && cand.protocol !== 'https:') return false;
        const ref = new URL(INITIAL_MODEL_URL, window.location.href);
        if (cand.origin !== ref.origin) return false;
        return /\\/(?:studio-files|files)\\/[A-Za-z0-9_-]+\\.[0-9a-f]{{64}}$/.test(cand.pathname);
      }} catch (_e) {{
        return false;
      }}
    }}

    function formatLocalExpiry(isoStr) {{
      if (!isoStr) return '';
      const d = new Date(isoStr);
      if (Number.isNaN(d.getTime())) return String(isoStr).replace('T', ' ').replace(/Z$/, '');
      const pad = (n) => String(n).padStart(2, '0');
      return (
        d.getFullYear() +
        '-' +
        pad(d.getMonth() + 1) +
        '-' +
        pad(d.getDate()) +
        ' ' +
        pad(d.getHours()) +
        ':' +
        pad(d.getMinutes())
      );
    }}

    if (expiresBadge && expiresBadge.dataset.expires) {{
      const formatted = formatLocalExpiry(expiresBadge.dataset.expires);
      if (formatted) {{
        expiresBadge.textContent = '有效期至 ' + formatted;
      }}
    }}

    function notifyHostSize() {{
      const height = Math.ceil(shell.getBoundingClientRect().height);
      if (window.parent && window.parent !== window) {{
        window.parent.postMessage({{ type: 'ui-size-change', payload: {{ height }} }}, '*');
      }}
    }}
    notifyHostSize();
    window.addEventListener('resize', notifyHostSize);

    function showError(msg) {{
      loadingEl.hidden = true;
      errorEl.hidden = false;
      errorMsgEl.textContent = msg || '三维模型加载失败，请检查网络或点击“刷新预览链接”重新获取。';
    }}

    let loadModelFn = null;

    function applyRenewedModelLinks(candidate) {{
      if (!candidate || typeof candidate !== 'object') return false;
      if (candidate.modelId && candidate.modelId !== MODEL_ID) return false;
      const nextUrl = candidate.modelUrl || candidate.model_url || candidate.signedUrl || candidate.signed_url;
      if (!isValidRenewedModelUrl(nextUrl)) return false;
      currentModelUrl = nextUrl;
      const nextExpires = candidate.expiresAt || candidate.expires_at;
      if (nextExpires && expiresBadge) {{
        expiresBadge.dataset.expires = nextExpires;
        expiresBadge.textContent = '有效期至 ' + formatLocalExpiry(nextExpires);
      }}
      if (typeof loadModelFn === 'function') {{
        loadModelFn();
      }}
      return true;
    }}

    async function renewModelDirectly() {{
      try {{
        const res = await fetch(currentModelUrl, {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{
            action: 'renew',
            resource_uri: RESOURCE_URI,
            model_url: currentModelUrl
          }})
        }});
        const rawText = await res.text();
        let data = {{}};
        if (rawText) {{
          try {{
            const parsed = JSON.parse(rawText);
            if (parsed && typeof parsed === 'object') data = parsed;
          }} catch (_e) {{}}
        }}
        if (res.ok && applyRenewedModelLinks(data)) {{
          return true;
        }}
      }} catch (_err) {{}}
      return false;
    }}

    if (renewBtn) {{
      renewBtn.addEventListener('click', async () => {{
        errorMsgEl.textContent = '正在刷新三维模型预览链接，请稍候…';
        if (window.parent && window.parent !== window) {{
          pendingRenewRequestId = nextRequestId('renew-3d');
          window.parent.postMessage({{
            type: 'ui-link-renew',
            messageId: pendingRenewRequestId,
            payload: {{
              resourceUri: RESOURCE_URI,
              modelId: MODEL_ID,
              sessionNonce: SESSION_NONCE,
              modelUrl: currentModelUrl
            }}
          }}, '*');
        }}
        const ok = await renewModelDirectly();
        if (!ok && (!window.parent || window.parent === window)) {{
          errorMsgEl.textContent = '预览链接刷新失败，请稍后重试。';
        }}
      }});
    }}

    window.addEventListener('message', (ev) => {{
      if (!window.parent || window.parent === window || ev.source !== window.parent) return;
      if (!ev.data || typeof ev.data !== 'object') return;
      const msgType = ev.data.type;
      const payload = ev.data.payload || {{}};
      if (msgType === 'ui-lifecycle-iframe-render-data' && !renderDataReceived) {{
        renderDataReceived = true;
        const renderData = payload.renderData || payload;
        if (renderData && typeof renderData === 'object') {{
          applyRenewedModelLinks(renderData.renewedLinks || renderData);
        }}
        return;
      }}
      if (msgType === 'ui-message-response' && ev.data.messageId && ev.data.messageId === pendingRenewRequestId) {{
        pendingRenewRequestId = null;
        const resp = payload.response || payload;
        if (!resp || typeof resp !== 'object') return;
        if (resp.sessionNonce && resp.sessionNonce !== SESSION_NONCE) return;
        applyRenewedModelLinks(resp);
      }}
    }});

    try {{
      const THREE = await import('three');
      const {{ OrbitControls }} = await import('three/addons/controls/OrbitControls.js');
      const {{ GLTFLoader }} = await import('three/addons/loaders/GLTFLoader.js');
      const {{ RoomEnvironment }} = await import('three/addons/environments/RoomEnvironment.js');

      const renderer = new THREE.WebGLRenderer({{ antialias: true, alpha: true, powerPreference: 'high-performance' }});
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
      renderer.setSize(container.clientWidth || 640, container.clientHeight || 400);
      renderer.outputColorSpace = THREE.SRGBColorSpace;
      renderer.toneMapping = THREE.ACESFilmicToneMapping;
      renderer.toneMappingExposure = 1.05;
      renderer.shadowMap.enabled = true;
      renderer.shadowMap.type = THREE.PCFSoftShadowMap;
      container.appendChild(renderer.domElement);

      const scene = new THREE.Scene();
      const pmremGenerator = new THREE.PMREMGenerator(renderer);
      scene.environment = pmremGenerator.fromScene(new RoomEnvironment(), 0.04).texture;

      const hemiLight = new THREE.HemisphereLight(0xfffbf2, 0x4b453c, 0.85);
      hemiLight.position.set(0, 10, 0);
      scene.add(hemiLight);

      const keyLight = new THREE.DirectionalLight(0xfff7e8, 1.65);
      keyLight.position.set(4, 8, 5);
      keyLight.castShadow = true;
      keyLight.shadow.mapSize.width = 1024;
      keyLight.shadow.mapSize.height = 1024;
      keyLight.shadow.bias = -0.0005;
      scene.add(keyLight);

      const rimLight = new THREE.DirectionalLight(0xdce6f7, 0.65);
      rimLight.position.set(-5, 4, -5);
      scene.add(rimLight);

      const shadowPlane = new THREE.Mesh(
        new THREE.PlaneGeometry(20, 20),
        new THREE.ShadowMaterial({{ opacity: 0.24 }})
      );
      shadowPlane.rotation.x = -Math.PI / 2;
      shadowPlane.position.y = 0;
      shadowPlane.receiveShadow = true;
      scene.add(shadowPlane);

      const camera = new THREE.PerspectiveCamera(
        40,
        (container.clientWidth || 640) / (container.clientHeight || 400),
        0.01,
        100
      );
      const controls = new OrbitControls(camera, renderer.domElement);
      controls.enableDamping = true;
      controls.dampingFactor = 0.06;
      controls.enablePan = true;
      controls.enableZoom = true;

      const defaultView = {{
        pos: new THREE.Vector3(2.2, 1.5, 2.6),
        target: new THREE.Vector3(0, 0.45, 0),
      }};
      camera.position.copy(defaultView.pos);
      controls.target.copy(defaultView.target);
      controls.update();

      let currentModel = null;
      const loader = new GLTFLoader();
      loader.setCrossOrigin('anonymous');

      function fitCameraToModel(object3d) {{
        const box = new THREE.Box3().setFromObject(object3d);
        if (box.isEmpty()) return;
        const size = box.getSize(new THREE.Vector3());
        const center = box.getCenter(new THREE.Vector3());
        const maxDim = Math.max(size.x, size.y, size.z, 0.001);
        const scale = 1.6 / maxDim;
        object3d.scale.setScalar(scale);

        const scaledBox = new THREE.Box3().setFromObject(object3d);
        const scaledCenter = scaledBox.getCenter(new THREE.Vector3());
        const scaledSize = scaledBox.getSize(new THREE.Vector3());
        object3d.position.x += -scaledCenter.x;
        object3d.position.z += -scaledCenter.z;
        object3d.position.y += -scaledBox.min.y;

        const radius = Math.max(scaledSize.length() * 0.5, 0.4);
        const fovRad = THREE.MathUtils.degToRad(camera.fov);
        const distance = (radius / Math.sin(fovRad / 2)) * 1.12;
        defaultView.target.set(0, scaledSize.y * 0.45, 0);
        defaultView.pos.set(distance * 0.68, scaledSize.y * 0.65 + distance * 0.35, distance * 0.82);

        camera.near = Math.max(distance / 100, 0.005);
        camera.far = Math.max(distance * 25, 50);
        camera.updateProjectionMatrix();

        controls.minDistance = radius * 0.35;
        controls.maxDistance = distance * 4.5;
        controls.maxPolarAngle = Math.PI * 0.495;
        camera.position.copy(defaultView.pos);
        controls.target.copy(defaultView.target);
        controls.update();
      }}

      function loadModel() {{
        errorEl.hidden = true;
        loadingEl.hidden = false;
        progressFill.style.width = '12%';
        progressLabel.textContent = '正在加载三维模型...';

        loader.load(
          currentModelUrl,
          (gltf) => {{
            if (currentModel) {{
              scene.remove(currentModel);
            }}
            currentModel = gltf.scene;
            currentModel.traverse((node) => {{
              if (node.isMesh) {{
                node.castShadow = true;
                node.receiveShadow = true;
              }}
            }});
            scene.add(currentModel);
            fitCameraToModel(currentModel);
            loadingEl.hidden = true;
            notifyHostSize();
          }},
          (event) => {{
            if (event && event.total > 0) {{
              const pct = Math.min(100, Math.max(5, Math.round((event.loaded / event.total) * 100)));
              progressFill.style.width = pct + '%';
              progressLabel.textContent = '正在加载三维模型... ' + pct + '%';
            }}
          }},
          () => {{
            showError('三维模型加载失败或签名下载链接已过期，请点击“刷新预览链接”或使用对话消息中的 GLB 下载链接。');
          }}
        );
      }}
      loadModelFn = loadModel;

      resetBtn.addEventListener('click', () => {{
        camera.position.copy(defaultView.pos);
        controls.target.copy(defaultView.target);
        controls.update();
      }});
      retryBtn.addEventListener('click', loadModel);

      const resizeObserver = new ResizeObserver(() => {{
        const w = container.clientWidth || 640;
        const h = container.clientHeight || 400;
        camera.aspect = w / h;
        camera.updateProjectionMatrix();
        renderer.setSize(w, h);
        notifyHostSize();
      }});
      resizeObserver.observe(container);

      function animate() {{
        requestAnimationFrame(animate);
        controls.update();
        renderer.render(scene, camera);
      }}
      animate();
      loadModel();
    }} catch (err) {{
      showError('浏览器三维渲染组件初始化失败，请使用对话消息中的 GLB 下载链接查看。');
    }}
  </script>
</body>
</html>"""


# 封装单个三维模型任务的完整并发准入租约（含每用户配额、全局配额与Seed3D并发配额）
class _ModelAdmissionLease:
    def __init__(
        self,
        user_lease: ConcurrencyLease,
        seed3d_sem: asyncio.Semaphore,
    ) -> None:
        self._user_lease = user_lease
        self._seed3d_sem = seed3d_sem
        self._released = False

    # 查询当前三维模型并发租约是否已释放
    @property
    def released(self) -> bool:
        return self._released

    # 幂等释放Seed3D并发信号量与每用户/全局并发租约
    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._seed3d_sem.release()
        self._user_lease.release()


# 管理豆包Seed3D异步任务的提交、退避轮询、后台续跑、落盘与状态恢复
class ModelJobManager:
    def __init__(
        self,
        settings: StudioSettings,
        user_limiter: ConcurrencyLimiter,
        *,
        ark_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._user_limiter = user_limiter
        self._seed3d_sem = asyncio.Semaphore(settings.seed3d_max_parallel)
        self._ark_transport = ark_transport
        self._background_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._job_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._active_leases: dict[tuple[str, str], _ModelAdmissionLease] = {}
        self._known_task_ids: dict[tuple[str, str], str] = {}

    # 获取指定（user_id, model_id）的专属异步互斥锁以串行化轮询与状态迁移
    def _get_job_lock(self, uid: str, model_id: str) -> asyncio.Lock:
        key = (uid, model_id)
        lock = self._job_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._job_locks[key] = lock
        return lock

    # 获取单次三维模型任务的完整并发准入租约（每用户+全局+Seed3D信号量）
    async def _acquire_model_lease(self, uid: str) -> _ModelAdmissionLease:
        user_lease = await self._user_limiter.acquire_lease(uid)
        try:
            await self._seed3d_sem.acquire()
        except BaseException:
            user_lease.release()
            raise
        return _ModelAdmissionLease(user_lease, self._seed3d_sem)

    # 幂等释放并移除指定任务的并发准入租约
    def _release_job_lease(
        self,
        uid: str,
        model_id: str,
        lease: _ModelAdmissionLease | None = None,
    ) -> None:
        stored = self._active_leases.pop((uid, model_id), None)
        if stored is not None:
            stored.release()
        if lease is not None and lease is not stored:
            lease.release()

    # 从receipt或内存缓存中提取已记录的远端provider_task_id
    def _extract_provider_task_id(
        self, uid: str, model_id: str, receipt: dict[str, Any]
    ) -> str | None:
        raw_task_id = (
            receipt.get("provider_task_id")
            or receipt.get("task_id")
            or self._known_task_ids.get((uid, model_id))
        )
        if isinstance(raw_task_id, str) and raw_task_id.strip():
            cleaned = raw_task_id.strip()
            receipt["provider_task_id"] = cleaned
            self._known_task_ids[(uid, model_id)] = cleaned
            return cleaned
        return None

    # 创建访问火山方舟3D任务接口的HTTP客户端（总超时60秒、连接超时10秒以加速双栈回退）
    def _build_client(self) -> httpx.AsyncClient:
        timeout = httpx.Timeout(60.0, connect=10.0)
        return httpx.AsyncClient(
            transport=self._ark_transport,
            timeout=timeout,
            follow_redirects=False,
        )

    # 在单任务锁保护下查询单次远端3D生成任务状态并在成功时下载和落盘GLB文件
    async def _poll_once_and_finalize_if_ready(
        self,
        client: httpx.AsyncClient,
        *,
        uid: str,
        model_id: str,
        model_dir: Path,
        receipt: dict[str, Any],
        provider_task_id: str,
    ) -> dict[str, Any]:
        async with self._get_job_lock(uid, model_id):
            existing = _read_existing_receipt_dict(model_dir)
            if existing is not None and existing.get("state") == "saved":
                receipt.clear()
                receipt.update(existing)
            if receipt.get("state") == "saved" and (model_dir / "model.glb").is_file():
                glb_bytes, saved_receipt = load_saved_model_bytes(
                    self._settings, uid, model_id
                )
                return {
                    "state": "saved",
                    "model_id": model_id,
                    "job_id": model_id,
                    "source_image_id": str(saved_receipt.get("source_image_id") or ""),
                    "detail": str(saved_receipt.get("detail") or "standard"),
                    "faces": int(saved_receipt.get("faces") or 100_000),
                    "bytes": len(glb_bytes),
                    "sha256": str(saved_receipt.get("sha256") or ""),
                }

            receipt["provider_task_id"] = provider_task_id
            self._known_task_ids[(uid, model_id)] = provider_task_id

            tasks_base_url = (
                f"{self._settings.ark_base_url.rstrip('/')}/contents/generations/tasks"
            )
            headers = {
                "Authorization": f"Bearer {self._settings.ark_api_key}",
                "Content-Type": "application/json",
            }
            try:
                query_res = await client.get(
                    f"{tasks_base_url}/{provider_task_id}",
                    headers=headers,
                )
                query_res.raise_for_status()
                parsed = parse_seed3d_query_response(query_res.json())
            except httpx.HTTPStatusError as exc:
                # 查询接口HTTP异常（含401/403/404/429/5xx）仅表示状态查询失败，绝不能误判为远端任务failed
                _transition_model_receipt(
                    model_dir,
                    receipt,
                    "uncertain",
                    best_effort=True,
                    finished=time.time(),
                    provider_task_id=provider_task_id,
                    query_error_code=f"http_{exc.response.status_code}",
                )
                raise GenerationUncertain(
                    "3D model generation task status query failed; the request may have been billed. Do not automatically resubmit.",
                    job_id=model_id,
                    state="uncertain",
                ) from None
            except (
                httpx.TimeoutException,
                httpx.TransportError,
                ValueError,
                RuntimeError,
            ) as exc:
                _transition_model_receipt(
                    model_dir,
                    receipt,
                    "uncertain",
                    best_effort=True,
                    finished=time.time(),
                    provider_task_id=provider_task_id,
                )
                raise GenerationUncertain(
                    "3D model generation outcome is uncertain; the request may have been billed. Do not automatically resubmit.",
                    job_id=model_id,
                    state="uncertain",
                ) from exc

            status = parsed["status"]
            if status in ("queued", "running"):
                if receipt.get("state") != "running":
                    _transition_model_receipt(
                        model_dir,
                        receipt,
                        "running",
                        best_effort=True,
                        provider_task_id=provider_task_id,
                    )
                return {"state": "running", "provider_status": status}

            if status in ("failed", "cancelled", "expired"):
                _transition_model_receipt(
                    model_dir,
                    receipt,
                    "failed",
                    best_effort=True,
                    finished=time.time(),
                    error_code=parsed.get("error_code", status),
                    provider_task_id=provider_task_id,
                )
                raise GenerationUncertain(
                    f"3D model generation task {status} upstream; the request may have been billed. Do not automatically resubmit.",
                    job_id=model_id,
                    state="failed",
                )

            file_url = parsed["file_url"]
            try:
                dl_res = await client.get(file_url)
                dl_res.raise_for_status()
                glb_bytes = extract_glb_bytes(dl_res.content)
            except Exception as exc:
                _transition_model_receipt(
                    model_dir,
                    receipt,
                    "uncertain",
                    best_effort=True,
                    finished=time.time(),
                    provider_task_id=provider_task_id,
                )
                raise GenerationUncertain(
                    "3D model artifact download failed after upstream task completion; the request may have been billed. Do not automatically resubmit.",
                    job_id=model_id,
                    state="uncertain",
                ) from exc

            digest = hashlib.sha256(glb_bytes).hexdigest()
            try:
                write_glb_atomic(model_dir, glb_bytes)
                _transition_model_receipt(
                    model_dir,
                    receipt,
                    "saved",
                    best_effort=False,
                    file="model.glb",
                    bytes=len(glb_bytes),
                    sha256=digest,
                    finished=time.time(),
                    provider_task_id=provider_task_id,
                )
            except Exception as exc:
                _transition_model_receipt(
                    model_dir,
                    receipt,
                    "uncertain",
                    best_effort=True,
                    finished=time.time(),
                    provider_task_id=provider_task_id,
                )
                raise GenerationUncertain(
                    "3D model local persistence failed after upstream task completion; the request may have been billed. Do not automatically resubmit.",
                    job_id=model_id,
                    state="uncertain",
                ) from exc

            return {
                "state": "saved",
                "model_id": model_id,
                "job_id": model_id,
                "source_image_id": str(receipt.get("source_image_id") or ""),
                "detail": str(receipt.get("detail") or "standard"),
                "faces": int(receipt.get("faces") or 100_000),
                "bytes": len(glb_bytes),
                "sha256": digest,
            }

    # 在后台继续持有准入租约并轮询尚未完成的Seed3D任务直至终态或超时
    async def _background_poll_loop(
        self,
        user_id: str,
        model_id: str,
        model_dir: Path,
        receipt: dict[str, Any],
        provider_task_id: str,
        lease: _ModelAdmissionLease,
    ) -> None:
        deadline = time.monotonic() + float(
            self._settings.seed3d_background_timeout_seconds
        )
        delay = float(self._settings.seed3d_poll_initial_seconds)
        max_interval = float(self._settings.seed3d_poll_max_interval_seconds)
        try:
            async with self._build_client() as client:
                while time.monotonic() < deadline:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(delay, remaining))
                    delay = min(max_interval, delay * 1.5)
                    outcome = await self._poll_once_and_finalize_if_ready(
                        client,
                        uid=user_id,
                        model_id=model_id,
                        model_dir=model_dir,
                        receipt=receipt,
                        provider_task_id=provider_task_id,
                    )
                    if outcome["state"] == "saved":
                        return
            # 后台总轮询窗口耗尽仍未完成，在单任务锁保护下标记为uncertain（不覆盖saved终态）
            async with self._get_job_lock(user_id, model_id):
                _transition_model_receipt(
                    model_dir,
                    receipt,
                    "uncertain",
                    best_effort=True,
                    finished=time.time(),
                    provider_task_id=provider_task_id,
                )
        except (GenerationUncertain, RuntimeError, ValueError, OSError):
            return
        except asyncio.CancelledError:
            raise
        finally:
            self._background_tasks.pop((user_id, model_id), None)
            self._release_job_lease(user_id, model_id, lease)

    # 提交新的三维模型生成任务并在工具调用窗口内退避轮询，超时则无空隙移交后台继续轮询并返回job_id
    async def create_model(
        self,
        *,
        user_id: str,
        source_image_id: str,
        image_data_uri: str,
        detail: str = "standard",
        note: str | None = None,
    ) -> dict[str, Any]:
        uid = validate_user_id(user_id)
        payload, subdivision_level, faces = build_seed3d_create_payload(
            self._settings.seed3d_model,
            image_data_uri,
            detail=detail,
            note=note,
        )
        clean_note = sanitize_model_note(note)

        lease = await self._acquire_model_lease(uid)
        model_id = ""
        transferred_to_bg = False
        try:
            model_id, model_dir = allocate_model_job(self._settings, uid)
            self._active_leases[(uid, model_id)] = lease
            started_ts = time.time()
            receipt: dict[str, Any] = {
                "state": "pending",
                "model_id": model_id,
                "job_id": model_id,
                "user_id": uid,
                "source_image_id": source_image_id,
                "detail": detail,
                "subdivision_level": subdivision_level,
                "faces": faces,
                "note": clean_note,
                "model": self._settings.seed3d_model,
                "started": started_ts,
            }
            write_model_receipt(model_dir, receipt)

            tasks_url = (
                f"{self._settings.ark_base_url.rstrip('/')}/contents/generations/tasks"
            )
            headers = {
                "Authorization": f"Bearer {self._settings.ark_api_key}",
                "Content-Type": "application/json",
            }

            try:
                async with self._build_client() as client:
                    try:
                        create_res = await client.post(
                            tasks_url, headers=headers, json=payload
                        )
                        create_res.raise_for_status()
                        provider_task_id = parse_seed3d_create_response(
                            create_res.json()
                        )
                    except httpx.HTTPStatusError as exc:
                        if (
                            400 <= exc.response.status_code < 500
                            and exc.response.status_code != 429
                        ):
                            _transition_model_receipt(
                                model_dir,
                                receipt,
                                "failed",
                                best_effort=True,
                                finished=time.time(),
                                error_code=f"http_{exc.response.status_code}",
                            )
                            raise GenerationUncertain(
                                "3D model generation request was rejected by upstream service; the request may have been billed. Do not automatically resubmit.",
                                job_id=model_id,
                                state="failed",
                            ) from None
                        _transition_model_receipt(
                            model_dir,
                            receipt,
                            "uncertain",
                            best_effort=True,
                            finished=time.time(),
                        )
                        raise GenerationUncertain(
                            "3D model generation outcome is uncertain; the request may have been billed. Do not automatically resubmit.",
                            job_id=model_id,
                            state="uncertain",
                        ) from None
                    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                        # 连接阶段尚未发送任何请求字节，直接归类为failed且未计费（不产生uncertain警告）
                        _transition_model_receipt(
                            model_dir,
                            receipt,
                            "failed",
                            best_effort=True,
                            finished=time.time(),
                            billed=False,
                            error_code="connect_error",
                        )
                        connect_err = RuntimeError(
                            "3D model generation service connection failed before request dispatch "
                            "(the request was not billed); no automatic retry was performed"
                        )
                        connect_err.state = "failed"  # type: ignore[attr-defined]
                        connect_err.billed = False  # type: ignore[attr-defined]
                        connect_err.job_id = model_id  # type: ignore[attr-defined]
                        raise connect_err from exc
                    except (
                        httpx.TimeoutException,
                        httpx.TransportError,
                        ValueError,
                        RuntimeError,
                    ) as exc:
                        _transition_model_receipt(
                            model_dir,
                            receipt,
                            "uncertain",
                            best_effort=True,
                            finished=time.time(),
                        )
                        raise GenerationUncertain(
                            "3D model generation outcome is uncertain; the request may have been billed. Do not automatically resubmit.",
                            job_id=model_id,
                            state="uncertain",
                        ) from exc

                    # 立即在内存与receipt中记录provider_task_id，确保后续落盘失败也可通过GET恢复
                    self._known_task_ids[(uid, model_id)] = provider_task_id
                    receipt["provider_task_id"] = provider_task_id
                    try:
                        _transition_model_receipt(
                            model_dir,
                            receipt,
                            "running",
                            best_effort=False,
                            provider_task_id=provider_task_id,
                        )
                    except Exception as exc:
                        _transition_model_receipt(
                            model_dir,
                            receipt,
                            "uncertain",
                            best_effort=True,
                            finished=time.time(),
                            provider_task_id=provider_task_id,
                        )
                        raise GenerationUncertain(
                            "3D model local receipt persistence failed after upstream task creation; the request may have been billed. Do not automatically resubmit.",
                            job_id=model_id,
                            state="uncertain",
                        ) from exc

                    deadline = time.monotonic() + float(
                        self._settings.seed3d_poll_timeout_seconds
                    )
                    delay = float(self._settings.seed3d_poll_initial_seconds)
                    max_interval = float(
                        self._settings.seed3d_poll_max_interval_seconds
                    )

                    while True:
                        outcome = await self._poll_once_and_finalize_if_ready(
                            client,
                            uid=uid,
                            model_id=model_id,
                            model_dir=model_dir,
                            receipt=receipt,
                            provider_task_id=provider_task_id,
                        )
                        if outcome["state"] == "saved":
                            return outcome
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        await asyncio.sleep(min(delay, remaining))
                        delay = min(max_interval, delay * 1.5)
            except BaseException:
                if receipt.get("state") == "pending":
                    _transition_model_receipt(
                        model_dir,
                        receipt,
                        "uncertain",
                        best_effort=True,
                        finished=time.time(),
                    )
                raise

            # 工具内轮询窗口尚未完成，将准入租约直接移交后台续跑任务（不释放再重获）并返回job_id
            bg_task = asyncio.create_task(
                self._background_poll_loop(
                    uid, model_id, model_dir, receipt, provider_task_id, lease
                )
            )
            self._background_tasks[(uid, model_id)] = bg_task
            transferred_to_bg = True
            return {
                "state": "running",
                "job_id": model_id,
                "model_id": model_id,
                "source_image_id": source_image_id,
                "detail": detail,
                "faces": faces,
            }
        finally:
            if not transferred_to_bg:
                if model_id:
                    self._release_job_lease(uid, model_id, lease)
                else:
                    lease.release()

    # 通过GET轮询恢复或查询指定job_id的三维模型生成任务（绝不触发二次创建POST）
    async def check_model(self, *, user_id: str, job_id: str) -> dict[str, Any]:
        uid = validate_user_id(user_id)
        mid = validate_model_id(job_id)

        bg_task = self._background_tasks.get((uid, mid))
        if bg_task is not None and bg_task.done():
            self._background_tasks.pop((uid, mid), None)
            with contextlib.suppress(Exception):
                await bg_task

        model_dir, receipt = read_model_receipt(self._settings, uid, mid)
        provider_task_id = self._extract_provider_task_id(uid, mid, receipt)
        state = receipt.get("state")

        if state == "saved":
            active_bg = self._background_tasks.pop((uid, mid), None)
            if active_bg is not None and not active_bg.done():
                active_bg.cancel()
            self._release_job_lease(uid, mid)
            glb_bytes, saved_receipt = load_saved_model_bytes(self._settings, uid, mid)
            return {
                "state": "saved",
                "model_id": mid,
                "job_id": mid,
                "source_image_id": str(saved_receipt.get("source_image_id") or ""),
                "detail": str(saved_receipt.get("detail") or "standard"),
                "faces": int(saved_receipt.get("faces") or 100_000),
                "bytes": len(glb_bytes),
                "sha256": str(saved_receipt.get("sha256") or ""),
            }

        if state == "failed":
            self._release_job_lease(uid, mid)
            if (
                receipt.get("billed") is False
                or receipt.get("error_code") == "connect_error"
            ):
                connect_err = RuntimeError(
                    "3D model generation service connection failed before request dispatch "
                    "(the request was not billed); no automatic retry was performed"
                )
                connect_err.state = "failed"  # type: ignore[attr-defined]
                connect_err.billed = False  # type: ignore[attr-defined]
                connect_err.job_id = mid  # type: ignore[attr-defined]
                raise connect_err
            raise GenerationUncertain(
                "3D model generation task failed; the request may have been billed. Do not automatically resubmit.",
                job_id=mid,
                state="failed",
            )

        # 只要存在provider_task_id（无论receipt处于running、uncertain或pending），均通过GET轮询恢复
        if provider_task_id is None:
            _transition_model_receipt(
                model_dir,
                receipt,
                "uncertain",
                best_effort=True,
                finished=time.time(),
            )
            self._release_job_lease(uid, mid)
            raise GenerationUncertain(
                "3D model generation outcome is uncertain; the request may have been billed. Do not automatically resubmit.",
                job_id=mid,
                state="uncertain",
            )

        existing_lease = self._active_leases.get((uid, mid))
        if existing_lease is not None and not existing_lease.released:
            lease = existing_lease
            acquired_here = False
        else:
            lease = await self._acquire_model_lease(uid)
            self._active_leases[(uid, mid)] = lease
            acquired_here = True

        transferred_to_bg = False
        try:
            async with self._build_client() as client:
                outcome = await self._poll_once_and_finalize_if_ready(
                    client,
                    uid=uid,
                    model_id=mid,
                    model_dir=model_dir,
                    receipt=receipt,
                    provider_task_id=provider_task_id,
                )
            if outcome["state"] == "saved":
                active_bg = self._background_tasks.pop((uid, mid), None)
                if active_bg is not None and not active_bg.done():
                    active_bg.cancel()
                self._release_job_lease(uid, mid, lease)
                return outcome

            active_bg = self._background_tasks.get((uid, mid))
            if active_bg is None or active_bg.done():
                new_bg = asyncio.create_task(
                    self._background_poll_loop(
                        uid, mid, model_dir, receipt, provider_task_id, lease
                    )
                )
                self._background_tasks[(uid, mid)] = new_bg
            transferred_to_bg = True
            return {
                "state": "running",
                "job_id": mid,
                "model_id": mid,
                "source_image_id": str(receipt.get("source_image_id") or ""),
                "detail": str(receipt.get("detail") or "standard"),
                "faces": int(receipt.get("faces") or 100_000),
            }
        except BaseException:
            active_bg = self._background_tasks.pop((uid, mid), None)
            if active_bg is not None and not active_bg.done():
                active_bg.cancel()
            if acquired_here or active_bg is not None:
                self._release_job_lease(uid, mid, lease)
            raise
        finally:
            if acquired_here and not transferred_to_bg:
                self._release_job_lease(uid, mid, lease)

    # 关闭应用时清理所有未完成的后台轮询任务并释放持有的并发租约
    async def shutdown(self) -> None:
        tasks = list(self._background_tasks.values())
        self._background_tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for key, lease in list(self._active_leases.items()):
            self._active_leases.pop(key, None)
            lease.release()
