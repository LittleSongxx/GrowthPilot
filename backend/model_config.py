"""按调用用途保存模型配置，与业务场景和密钥值分离，不主动执行调用。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ModelProfile:
    model: str | None
    base_url: str | None
    temperature: float | None
    max_tokens: int
    max_tokens_param: str = "max_tokens"
    reasoning_effort: str | None = None
    extra_body: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AgentProfile(ModelProfile):
    max_iterations: int = 8


@dataclass(frozen=True)
class VoiceProfile:
    provider: str
    asr_model: str
    tts_model: str
    credential_env: str
    endpoint_env: str
    voice: str


@dataclass(frozen=True)
class Budgets:
    session_max_seconds: int


@dataclass(frozen=True)
class ModelProfiles:
    schema_version: str
    agent: AgentProfile
    judge: ModelProfile
    customer: ModelProfile
    profile: ModelProfile
    voice: VoiceProfile
    budgets: Budgets

    @property
    def snapshot_hash(self):
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()


@dataclass(frozen=True)
class AppProfile:
    schema_version: str
    default_scenario: str


def load_app_profile(path: Path) -> AppProfile:
    return AppProfile(**yaml.safe_load(path.read_text()))


def load_model_profiles(path: Path) -> ModelProfiles:
    data = yaml.safe_load(path.read_text())
    return ModelProfiles(
        schema_version=data["schema_version"],
        agent=AgentProfile(**data["agent"]),
        judge=ModelProfile(**data["judge"]),
        customer=ModelProfile(**data["customer"]),
        profile=ModelProfile(**data["profile"]),
        voice=VoiceProfile(**data["voice"]),
        budgets=Budgets(**data["budgets"]),
    )
