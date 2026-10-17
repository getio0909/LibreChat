import asyncio
import fcntl
import hashlib
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from .config import StudioSettings
from .store import read_bytes_no_follow, user_studio_dir


def comparison_key(
    *,
    conversation_id: str | None,
    base_id: str,
    base_sha256: str,
    target: str,
    material_ids: list[str],
    sample_fingerprints: list[list[str]],
    size: str,
    request_key: str,
) -> str:
    payload = [
        "compare-materials-v1",
        conversation_id or "",
        base_id,
        base_sha256,
        target,
        material_ids,
        sample_fingerprints,
        size,
        request_key,
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def comparison_job_id(key: str, index: int) -> str:
    digest = hashlib.sha256(f"{key}:{index}".encode("ascii")).hexdigest()
    return f"img_{digest[:32]}"


def comparison_job_dir(settings: StudioSettings, user_id: str, job_id: str) -> Path:
    jobs_dir = user_studio_dir(settings, user_id, create=True) / "jobs"
    if jobs_dir.is_symlink():
        raise ValueError("Invalid comparison jobs directory")
    jobs_dir.mkdir(mode=0o700, exist_ok=True)
    job_dir = jobs_dir / job_id
    if job_dir.is_symlink():
        raise ValueError("Invalid comparison job directory")
    job_dir.mkdir(mode=0o700, exist_ok=True)
    return job_dir


def saved_comparison_result(
    settings: StudioSettings, user_id: str, job_id: str
) -> tuple[dict[str, Any] | None, str | None, bool] | None:
    job_dir = comparison_job_dir(settings, user_id, job_id)
    receipt_file = job_dir / "receipt.json"
    if not receipt_file.exists():
        if any(job_dir.iterdir()):
            return None, "Previous comparison job has no receipt; no paid retry was made", True
        return None

    user_dir = user_studio_dir(settings, user_id)
    try:
        receipt = json.loads(read_bytes_no_follow(receipt_file, user_dir))
        if not isinstance(receipt, dict):
            raise ValueError("Invalid comparison receipt")
        if receipt.get("state") == "saved":
            filename = receipt.get("file")
            if filename not in ("original.png", "original.jpg", "original.webp"):
                raise ValueError("Invalid comparison file")
            raw_bytes = read_bytes_no_follow(job_dir / filename, user_dir)
            digest = hashlib.sha256(raw_bytes).hexdigest()
            if digest != receipt.get("sha256"):
                raise ValueError("Comparison checksum mismatch")
            return (
                {
                    "id": job_id,
                    "raw_bytes": raw_bytes,
                    "width": int(receipt["width"]),
                    "height": int(receipt["height"]),
                    "sha256": digest,
                },
                None,
                False,
            )
        if receipt.get("state") == "failed" and receipt.get("billed") is False:
            return None, "Previous comparison variant failed; not billed", False
        if receipt.get("state") == "rejected":
            return None, "Previous comparison variant was rejected; no paid retry was made", False
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        pass
    return None, "Previous comparison variant may have been billed; no paid retry was made", True


@asynccontextmanager
async def comparison_guard(
    settings: StudioSettings, user_id: str, key: str
) -> AsyncIterator[None]:
    comparison_dir = user_studio_dir(settings, user_id, create=True) / "comparisons"
    if comparison_dir.is_symlink():
        raise ValueError("Invalid comparison directory")
    comparison_dir.mkdir(mode=0o700, exist_ok=True)
    descriptor = os.open(
        comparison_dir / f"{key}.lock",
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.05)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
