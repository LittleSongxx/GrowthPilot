"""连接继承在使用时解析，替换配置不能保留旧模型或将共享密钥带到新服务。"""

from dataclasses import replace

import pytest

from backend.database import DatabaseConfig
from backend.settings import Settings, model_connection


@pytest.fixture
def settings(tmp_path):
    """使用固定的本地测试值，不加载环境变量或建立数据库连接。"""
    return Settings(
        tmp_path, tmp_path / "runtime", DatabaseConfig("postgresql://localhost/offline_test"),
        agent_api_key="synthetic-agent-key", agent_base_url="https://agent.example/v1",
        agent_model="agent-model",
    )


@pytest.mark.parametrize("role", ["judge", "customer", "profile"])
def test_inherited_connections_follow_agent_changes_without_overwriting_raw_fields(settings, role):
    for suffix in ("api_key", "base_url", "model"):
        assert getattr(settings, f"{role}_{suffix}") == ""
    assert model_connection(settings, role) == model_connection(settings)

    updated = replace(
        settings, agent_api_key="synthetic-new-key", agent_base_url="https://new.example/v1",
        agent_model="new-model",
    )
    assert model_connection(updated, role) == model_connection(updated)
    assert model_connection(updated, role).model == "new-model"
    assert model_connection(settings, role).model == "agent-model"


@pytest.mark.parametrize("role", ["judge", "customer", "profile"])
def test_replacing_an_inherited_endpoint_requires_an_explicit_role_key(settings, role):
    with pytest.raises(ValueError, match=f"{role.upper()}_API_KEY"):
        replace(settings, **{f"{role}_base_url": "https://independent.example/v1"})

    updated = replace(settings, **{
        f"{role}_base_url": "https://independent.example/v1",
        f"{role}_api_key": "synthetic-role-key",
    })
    connection = model_connection(updated, role)
    assert connection.base_url == "https://independent.example/v1"
    assert connection.api_key == "synthetic-role-key"
    assert connection.model == settings.agent_model


@pytest.mark.parametrize("role", ["judge", "customer", "profile"])
def test_explicit_role_connections_survive_agent_changes(settings, role):
    configured = replace(settings, **{
        f"{role}_base_url": "https://independent.example/v1",
        f"{role}_api_key": "synthetic-role-key",
        f"{role}_model": "independent-model",
    })
    updated = replace(
        configured, agent_api_key="synthetic-new-key", agent_base_url="https://new.example/v1",
        agent_model="new-model",
    )
    assert model_connection(updated, role) == model_connection(configured, role)
    assert "synthetic-role-key" not in repr(model_connection(updated, role))


def test_explicit_model_override_still_inherits_the_latest_agent_connection(settings):
    updated = replace(settings, judge_model="judge-model", agent_api_key="synthetic-new-key")
    connection = model_connection(updated, "judge")
    assert connection.model == "judge-model"
    assert connection.api_key == "synthetic-new-key"
    assert connection.base_url == settings.agent_base_url


def test_unknown_model_role_is_rejected(settings):
    with pytest.raises(ValueError, match="未知模型用途"):
        model_connection(settings, "unknown")
