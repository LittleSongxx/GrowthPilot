"""按 Agent、评审、模拟客户和画像提取用途读取模型配置，密钥仅保存在服务端。"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

from backend.database import DatabaseConfig

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRAMEWORK_COMMIT = "e4c1e074dee3f0f6cca28ca29e934c4ec0605354"
MODEL_ROLES = ("agent", "judge", "customer", "profile")


@dataclass(frozen=True)
class ModelConnection:
    """模型调用使用的有效连接；密钥不参与日志展示。"""

    api_key: str = field(repr=False)
    base_url: str
    model: str


def model_connection(settings, role="agent") -> ModelConnection:
    """辅助用途留空时动态沿用 Agent，保留原始覆盖值以支持安全地替换配置。"""
    if role not in MODEL_ROLES:
        raise ValueError("未知模型用途。")
    agent_base_url = getattr(settings, "agent_base_url", "")
    base_url = getattr(settings, f"{role}_base_url", "") or agent_base_url
    api_key = getattr(settings, f"{role}_api_key", "")
    if role != "agent" and base_url.rstrip("/") != agent_base_url.rstrip("/") and not api_key:
        raise ValueError(f"独立的 {role.upper()}_BASE_URL 需要同时配置 {role.upper()}_API_KEY。")
    return ModelConnection(
        api_key=api_key or getattr(settings, "agent_api_key", ""),
        base_url=base_url,
        model=getattr(settings, f"{role}_model", "") or getattr(settings, "agent_model", ""),
    )


def _env(name, default=None):
    """空白示例配置视为未填写；环境变量优先于配置文件。"""
    return os.environ.get(name, "").strip() or default


def _temperature(name, default):
    value = _env(name)
    if value is None:
        return default
    if value.lower() in {"null", "none"}:
        return None
    try:
        return float(value)
    except ValueError:
        raise ValueError(f"{name} 应为数值或 null。") from None


def _extra_body(name, default):
    value = _env(name)
    if value is None:
        return dict(default)
    try:
        result = json.loads(value)
    except json.JSONDecodeError:
        raise ValueError(f"{name} 应为 JSON 对象。") from None
    if not isinstance(result, dict):
        raise ValueError(f"{name} 应为 JSON 对象。")  # noqa: TRY004 配置值无效，统一按值错误处理。
    return result


@dataclass(frozen=True)
class Settings:
    project_root: Path
    runtime_dir: Path
    app_db: DatabaseConfig
    app_port: int = 8787
    agent_api_key: str = field(default="", repr=False)
    agent_base_url: str = "https://api.deepseek.com"
    agent_model: str = "deepseek-flash"
    agent_temperature: float | None = 0.0
    agent_max_tokens: int = 700
    agent_max_tokens_param: str = "max_tokens"
    agent_reasoning_effort: str | None = None
    agent_extra_body: dict = field(default_factory=dict, repr=False)
    agent_max_iterations: int = 8
    judge_api_key: str = field(default="", repr=False)
    judge_base_url: str = ""
    judge_model: str = ""
    judge_temperature: float | None = 0.0
    judge_max_tokens: int = 16384
    judge_max_tokens_param: str = "max_tokens"
    judge_reasoning_effort: str | None = None
    judge_extra_body: dict = field(default_factory=dict, repr=False)
    customer_api_key: str = field(default="", repr=False)
    customer_base_url: str = ""
    customer_model: str = ""
    customer_temperature: float | None = 0.7
    customer_max_tokens: int = 700
    customer_max_tokens_param: str = "max_tokens"
    customer_reasoning_effort: str | None = None
    customer_extra_body: dict = field(default_factory=dict, repr=False)
    profile_api_key: str = field(default="", repr=False)
    profile_base_url: str = ""
    profile_model: str = ""
    profile_temperature: float | None = 0.0
    profile_max_tokens: int = 3000
    profile_max_tokens_param: str = "max_tokens"
    profile_reasoning_effort: str | None = None
    profile_extra_body: dict = field(default_factory=dict, repr=False)
    dashscope_api_key: str = field(default="", repr=False)
    dashscope_base_url: str = "https://dashscope.aliyuncs.com"
    asr_model: str = "qwen3-asr-flash"
    tts_model: str = "qwen3-tts-flash"
    tts_voice: str = "Cherry"
    session_secret: str = field(default="", repr=False)
    session_max_seconds: int = 300
    model_config_hash: str = ""
    default_scenario_id: str = ""

    def __post_init__(self):
        reserved = {"model", "messages", "tools", "tool_choice", "stream", "stream_options",
                    "temperature", "max_tokens", "max_completion_tokens", "reasoning_effort"}
        for role in MODEL_ROLES:
            prefix = role.upper()
            endpoint = urlsplit(model_connection(self, role).base_url)
            if (endpoint.scheme not in {"http", "https"} or not endpoint.netloc
                    or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
                raise ValueError(f"{prefix}_BASE_URL 应为 HTTP(S) API 基础地址，不含凭据、查询参数或片段。")
            token_param = getattr(self, f"{role}_max_tokens_param")
            if token_param not in {"max_tokens", "max_completion_tokens"}:
                raise ValueError(f"{prefix}_MAX_TOKENS_PARAM 只支持 max_tokens 或 max_completion_tokens。")
            temperature = getattr(self, f"{role}_temperature")
            if temperature is not None and not 0 <= temperature <= 2:
                raise ValueError(f"{prefix}_TEMPERATURE 应在 0 到 2 之间，或用 null 省略。")
            if getattr(self, f"{role}_max_tokens") <= 0:
                raise ValueError(f"{prefix}_MAX_TOKENS 必须大于 0。")
            extra = getattr(self, f"{role}_extra_body")
            if not isinstance(extra, dict) or reserved.intersection(extra):
                raise ValueError(f"{prefix}_EXTRA_BODY 应为对象，且不能覆盖模型、消息、工具或标准生成参数。")
        endpoint = urlsplit(self.dashscope_base_url)
        if (endpoint.scheme != "https" or not endpoint.netloc or endpoint.path not in {"", "/"}
                or endpoint.query or endpoint.fragment):
            raise ValueError("DASHSCOPE_BASE_URL 应为 HTTPS 域名根地址，不含 WebSocket/API 路径。")

    @classmethod
    def from_env(cls) -> Settings:
        from backend.paths import resolve_project_path

        root = resolve_project_path(os.environ.get("GROWTH_PROJECT_ROOT", "."), PROJECT_ROOT)
        load_dotenv(root / ".env", override=False)
        runtime = resolve_project_path(os.environ.get("GROWTH_RUNTIME_DIR", ".run"), root)
        from backend.model_config import load_app_profile, load_model_profiles

        profiles = load_model_profiles(root / "backend/config/models.yaml")
        app_profile = load_app_profile(root / "backend/config/app.yaml")
        options = {}
        credentials = {}
        for role in MODEL_ROLES:
            profile = getattr(profiles, role)
            prefix = role.upper()
            credentials[f"{role}_api_key"] = _env(f"{prefix}_API_KEY", "")
            options.update({
                f"{role}_base_url": _env(f"{prefix}_BASE_URL", profile.base_url or ""),
                f"{role}_model": _env(f"{prefix}_MODEL", profile.model or ""),
                f"{role}_temperature": _temperature(f"{prefix}_TEMPERATURE", profile.temperature),
                f"{role}_max_tokens": int(_env(f"{prefix}_MAX_TOKENS", profile.max_tokens)),
                f"{role}_max_tokens_param": _env(f"{prefix}_MAX_TOKENS_PARAM", profile.max_tokens_param),
                f"{role}_reasoning_effort": _env(f"{prefix}_REASONING_EFFORT", profile.reasoning_effort),
                f"{role}_extra_body": _extra_body(f"{prefix}_EXTRA_BODY", profile.extra_body),
            })
        options.update(
            agent_max_iterations=profiles.agent.max_iterations,
            asr_model=_env("ASR_MODEL", profiles.voice.asr_model),
            tts_model=_env("TTS_MODEL", profiles.voice.tts_model),
            tts_voice=_env("TTS_VOICE", profiles.voice.voice),
            dashscope_base_url=_env(profiles.voice.endpoint_env, "https://dashscope.aliyuncs.com"),
        )
        model_hash = hashlib.sha256(
            json.dumps({"profile_hash": profiles.snapshot_hash, **options}, sort_keys=True).encode()
        ).hexdigest()
        return cls(
            project_root=root,
            runtime_dir=runtime,
            app_db=DatabaseConfig.from_env(),
            app_port=int(os.environ.get("GROWTH_APP_PORT", "8787")),
            dashscope_api_key=_env(profiles.voice.credential_env, ""),
            session_secret=os.environ.get("SESSION_SECRET", ""),
            session_max_seconds=profiles.budgets.session_max_seconds,
            model_config_hash=model_hash,
            default_scenario_id=app_profile.default_scenario,
            **credentials,
            **options,
        )

    def missing(self) -> list[str]:
        fields = {
            "AGENT_API_KEY": self.agent_api_key,
            "DASHSCOPE_API_KEY": self.dashscope_api_key,
            "SESSION_SECRET": self.session_secret,
        }
        return [name for name, value in fields.items() if not value]
