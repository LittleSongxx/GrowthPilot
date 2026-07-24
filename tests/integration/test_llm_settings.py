"""隔离宿主环境，验证按用途配置模型、连接继承和请求参数，不访问外部服务。"""

import json
import os
from types import SimpleNamespace

import pytest
import yaml

import backend.settings as settings_module
from backend.llm import completion_parameters
from backend.settings import Settings, model_connection
from tests.paths import ROOT

ROLES = ("agent", "judge", "customer", "profile")


@pytest.fixture(autouse=True)
def isolated_model_config(tmp_path, monkeypatch):
    """只拷贝公开配置，并阻止加载开发者的 .env 或继承真实模型凭据。"""
    for name in list(os.environ):
        if name.startswith((
            "AGENT_", "JUDGE_", "CUSTOMER_", "PROFILE_", "LLM_", "OPENAI_", "DEEPSEEK_",
            "DASHSCOPE_",
        )) or name in {
            "GROWTH_PROJECT_ROOT", "GROWTH_RUNTIME_DIR", "GROWTH_APP_PORT",
            "DATABASE_URL", "DATABASE_SCHEMA", "SESSION_SECRET", "ASR_MODEL", "TTS_MODEL",
            "TTS_VOICE",
        }:
            monkeypatch.delenv(name)
    directory = tmp_path / "backend/config"
    directory.mkdir(parents=True)
    for name in ("models.yaml", "app.yaml"):
        (directory / name).write_text((ROOT / "backend/config" / name).read_text())
    monkeypatch.setenv("GROWTH_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENT_API_KEY", "agent-test-key")
    monkeypatch.setenv("AGENT_BASE_URL", "https://agent.example/v1")
    monkeypatch.setenv("AGENT_MODEL", "agent-test-model")

    def skip_dotenv(path, *, override):
        assert path == tmp_path / ".env"
        assert override is False
        return False

    monkeypatch.setattr(settings_module, "load_dotenv", skip_dotenv)
    return directory / "models.yaml"


def update_profile(path, role, **changes):
    """修改临时配置，模拟学员通过 YAML 选择模型和参数。"""
    data = yaml.safe_load(path.read_text())
    data[role].update(changes)
    path.write_text(yaml.safe_dump(data))


def test_default_generation_parameters_do_not_infer_vendor_features():
    settings = Settings.from_env()
    for role in ROLES:
        request = completion_parameters(settings, role=role)
        assert "extra_body" not in request
        assert "reasoning_effort" not in request
        assert "thinking" not in request
        assert getattr(settings, f"{role}_extra_body") == {}
        assert getattr(settings, f"{role}_reasoning_effort") is None


@pytest.mark.parametrize("prefix", ["DEEPSEEK", "OPENAI", "LLM"])
def test_retired_environment_variables_do_not_configure_models(monkeypatch, prefix):
    monkeypatch.delenv("AGENT_API_KEY")
    baseline = Settings.from_env()
    monkeypatch.setenv(f"{prefix}_API_KEY", "retired-test-key")
    monkeypatch.setenv(f"{prefix}_BASE_URL", "https://retired.example/v1")
    monkeypatch.setenv(f"{prefix}_MODEL", "retired-model")
    monkeypatch.setenv(f"{prefix}_PROVIDER", "deepseek")
    monkeypatch.setenv(f"{prefix}_THINKING", "true")
    monkeypatch.setenv(f"{prefix}_EXTRA_BODY", '{"thinking": {"type": "enabled"}}')
    configured = Settings.from_env()

    assert configured.agent_api_key == baseline.agent_api_key == ""
    assert configured.agent_base_url == baseline.agent_base_url
    assert configured.agent_model == baseline.agent_model
    assert configured.agent_extra_body == baseline.agent_extra_body == {}
    assert configured.model_config_hash == baseline.model_config_hash
    assert "AGENT_API_KEY" in configured.missing()


@pytest.mark.parametrize("role", ROLES)
def test_each_role_accepts_independent_connection_and_generation_options(monkeypatch, role):
    prefix = role.upper()
    values = {
        "API_KEY": f"{role}-independent-key",
        "BASE_URL": f"https://{role}-independent.example/v1",
        "MODEL": f"{role}-independent-model",
        "TEMPERATURE": "0.6",
        "MAX_TOKENS": "1800",
        "MAX_TOKENS_PARAM": "max_completion_tokens",
        "REASONING_EFFORT": "low",
        "EXTRA_BODY": '{"enable_thinking": true}',
    }
    for suffix, value in values.items():
        monkeypatch.setenv(f"{prefix}_{suffix}", value)
    settings = Settings.from_env()
    for suffix in ("API_KEY", "BASE_URL", "MODEL"):
        assert getattr(settings, f"{role}_{suffix.lower()}") == values[suffix]
    request = completion_parameters(settings, role=role)
    assert request["max_completion_tokens"] == 1800
    assert "max_tokens" not in request
    assert request["temperature"] == 0.6
    assert request["reasoning_effort"] == "low"
    assert request["extra_body"] == {"enable_thinking": True}


@pytest.mark.parametrize("role", ROLES)
def test_yaml_connection_and_generation_options_apply_before_environment_overrides(
    isolated_model_config, monkeypatch, role,
):
    prefix = role.upper()
    monkeypatch.delenv(f"{prefix}_MODEL", raising=False)
    monkeypatch.delenv(f"{prefix}_BASE_URL", raising=False)
    monkeypatch.setenv(f"{prefix}_API_KEY", f"{role}-yaml-test-key")
    update_profile(
        isolated_model_config, role, model="yaml-model", base_url="https://yaml.example/v1",
        temperature=0.3, max_tokens=1234, max_tokens_param="max_completion_tokens",
        reasoning_effort="low", extra_body={"enable_thinking": False},
    )
    settings = Settings.from_env()
    assert getattr(settings, f"{role}_model") == "yaml-model"
    assert getattr(settings, f"{role}_base_url") == "https://yaml.example/v1"
    assert completion_parameters(settings, role=role) == {
        "temperature": 0.3, "max_completion_tokens": 1234, "reasoning_effort": "low",
        "extra_body": {"enable_thinking": False},
    }
    monkeypatch.setenv(f"{prefix}_MODEL", "environment-model")
    monkeypatch.setenv(f"{prefix}_BASE_URL", "https://environment.example/v1")
    monkeypatch.setenv(f"{prefix}_TEMPERATURE", "null")
    settings = Settings.from_env()
    assert getattr(settings, f"{role}_model") == "environment-model"
    assert getattr(settings, f"{role}_base_url") == "https://environment.example/v1"
    assert "temperature" not in completion_parameters(settings, role=role)


@pytest.mark.parametrize("role", ROLES[1:])
def test_auxiliary_roles_inherit_agent_connection_and_model(role):
    settings = Settings.from_env()
    connection = model_connection(settings, role=role)
    agent = model_connection(settings)
    for suffix in ("api_key", "base_url", "model"):
        assert getattr(connection, suffix) == getattr(agent, suffix)


@pytest.mark.parametrize("role", ROLES[1:])
def test_independent_endpoint_requires_an_explicit_role_key(monkeypatch, role):
    monkeypatch.setenv(f"{role.upper()}_BASE_URL", f"https://{role}-private.example/v1")
    with pytest.raises(ValueError, match=f"{role.upper()}_API_KEY"):
        Settings.from_env()


def test_agent_generation_parameters_do_not_change_auxiliary_role_parameters(monkeypatch):
    baseline = Settings.from_env()
    monkeypatch.setenv("AGENT_TEMPERATURE", "0.8")
    monkeypatch.setenv("AGENT_MAX_TOKENS", "1100")
    monkeypatch.setenv("AGENT_MAX_TOKENS_PARAM", "max_completion_tokens")
    monkeypatch.setenv("AGENT_EXTRA_BODY", '{"enable_thinking": true}')
    monkeypatch.setenv("AGENT_REASONING_EFFORT", "high")
    configured = Settings.from_env()
    for role in ROLES[1:]:
        assert completion_parameters(configured, role=role) == completion_parameters(baseline, role=role)


@pytest.mark.parametrize("independent", [False, True])
async def test_judge_request_uses_resolved_connection_and_model(monkeypatch, independent):
    import openai

    from evaluation.judges.review import review_trace
    from evaluation.judges.schema import JUDGE_RESPONSE_DIMENSIONS

    if independent:
        monkeypatch.setenv("JUDGE_MODEL", "judge-test-model")
        monkeypatch.setenv("JUDGE_API_KEY", "judge-test-key")
        monkeypatch.setenv("JUDGE_BASE_URL", "https://judge.example/v1")
    settings = Settings.from_env()
    calls = {}

    class Client:
        def __init__(self, **kwargs):
            calls["connection"] = kwargs
            self.chat = SimpleNamespace(completions=self)

        async def create(self, **kwargs):
            calls["request"] = kwargs
            content = {key: {"score": 4, "reason": "测试评分"} for key in JUDGE_RESPONSE_DIMENSIONS}
            return SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(content=json.dumps(content)),
            )])

        async def close(self):
            calls["closed"] = True

    monkeypatch.setattr(openai, "AsyncOpenAI", Client)
    trace = {"turns": [{"turn": 1, "origin": "customer", "user_message": "谢谢",
                        "reply": "不客气", "tool_events": []}]}
    case = {"goal": {"description": "解答问题", "success_conditions": ["回答清楚"]}}
    await review_trace(settings, trace, case)

    prefix = "judge" if independent else "agent"
    assert calls["connection"]["api_key"] == f"{prefix}-test-key"
    assert calls["connection"]["base_url"] == f"https://{prefix}.example/v1"
    assert calls["request"]["model"] == model_connection(settings, role="judge").model
    assert calls["request"]["model"] == f"{prefix}-test-model"
    assert calls["closed"] is True
    assert "extra_body" not in calls["request"]
    assert "reasoning_effort" not in calls["request"]


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("value", ["null", "None"])
def test_null_temperature_omits_it_from_all_role_requests(monkeypatch, role, value):
    monkeypatch.setenv(f"{role.upper()}_TEMPERATURE", value)
    settings = Settings.from_env()
    assert "temperature" not in completion_parameters(settings, role=role)


@pytest.mark.parametrize("role", ROLES)
def test_token_parameter_can_be_selected_for_each_role(monkeypatch, role):
    monkeypatch.setenv(f"{role.upper()}_MAX_TOKENS_PARAM", "max_completion_tokens")
    monkeypatch.setenv(f"{role.upper()}_MAX_TOKENS", "1900")
    request = completion_parameters(Settings.from_env(), role=role)
    assert request["max_completion_tokens"] == 1900
    assert "max_tokens" not in request


@pytest.mark.parametrize("role", ROLES)
def test_unknown_token_parameter_is_rejected(monkeypatch, role):
    variable = f"{role.upper()}_MAX_TOKENS_PARAM"
    monkeypatch.setenv(variable, "max_output_tokens")
    with pytest.raises(ValueError, match=variable):
        Settings.from_env()


def test_extra_body_preserves_vendor_parameters_without_sharing_mutable_data(monkeypatch):
    monkeypatch.setenv(
        "AGENT_EXTRA_BODY", '{"enable_thinking": false, "vendor": {"mode": "fast"}}',
    )
    monkeypatch.setenv("JUDGE_EXTRA_BODY", '{"thinking": {"type": "enabled"}}')
    settings = Settings.from_env()
    request = completion_parameters(settings)
    assert request["extra_body"]["enable_thinking"] is False
    assert completion_parameters(settings, role="judge")["extra_body"] == {
        "thinking": {"type": "enabled"},
    }
    request["extra_body"]["vendor"]["mode"] = "changed"
    assert completion_parameters(settings)["extra_body"]["vendor"]["mode"] == "fast"


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("value", ["{broken", "[]", "null", "3", '{"messages": []}'])
def test_extra_body_rejects_invalid_json_shapes_and_reserved_fields(monkeypatch, role, value):
    variable = f"{role.upper()}_EXTRA_BODY"
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError, match=variable):
        Settings.from_env()


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("suffix,value", [
    ("BASE_URL", "https://changed.example/v1"),
    ("MODEL", "changed-model"),
    ("TEMPERATURE", "null"),
    ("MAX_TOKENS_PARAM", "max_completion_tokens"),
    ("MAX_TOKENS", "1700"),
    ("REASONING_EFFORT", "medium"),
    ("EXTRA_BODY", '{"enable_thinking": false}'),
])
def test_hash_changes_with_each_roles_endpoint_and_parameters(monkeypatch, role, suffix, value):
    for purpose in ROLES[1:]:
        monkeypatch.setenv(f"{purpose.upper()}_API_KEY", f"{purpose}-test-key")
    baseline = Settings.from_env().model_config_hash
    monkeypatch.setenv(f"{role.upper()}_{suffix}", value)
    assert Settings.from_env().model_config_hash != baseline


def test_secret_rotation_does_not_change_hash_or_expose_secrets_in_repr(monkeypatch):
    baseline = Settings.from_env().model_config_hash
    variables = [f"{role.upper()}_API_KEY" for role in ROLES]
    variables.extend(("DASHSCOPE_API_KEY", "SESSION_SECRET"))
    for variable in variables:
        secret = f"synthetic-secret-for-{variable}"
        monkeypatch.setenv(variable, secret)
        settings = Settings.from_env()
        assert settings.model_config_hash == baseline
        assert secret not in repr(settings)


@pytest.mark.parametrize("configured", [None, "cache/agent-run"])
def test_runtime_directory_is_resolved_from_project_root_not_current_directory(
    isolated_model_config, monkeypatch, tmp_path, configured,
):
    project_root = isolated_model_config.parents[2]
    unrelated = tmp_path / "unrelated-working-directory"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    if configured is not None:
        monkeypatch.setenv("GROWTH_RUNTIME_DIR", configured)
    settings = Settings.from_env()
    assert settings.project_root == project_root
    assert settings.runtime_dir == project_root / (configured or ".run")


def test_absolute_runtime_directory_remains_an_explicit_override(monkeypatch, tmp_path):
    directory = tmp_path / "separate-agent-run"
    monkeypatch.setenv("GROWTH_RUNTIME_DIR", str(directory))
    assert Settings.from_env().runtime_dir == directory
