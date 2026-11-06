"""命令行不能隐式调用模型、泄露凭证或修改重放的状态。"""

import gzip
import hashlib
import json
from argparse import Namespace

import pytest
import yaml

from backend.cli import (
    doctor,
    init_secrets,
    owned_process,
    port_available,
    replay,
    safe_error,
    up,
    voice_case,
)
from backend.database import DatabaseConfig, connect
from backend.model_config import load_app_profile, load_model_profiles
from backend.settings import Settings
from tests.db import database_for, snapshot_database
from tests.paths import ROOT


def test_checked_in_model_profiles_match_the_roles_and_budgets():
    profiles = load_model_profiles(ROOT / "backend/config/models.yaml")
    assert profiles.agent.model and profiles.agent.base_url
    for profile in (profiles.agent, profiles.judge, profiles.customer, profiles.profile):
        assert profile.extra_body == {}
        assert profile.reasoning_effort is None
        assert profile.max_tokens_param == "max_tokens"
    assert 0 <= profiles.agent.temperature <= 2
    assert 16 <= profiles.agent.max_tokens <= 2000
    assert 1 <= profiles.agent.max_iterations <= 8
    assert profiles.judge.temperature == 0.0
    assert profiles.judge.max_tokens == 16384
    assert profiles.voice.provider == "dashscope"
    assert profiles.voice.credential_env == "DASHSCOPE_API_KEY"
    assert profiles.voice.endpoint_env == "DASHSCOPE_BASE_URL"
    assert profiles.voice.asr_model and profiles.voice.tts_model and profiles.voice.voice
    assert 15 <= profiles.budgets.session_max_seconds <= 300


def test_model_profile_snapshot_preserves_the_configuration_content_hash():
    path = ROOT / "backend/config/models.yaml"
    content = yaml.safe_load(path.read_text())
    expected = hashlib.sha256(
        json.dumps(content, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    assert load_model_profiles(path).snapshot_hash == expected


def test_judge_generation_configuration_reaches_settings_and_changes_snapshot(tmp_path, monkeypatch):
    # 前序用例或真实 .env 可能把角色环境变量留在进程里；本用例只验证 YAML 默认值，
    # 必须先清理，否则断言会依赖测试执行顺序。
    for role in ("AGENT", "JUDGE", "CUSTOMER", "PROFILE"):
        for field in (
            "API_KEY", "BASE_URL", "MODEL", "TEMPERATURE", "MAX_TOKENS",
            "MAX_TOKENS_PARAM", "REASONING_EFFORT", "EXTRA_BODY",
        ):
            monkeypatch.delenv(f"{role}_{field}", raising=False)
    config = tmp_path / "backend/config"
    config.mkdir(parents=True)
    source = ROOT / "backend/config"
    data = yaml.safe_load((source / "models.yaml").read_text())
    (config / "models.yaml").write_text(yaml.safe_dump(data))
    (config / "app.yaml").write_text((source / "app.yaml").read_text())
    monkeypatch.setenv("GROWTH_PROJECT_ROOT", str(tmp_path))
    before = Settings.from_env()
    assert before.judge_extra_body == {}
    assert before.judge_reasoning_effort is None
    data["judge"].update(extra_body={"thinking": {"type": "enabled"}},
                         reasoning_effort="high", temperature=None)
    (config / "models.yaml").write_text(yaml.safe_dump(data))
    configured = Settings.from_env()
    assert configured.judge_extra_body == {"thinking": {"type": "enabled"}}
    assert configured.judge_reasoning_effort == "high"
    assert configured.judge_temperature is None
    assert configured.model_config_hash != before.model_config_hash
    assert configured.agent_temperature == before.agent_temperature
    assert configured.agent_max_tokens == before.agent_max_tokens


def test_checked_in_app_profile_selects_an_existing_scenario():
    app = load_app_profile(ROOT / "backend/config/app.yaml")
    assert (ROOT / "backend/scenarios" / app.default_scenario / "scenario.yaml").is_file()


def test_offline_doctor_does_not_construct_provider_client(tmp_path, monkeypatch, capsys):
    import openai

    def forbidden(*args, **kwargs):
        raise AssertionError("offline doctor must not instantiate a provider client")

    monkeypatch.setattr(openai, "OpenAI", forbidden)
    secret = "test-value-that-must-never-be-printed"
    settings = Settings(
        project_root=ROOT,
        runtime_dir=tmp_path,
        app_db=DatabaseConfig.from_env(),
        agent_api_key=secret,
        dashscope_api_key=secret,
        session_secret=secret,
    )
    assert doctor(Namespace(init_secrets=False, online=False), settings) == 0
    output = capsys.readouterr().out
    assert secret not in output
    assert json.loads(output)["online_probe"] == "not_requested"


def test_secret_initialization_is_idempotent_and_private(tmp_path):
    from dotenv import dotenv_values

    settings = Settings(tmp_path, tmp_path / ".run", DatabaseConfig.from_env())
    assert init_secrets(settings) == ["SESSION_SECRET"]
    first = dotenv_values(tmp_path / ".env")
    assert set(first) == {"SESSION_SECRET"}
    assert init_secrets(settings) == []
    assert dotenv_values(tmp_path / ".env") == first
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600


def test_replay_uses_saved_data_and_never_executes_tools(tmp_path, capsys):
    from backend.agent_harness import BackendService, load_scenario

    scenario = load_scenario(ROOT / "backend/scenarios/didi_driver/scenario.yaml")
    db_config = database_for(tmp_path / "application")
    service = BackendService(db_config, scenario)
    ctx = service.create_session("synthetic-replay", run_id="run-read-only")
    service.add_message(ctx, "我只是口头说完成了。")
    service.end_session(ctx)
    service.close()
    from backend.agent_harness.runtime.store import RuntimeStore

    runtime = RuntimeStore(db_config)
    runtime.create(ctx.session_id, scenario, "text")
    runtime.emit(
        ctx.session_id, "framework", {"type": "TOOL_RESULT_START", "tool_call_name": "Skill"}
    )
    settings = Settings(ROOT, tmp_path, db_config)
    before = snapshot_database(db_config)
    assert replay(Namespace(run="run-read-only"), settings) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "read_only"
    assert len(result["sessions"]) == 1
    assert result["sessions"][0]["messages"][0]["text"] == "我只是口头说完成了。"
    assert result["sessions"][0]["runtime_trace"][0]["data"]["tool_call_name"] == "Skill"
    assert snapshot_database(db_config) == before


@pytest.mark.parametrize("prefix, reference", [
    ("evaluation/reports", "test-report"),
    ("docs/reports", "test-report"),
    ("evaluation/reports/agent", "test-report"),
    ("evaluation/reports/agent", "agent/test-report"),
])
def test_evaluation_replay_needs_no_live_db_and_rejects_traversal(tmp_path, capsys, prefix, reference):
    folder = tmp_path / prefix / "test-report"
    folder.mkdir(parents=True)
    artifact = folder / "traces.json"
    artifact.write_text('[{"status":"frozen"}]')
    settings = Settings(tmp_path, tmp_path / ".run", DatabaseConfig.from_env())
    assert replay(Namespace(run=reference), settings) == 0
    assert json.loads(capsys.readouterr().out)["data"] == [{"status": "frozen"}]
    assert artifact.read_text() == '[{"status":"frozen"}]'
    with pytest.raises(ValueError):
        replay(Namespace(run="../test-report"), settings)


def test_compressed_evaluation_replay_needs_no_database_or_tool_execution(
    tmp_path, monkeypatch, capsys
):
    import backend.agent_harness
    import backend.database

    def forbidden(*_args, **_kwargs):
        raise AssertionError("回放压缩记录不能连接数据库或创建工具服务")

    monkeypatch.setattr(backend.database, "connect", forbidden)
    monkeypatch.setattr(backend.agent_harness, "BackendService", forbidden)
    folder = tmp_path / "evaluation/reports/agent/test-compressed"
    folder.mkdir(parents=True)
    traces = [{"status": "frozen", "tool_events": [
        {"type": "tool_start", "data": {"name": "send_sms", "arguments": {}}},
    ]}]
    artifact = folder / "traces.json.gz"
    original = gzip.compress(json.dumps(traces, ensure_ascii=False).encode("utf-8"))
    artifact.write_bytes(original)
    settings = Settings(tmp_path, tmp_path / ".run", DatabaseConfig.from_env())

    assert replay(Namespace(run="agent/test-compressed"), settings) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "read_only"
    assert result["source"] == "traces.json.gz"
    assert result["data"] == traces
    assert artifact.read_bytes() == original
    assert not (folder / "traces.json").exists()


def test_cli_errors_never_print_provider_payload_or_secret(monkeypatch):
    secret = "synthetic-api-secret-for-error-test"
    monkeypatch.setenv("AGENT_API_KEY", secret)
    assert secret not in safe_error(ValueError(f"bad endpoint with {secret}"))
    assert "Authorization" not in safe_error(RuntimeError(f"Authorization: Bearer {secret}"))


def test_effective_model_environment_overrides_are_part_of_snapshot(monkeypatch):
    monkeypatch.setenv("GROWTH_PROJECT_ROOT", str(ROOT))
    monkeypatch.setenv("AGENT_MODEL", "text-version-one")
    first = Settings.from_env().model_config_hash
    monkeypatch.setenv("AGENT_MODEL", "text-version-two")
    assert Settings.from_env().model_config_hash != first
    second = Settings.from_env().model_config_hash
    monkeypatch.setenv("JUDGE_MODEL", "judge-version-one")
    configured = Settings.from_env()
    assert configured.judge_model == "judge-version-one"
    assert configured.model_config_hash != second
    second = configured.model_config_hash
    monkeypatch.setenv("AGENT_API_KEY", "rotated-synthetic-secret")
    assert Settings.from_env().model_config_hash == second


def test_human_voice_case_only_produces_a_conversation_url(tmp_path, monkeypatch, capsys):
    from urllib.parse import parse_qs, urlparse

    import httpx

    from evaluation.checks.voice_review import CASE_SCENARIOS, REVIEW_VERSION

    def forbidden(*args, **kwargs):
        raise AssertionError("voice-case must not call any external service")

    monkeypatch.setattr(httpx, "post", forbidden)
    settings = Settings(ROOT, tmp_path, DatabaseConfig.from_env())
    for case, scenario in CASE_SCENARIOS.items():
        assert voice_case(Namespace(case=case, customer="synthetic-human-01"), settings) == 0
        output = json.loads(capsys.readouterr().out)
        assert output["model_requests"] == 0
        assert output["mode"] == "conversation_only"
        assert output["review_version"] == REVIEW_VERSION
        assert "failure_plan" not in output
        query = parse_qs(urlparse(output["url"]).query)
        assert query["voice_case"] == [case]
        assert query["scenario"] == [scenario]
        assert query["review_version"] == [REVIEW_VERSION]


def test_voice_case_continuation_uses_explicit_same_customer_and_distinct_runs(tmp_path, capsys):
    from evaluation.checks.voice_review import REVIEW_VERSION

    settings = Settings(ROOT, tmp_path, DatabaseConfig.from_env())
    voice_case(Namespace(case=f"{REVIEW_VERSION}-D03", customer=None), settings)
    first = json.loads(capsys.readouterr().out)
    voice_case(Namespace(case=f"{REVIEW_VERSION}-D04", customer=first["customer_id"]), settings)
    second = json.loads(capsys.readouterr().out)
    assert first["customer_id"] == second["customer_id"]
    assert first["run_id"] != second["run_id"]
    assert "--customer " + first["customer_id"] in first["next"]
    assert "D03/D04" in second["next"]
    with pytest.raises(ValueError, match="当前语音案例"):
        voice_case(Namespace(case="D03", customer=first["customer_id"]), settings)


def test_port_preflight_allows_restart_after_closed_connection_but_not_live_listener():
    import socket

    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.listen()
        assert not port_available(port)
        with socket.create_connection(("127.0.0.1", port)) as client:
            server, _ = listener.accept()
            server.close()
            assert client.recv(1) == b""
    assert port_available(port)


def test_secret_initialization_preserves_unrelated_configuration(tmp_path):
    from dotenv import dotenv_values

    settings = Settings(
        tmp_path,
        tmp_path / ".run",
        DatabaseConfig.from_env(),
        agent_api_key="text",
        dashscope_api_key="voice",
        session_secret="session",
    )
    assert settings.missing() == []
    (tmp_path / ".env").write_text("OTHER_SERVICE_TOKEN='preserved-service-value'\n")
    assert init_secrets(settings) == ["SESSION_SECRET"]
    assert dotenv_values(tmp_path / ".env")["OTHER_SERVICE_TOKEN"] == "preserved-service-value"


def test_up_starts_only_the_application_and_checks_its_port(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    import httpx

    import backend.agent_harness.scenario
    from backend import cli

    entry = tmp_path / "backend/scenarios/example/scenario.yaml"
    entry.parent.mkdir(parents=True)
    entry.write_text("{}")
    front = tmp_path / "frontend/dist/index.html"
    front.parent.mkdir(parents=True)
    front.write_text("ready")
    monkeypatch.setattr(
        backend.agent_harness.scenario,
        "load_scenario",
        lambda *args: SimpleNamespace(id="example", data={"scenario": {"title": "Example"}}),
    )
    launched, checked, prepared, environments = [], [], [], []
    monkeypatch.setattr(cli, "port_available", lambda port: checked.append(port) or True)
    monkeypatch.setattr(cli, "prepare_database", lambda settings: None)
    monkeypatch.setattr(cli, "prepare_knowledge_once", lambda settings: prepared.append(settings))
    monkeypatch.setattr(cli, "process_field", lambda *args: "synthetic-birth")

    def launch(command, **kwargs):
        launched.append(command)
        environments.append(kwargs["env"])
        return SimpleNamespace(pid=12345, poll=lambda: None)

    monkeypatch.setattr(cli.subprocess, "Popen", launch)
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: SimpleNamespace(status_code=200))
    settings = Settings(
        tmp_path,
        tmp_path / ".run",
        DatabaseConfig.from_env(),
        agent_api_key="text",
        dashscope_api_key="voice",
        session_secret="session",
    )
    assert up(Namespace(scenario=None, build=False), settings) == 0
    assert prepared == [settings]
    assert checked == [8787]
    assert len(launched) == 1
    from pathlib import Path

    from backend.paths import resolve_project_path

    environment = environments[0]
    assert all(not Path(environment[key]).is_absolute() for key in (
        "GROWTH_PROJECT_ROOT", "GROWTH_RUNTIME_DIR", "GROWTH_SCENARIO",
    ))
    child_root = resolve_project_path(environment["GROWTH_PROJECT_ROOT"])
    assert child_root == settings.project_root
    assert resolve_project_path(environment["GROWTH_RUNTIME_DIR"], child_root) == settings.runtime_dir
    assert resolve_project_path(environment["GROWTH_SCENARIO"], child_root) == entry
    assert launched[0][3] == "backend.api.app:create_app"
    assert launched[0][-2:] == ["--log-config", "backend/config/logging.json"]
    state_text = (settings.runtime_dir / "services.json").read_text()
    state = json.loads(state_text)
    assert [p["name"] for p in state["processes"]] == ["application"]
    assert state["processes"][0]["project_root"] == "/growth-agent"
    assert str(tmp_path) not in state_text
    assert str(tmp_path) not in capsys.readouterr().out


@pytest.mark.parametrize("logging_options", ["", " --log-config backend/config/logging.json"])
def test_process_ownership_requires_exact_command_and_birth_timestamp(monkeypatch, logging_options):
    from backend import cli

    record = {
        "pid": 123,
        "module": "backend.api.app:create_app",
        "port": 8787,
        "process_started": "original-process",
    }
    command = ("python -m uvicorn backend.api.app:create_app --factory --host 127.0.0.1 "
               "--port 8787 --no-access-log" + logging_options)
    monkeypatch.setattr(
        cli, "process_field", lambda pid, field: command if field == "command" else "new-process"
    )
    assert not owned_process(record)
    monkeypatch.setattr(
        cli,
        "process_field",
        lambda pid, field: command if field == "command" else "original-process",
    )
    assert owned_process(record)
    assert not owned_process({**record, "module": "unrelated.app:create_app"})
    assert not owned_process({**record, "port": 878})
    assert not owned_process({**record, "pid": -1})


def test_down_only_signals_the_owned_application_and_preserves_data(tmp_path, monkeypatch):
    from backend import cli

    settings = Settings(
        project_root=ROOT, runtime_dir=tmp_path, app_db=database_for(tmp_path / "application")
    )
    with connect(settings.app_db) as db:
        db.execute("CREATE TABLE saved_customer_data (id TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO saved_customer_data VALUES ('customer', 'saved customer data')")
    before = snapshot_database(settings.app_db)
    records = [
        {
            "pid": 123,
            "module": "backend.api.app:create_app",
            "port": 8787,
            "process_started": "birth",
        },
        {
            "pid": 456,
            "module": "unrelated.app:create_app",
            "port": 8788,
            "process_started": "birth",
        },
    ]
    (tmp_path / "services.json").write_text(json.dumps({"processes": records}))
    signalled = []
    command = "python -m uvicorn backend.api.app:create_app --factory --host 127.0.0.1 --port 8787 --no-access-log"

    def process_field(pid, field):
        if pid != 123 or signalled:
            return ""
        return command if field == "command" else "birth"

    monkeypatch.setattr(cli, "process_field", process_field)
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: signalled.append((pid, sig)))
    assert cli.down(Namespace(), settings) == 0
    assert [pid for pid, _ in signalled] == [123]
    assert snapshot_database(settings.app_db) == before


def test_validation_outputs_only_project_relative_display_paths(tmp_path, capsys):
    import shutil

    from backend.cli import validate

    shutil.copytree(ROOT / "backend/scenarios", tmp_path / "backend/scenarios")
    shutil.copytree(ROOT / "backend/agent_harness/skills", tmp_path / "backend/agent_harness/skills")
    settings = Settings(tmp_path, tmp_path / ".run", DatabaseConfig.from_env())
    assert validate(Namespace(scenario=None), settings) == 0
    output = capsys.readouterr().out
    assert str(tmp_path) not in output
    result = json.loads(output)
    assert all(item["entry"].startswith("/growth-agent/backend/scenarios/")
               for item in result["scenarios"])


def test_cli_path_errors_hide_the_local_project_root(tmp_path, monkeypatch):
    monkeypatch.setenv("GROWTH_PROJECT_ROOT", str(tmp_path))
    path = tmp_path / "backend/agent_harness/rag/knowledge/v1/catalog.json"
    output = safe_error(FileNotFoundError(f"找不到文件：{path}"))
    assert str(tmp_path) not in output
    assert "/growth-agent/backend/agent_harness/rag/knowledge/v1/catalog.json" in output
