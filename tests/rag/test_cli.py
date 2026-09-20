"""启动和默认重建只使用固定教学语料，显式刷新才允许采集网页。"""

from argparse import Namespace
from copy import deepcopy

import pytest

from backend.agent_harness.rag.corpus import CORPUS_RELATIVE_PATH
from backend.agent_harness.rag.corpus import load_corpus as load_fixed_corpus
from backend.cli import knowledge, prepare_knowledge_once
from backend.database import DatabaseConfig
from backend.settings import Settings

SCENARIOS = ("meituan_rider", "meituan_merchant", "didi_driver", "financial_consultation")


def args(action, **values):
    return Namespace(
        **({"action": action, "scenario": None, "query": None,
            "from_cache": False, "refresh": False} | values)
    )


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from backend import cli
    from backend.agent_harness.rag import corpus, index, ingest

    for scenario in SCENARIOS:
        entry = tmp_path / "backend/scenarios" / scenario / "scenario.yaml"
        entry.parent.mkdir(parents=True)
        entry.touch()
    settings = Settings(tmp_path, tmp_path / ".run", DatabaseConfig.from_env())
    chunks = [{"id": scenario, "scenario_id": scenario, "text": "固定教学资料"}
              for scenario in SCENARIOS]
    calls = []

    class Index:
        def __init__(self, directory, *, database):
            assert directory == settings.runtime_dir / "knowledge"
            assert database == settings.app_db

        def status(self):
            return {"status": "no_data"}

        def build(self, members, scenario_id=None):
            calls.append(("build", members, scenario_id))
            return {"status": "ready"}

        def search(self, query, scenario_id):
            calls.append(("search", query, scenario_id))
            return {"status": "success"}

    def collect(*values):
        calls.append(("collect", values[-1]))
        return {"failed": 1}

    def load_corpus(directory, scenario_id=None):
        assert directory == settings.project_root / CORPUS_RELATIVE_PATH
        return [chunk for chunk in chunks
                if scenario_id is None or chunk["scenario_id"] == scenario_id]

    def prepare_database(received_settings):
        assert received_settings is settings
        calls.append(("database",))

    monkeypatch.setattr(index, "PolicyIndex", Index)
    monkeypatch.setattr(ingest, "collect", collect)
    monkeypatch.setattr(ingest, "load_chunks", lambda *_: [])
    monkeypatch.setattr(corpus, "load_corpus", load_corpus)
    monkeypatch.setattr(corpus, "corpus_status", lambda *_: {"version": "teaching-v1"})
    monkeypatch.setattr(cli, "prepare_database", prepare_database)
    return settings, calls, chunks


@pytest.mark.parametrize("scenario", [None, "meituan_rider"])
def test_default_build_uses_fixed_corpus_without_collecting(setup, scenario, capsys):
    settings, calls, chunks = setup
    assert knowledge(args("build", scenario=scenario), settings) == 0
    expected = [chunk for chunk in chunks
                if scenario is None or chunk["scenario_id"] == scenario]
    assert calls == [("database",), ("build", expected, scenario)]
    output = capsys.readouterr().out
    assert '"version": "teaching-v1"' in output
    assert str(settings.project_root) not in output


def test_partial_refresh_replaces_scope_and_reports_failure(setup):
    settings, calls, _ = setup
    assert knowledge(args("build", scenario="meituan_rider", refresh=True), settings) == 1
    assert calls == [("collect", "meituan_rider"), ("database",),
                     ("build", [], "meituan_rider")]


def test_status_does_not_collect_and_full_cache_rebuild_is_one_transaction(setup):
    settings, calls, _ = setup
    assert knowledge(args("status"), settings) == 0
    assert calls == []
    directory = settings.runtime_dir / "knowledge"
    directory.mkdir(parents=True)
    (directory / "catalog.json").write_text("{}")
    assert knowledge(args("build", from_cache=True), settings) == 0
    assert calls == [("database",), ("build", [], None)]


def test_missing_cache_and_unknown_scenario_fail_before_network_or_database(setup):
    settings, calls, _ = setup
    with pytest.raises(ValueError, match="没有在线采集快照"):
        knowledge(args("build", from_cache=True), settings)
    with pytest.raises(ValueError, match="未知知识场景"):
        knowledge(args("build", scenario="../another"), settings)
    assert calls == []


def test_damaged_corpus_fails_before_database_write_or_network(setup, monkeypatch):
    from backend.agent_harness.rag import corpus

    settings, calls, _ = setup
    directory = settings.project_root / CORPUS_RELATIVE_PATH
    directory.mkdir(parents=True)
    (directory / "catalog.sha256").write_text("0" * 64)
    (directory / "catalog.json").write_text("{}")

    # 恢复真实校验入口，确认损坏语料不能进入建表或重建索引步骤。
    monkeypatch.setattr(corpus, "load_corpus", load_fixed_corpus)
    with pytest.raises(ValueError, match="SHA-256 不一致"):
        knowledge(args("build"), settings)
    assert calls == []


def test_search_requires_explicit_scope_and_query(setup):
    settings, calls, _ = setup
    with pytest.raises(ValueError, match="需要"):
        knowledge(args("search", query="报名收费吗"), settings)
    knowledge(args("search", scenario="meituan_rider", query="报名收费吗"), settings)
    assert calls == [("search", "报名收费吗", "meituan_rider")]


@pytest.fixture
def initial_knowledge(tmp_path, monkeypatch):
    from backend.agent_harness.rag import corpus, index, ingest

    settings = Settings(tmp_path, tmp_path / ".run", DatabaseConfig.from_env())
    chunks = [{"id": scenario, "scenario_id": scenario, "text": "固定教学资料"}
              for scenario in SCENARIOS]
    complete = {"status": "ready", "scenarios": [
        {"scenario_id": scenario, **version}
        for scenario, version in index.corpus_versions(chunks).items()
    ]}
    state = {"status": {"status": "no_data", "scenarios": []},
             "built_status": deepcopy(complete)}
    calls = []

    class Index:
        def __init__(self, directory, *, database):
            assert directory == settings.runtime_dir / "knowledge"
            assert database == settings.app_db

        def status(self):
            calls.append("status")
            return state["status"]

        def build(self, members):
            assert members == chunks
            calls.append("build")
            if state.get("interrupted"):
                raise KeyboardInterrupt
            state["status"] = deepcopy(state["built_status"])
            return state["status"]

        def prepare_model(self):
            calls.append("prepare_model")

    def load_corpus(directory):
        assert directory == settings.project_root / CORPUS_RELATIVE_PATH
        calls.append("load_corpus")
        return chunks

    def forbidden(*_):
        raise AssertionError("首次启动不能采集网页或读取在线刷新缓存")

    monkeypatch.setattr(index, "PolicyIndex", Index)
    monkeypatch.setattr(corpus, "load_corpus", load_corpus)
    monkeypatch.setattr(ingest, "collect", forbidden)
    monkeypatch.setattr(ingest, "load_chunks", forbidden)
    return settings, state, calls, complete


def test_first_start_builds_all_scenarios_from_fixed_corpus(initial_knowledge, capsys):
    settings, state, calls, complete = initial_knowledge
    prepare_knowledge_once(settings)
    assert state["status"] == complete
    assert calls == ["load_corpus", "status", "build"]
    assert "固定教学语料" in capsys.readouterr().out
    calls.clear()
    prepare_knowledge_once(settings)
    assert calls == ["load_corpus", "status", "prepare_model"]


def test_existing_complete_index_validates_local_model_without_rebuilding(initial_knowledge):
    settings, state, calls, complete = initial_knowledge
    state["status"] = deepcopy(complete)
    prepare_knowledge_once(settings)
    assert calls == ["load_corpus", "status", "prepare_model"]


@pytest.mark.parametrize("mismatch", ["version", "chunk_count", "missing", "extra"])
def test_existing_wrong_corpus_is_rebuilt_even_when_index_is_ready(initial_knowledge, mismatch):
    settings, state, calls, complete = initial_knowledge
    state["status"] = deepcopy(complete)
    scenarios = state["status"]["scenarios"]
    if mismatch == "version":
        scenarios[0]["version"] = "old-teaching-corpus"
    elif mismatch == "chunk_count":
        scenarios[0]["chunk_count"] += 1
    elif mismatch == "missing":
        scenarios.pop()
    else:
        scenarios.append({"scenario_id": "unexpected", "version": "old", "chunk_count": 1})
    prepare_knowledge_once(settings)
    assert calls == ["load_corpus", "status", "build"]
    assert state["status"] == complete


@pytest.mark.parametrize("empty_index", [False, True])
def test_incomplete_first_build_cannot_be_mistaken_for_initialized(initial_knowledge, empty_index):
    settings, state, calls, _ = initial_knowledge
    if empty_index:
        state["built_status"] = {"status": "no_data", "scenarios": []}
    else:
        state["built_status"]["scenarios"][0]["chunk_count"] = 0
    for _ in range(2):
        calls.clear()
        with pytest.raises(ValueError, match="知识库初始化未完成"):
            prepare_knowledge_once(settings)
        assert calls == ["load_corpus", "status", "build"]


def test_interrupted_first_build_preserves_the_fixed_corpus(initial_knowledge):
    settings, state, calls, _ = initial_knowledge
    snapshot = settings.project_root / CORPUS_RELATIVE_PATH / "catalog.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text('{"version":"teaching-v1"}')
    state["interrupted"] = True
    with pytest.raises(KeyboardInterrupt):
        prepare_knowledge_once(settings)
    assert state["status"]["status"] == "no_data"
    assert snapshot.read_text() == '{"version":"teaching-v1"}'
    assert calls == ["load_corpus", "status", "build"]
