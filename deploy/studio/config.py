import asyncio
import json
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

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

    reference_rules: str
    edit_rules: str
    planner_rules: str
    planner_system: str
    render_prefix: str
    edit_prefix: str
    no_reference_rules: str
    materials: list[StoneMaterial]


# 工作台运行设置模型
class StudioSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    shared_token: str
    gemini_api_key: str
    ark_api_key: str
    seedream_model: str = "doubao-seedream-5-0-pro-260628"
    planner_model: str = "gemini-4-argon"
    planner_thinking_level: str = "HIGH"
    max_parallel_per_user: int = Field(default=3, ge=1, le=10)
    global_max_parallel: int = Field(default=12, ge=1, le=64)
    seedream_timeout_seconds: int = Field(default=240, ge=5, le=600)
    planner_timeout_seconds: int = Field(default=540, ge=5, le=600)
    librechat_images_dir: Path = Path("/librechat/images")
    data_dir: Path = Path("/data")
    kb_root: Path = Path("/kb")
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"

    # 校验shared_token必须为64位十六进制字符串
    @field_validator("shared_token")
    @classmethod
    def _validate_shared_token(cls, value: str) -> str:
        cleaned = value.strip()
        if not SHARED_TOKEN_RE.fullmatch(cleaned):
            raise ValueError(
                "shared_token must be an exact 64-character hexadecimal string"
            )
        return cleaned

    # 校验API密钥不包含空白字符且非空
    @field_validator("gemini_api_key", "ark_api_key")
    @classmethod
    def _validate_secret(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or any(ch.isspace() for ch in cleaned):
            raise ValueError(
                "Secret configuration value must be a non-empty string without whitespace"
            )
        return cleaned


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
    policy_cfg = json.loads(
        (base / "studio-creation-policy.json").read_text(encoding="utf-8")
    )
    materials_cfg = json.loads(
        (base / "studio-materials.json").read_text(encoding="utf-8")
    )
    return StudioRules(
        reference_rules=image_cfg["reference_rules"],
        edit_rules=image_cfg["edit_rules"],
        planner_rules=runtime_cfg["planner_rules"],
        planner_system=runtime_cfg["planner_system"],
        render_prefix=runtime_cfg["render_prefix"],
        edit_prefix=runtime_cfg["edit_prefix"],
        no_reference_rules=policy_cfg["no_reference_rules"],
        materials=[StoneMaterial.model_validate(item) for item in materials_cfg],
    )


# 按用户与全局上限控制并发任务的信号量管理器
class ConcurrencyLimiter:
    def __init__(self, max_per_user: int = 3, global_max: int = 12) -> None:
        self._max_per_user = max_per_user
        self._global_max = global_max
        self._global_sem = asyncio.Semaphore(global_max)
        self._user_sems: dict[str, asyncio.Semaphore] = {}

    # 获取指定用户的并发信号量与全局并发信号量
    @asynccontextmanager
    async def acquire(self, user_id: str) -> AsyncIterator[None]:
        user_sem = self._user_sems.get(user_id)
        if user_sem is None:
            user_sem = asyncio.Semaphore(self._max_per_user)
            self._user_sems[user_id] = user_sem
        async with user_sem, self._global_sem:
            yield
