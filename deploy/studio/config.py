import asyncio
import json
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

# 默认密钥配置文件路径、规则数据目录与共享令牌64位十六进制正则
DEFAULT_SETTINGS_PATH = Path("/run/secrets/studio.json")
RULES_DIR = Path(__file__).resolve().parent / "rules"
SHARED_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{64}$")


# 石材目录条目模型
class StoneMaterial(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    search_terms: list[str] = Field(default_factory=list)
    group: str = ""
    notes: list[str] = Field(default_factory=list)
    sample_asset_ids: list[str] = Field(default_factory=list)
    all_slab_asset_ids: list[str] = Field(default_factory=list)


# 从规则目录加载的提示词与业务规则数据
class StudioRules(BaseModel):
    model_config = ConfigDict(extra="ignore")

    edit_rules: str
    edit_prefix: str
    materials: list[StoneMaterial]


# 工作台运行设置模型
class StudioSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    shared_token: str
    file_signing_key: str
    public_base_url: str = "https://dev.xuqian.io"
    file_token_ttl_seconds: int = Field(default=86400, ge=60, le=604800)
    image_provider: Literal["gemini", "seedream"] = "gemini"
    gemini_image_model: str = "gemini-nano-banana-2.1"
    enable_3d: bool = False
    gemini_api_key: str
    ark_api_key: str = ""
    seedream_model: str = "doubao-seedream-5-0-pro-260628"
    seed3d_model: str = "doubao-seed3d-2-0-260328"
    max_parallel_per_user: int = Field(default=3, ge=1, le=10)
    global_max_parallel: int = Field(default=12, ge=1, le=64)
    seed3d_max_parallel: int = Field(default=5, ge=1, le=32)
    seedream_timeout_seconds: int = Field(default=240, ge=5, le=600)
    seed3d_poll_timeout_seconds: float = Field(default=480.0, gt=0.0, le=600.0)
    seed3d_poll_initial_seconds: float = Field(default=2.0, gt=0.0, le=60.0)
    seed3d_poll_max_interval_seconds: float = Field(default=15.0, gt=0.0, le=60.0)
    seed3d_background_timeout_seconds: float = Field(default=1800.0, gt=0.0, le=3600.0)
    librechat_images_dir: Path = Path("/librechat/images")
    data_dir: Path = Path("/data")
    kb_root: Path = Path("/kb")
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"

    # 校验shared_token与file_signing_key必须为64位十六进制字符串
    @field_validator("shared_token", "file_signing_key")
    @classmethod
    def _validate_hex_64_key(cls, value: str, info: ValidationInfo) -> str:
        cleaned = value.strip()
        if not SHARED_TOKEN_RE.fullmatch(cleaned):
            raise ValueError(
                f"{info.field_name} must be an exact 64-character hexadecimal string"
            )
        return cleaned

    # 校验public_base_url为合法的HTTP/HTTPS基础地址并移除末尾斜杠
    @field_validator("public_base_url")
    @classmethod
    def _validate_public_base_url(cls, value: str) -> str:
        cleaned = value.strip().rstrip("/")
        if (
            not cleaned
            or any(ch.isspace() for ch in cleaned)
            or not cleaned.startswith(("https://", "http://"))
        ):
            raise ValueError("public_base_url must be a valid http(s) base URL")
        return cleaned

    # 校验必填API密钥不包含空白字符且非空
    @field_validator("gemini_api_key")
    @classmethod
    def _validate_secret(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or any(ch.isspace() for ch in cleaned):
            raise ValueError(
                "Secret configuration value must be a non-empty string without whitespace"
            )
        return cleaned

    # 校验可选ARK密钥：为空时允许缺省，非空时禁止包含空白字符
    @field_validator("ark_api_key")
    @classmethod
    def _validate_optional_ark_secret(cls, value: str) -> str:
        if value == "":
            return ""
        cleaned = value.strip()
        if not cleaned or any(ch.isspace() for ch in cleaned):
            raise ValueError(
                "Secret configuration value must be a non-empty string without whitespace"
            )
        return cleaned

    # 当启用seedream生图或enable_3d时要求提供有效的ark_api_key
    @model_validator(mode="after")
    def _validate_provider_requirements(self) -> "StudioSettings":
        if (
            self.image_provider == "seedream" or self.enable_3d
        ) and not self.ark_api_key:
            raise ValueError(
                "ark_api_key is required when image_provider is 'seedream' or enable_3d is true"
            )
        return self


# 从受保护的JSON文件加载工作台运行配置
def load_settings(path: str | Path | None = None) -> StudioSettings:
    resolved_path = Path(
        path
        or os.environ.get("STUDIO_SETTINGS_PATH")
        or os.environ.get("STUDIO_CONFIG_PATH")
        or DEFAULT_SETTINGS_PATH
    )
    if resolved_path.is_symlink():
        raise ValueError("Settings file must not be a symlink")
    if not resolved_path.is_file():
        raise ValueError(f"Settings file not found: {resolved_path}")
    mode = resolved_path.stat().st_mode
    if mode & 0o077:
        raise ValueError(
            "Settings file permissions must not grant group or world access (expected 0600 or 0400)"
        )
    raw = json.loads(resolved_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("Settings file must contain a JSON object")
    return StudioSettings.model_validate(raw)


# 从rules目录加载提示词规则与石材名称映射数据
def load_rules(rules_dir: str | Path = RULES_DIR) -> StudioRules:
    base = Path(rules_dir)
    if base.is_symlink() or not base.is_dir():
        raise ValueError("Rules directory is invalid")
    image_cfg = json.loads((base / "studio-image.json").read_text(encoding="utf-8"))
    runtime_cfg = json.loads((base / "studio-runtime.json").read_text(encoding="utf-8"))
    materials_cfg = json.loads(
        (base / "studio-materials.json").read_text(encoding="utf-8")
    )
    return StudioRules(
        edit_rules=image_cfg["edit_rules"],
        edit_prefix=runtime_cfg["edit_prefix"],
        materials=[StoneMaterial.model_validate(item) for item in materials_cfg],
    )


# 表示已获取的单用户与全局并发配额租约（支持跨协程与后台轮询移交生命周期）
class ConcurrencyLease:
    def __init__(
        self,
        user_sem: asyncio.Semaphore,
        global_sem: asyncio.Semaphore,
    ) -> None:
        self._user_sem = user_sem
        self._global_sem = global_sem
        self._released = False

    # 查询当前租约是否已释放
    @property
    def released(self) -> bool:
        return self._released

    # 幂等释放持有的全局与单用户并发信号量
    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._global_sem.release()
        self._user_sem.release()


# 按用户与全局上限控制并发任务的信号量管理器
class ConcurrencyLimiter:
    def __init__(self, max_per_user: int = 3, global_max: int = 12) -> None:
        self._max_per_user = max_per_user
        self._global_max = global_max
        self._global_sem = asyncio.Semaphore(global_max)
        self._user_sems: dict[str, asyncio.Semaphore] = {}

    # 获取可跨协程移交的单用户与全局并发租约
    async def acquire_lease(self, user_id: str) -> ConcurrencyLease:
        user_sem = self._user_sems.get(user_id)
        if user_sem is None:
            user_sem = asyncio.Semaphore(self._max_per_user)
            self._user_sems[user_id] = user_sem
        await user_sem.acquire()
        try:
            await self._global_sem.acquire()
        except BaseException:
            user_sem.release()
            raise
        return ConcurrencyLease(user_sem, self._global_sem)

    # 获取指定用户的并发信号量与全局并发信号量
    @asynccontextmanager
    async def acquire(self, user_id: str) -> AsyncIterator[None]:
        lease = await self.acquire_lease(user_id)
        try:
            yield
        finally:
            lease.release()
