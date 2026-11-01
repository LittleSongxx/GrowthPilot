"""探针保留缺口与逐轮证据；冒烟标记不能代替业务审阅。"""

import hashlib
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from evaluation.checks import knowledge_probe


def test_frozen_closure_questions_cover_each_stage_in_each_scenario():
    path = Path("evaluation/checks/data/knowledge_questions.yaml")
    cases = knowledge_probe.load_questions(path)
    assert len(cases) == 16
    assert Counter(c["scenario"] for c in cases) == dict.fromkeys(knowledge_probe.QUESTIONS, 4)
    assert len([c for c in cases if c.get("followups")]) == 4
    assert all(len({c["topic"] for c in cases if c["scenario"] == s}) == 4
               for s in knowledge_probe.QUESTIONS)


@pytest.mark.parametrize("changes", [{"scenario": "unknown"}, {"followups": [""]}])
def test_bad_question_data_is_rejected(tmp_path, changes):
    path = tmp_path / "questions.yaml"
    case = {"id": "a", "scenario": "meituan_rider", "question": "报名？", **changes}
    path.write_text(yaml.safe_dump({"questions": [case]}))
    with pytest.raises(ValueError):
        knowledge_probe.load_questions(path)


async def test_no_data_does_not_skip_agent_or_become_business_pass(tmp_path, monkeypatch):
    path = tmp_path / "questions.yaml"
    path.write_text(yaml.safe_dump({"questions": [
        {"id": "a", "scenario": "meituan_rider", "question": "报名？",
         "followups": ["装备呢？"], "expected_evidence": "不能发送给Agent"}
    ]}))
    queries, called = [], []

    class Index:
        def __init__(self, _, *, database):
            pass

        def status(self):
            return {"status": "ready", "version": "same"}

        def search(self, query, scenario):
            queries.append((query, scenario))
            return {"status": "no_data", "data": {"clauses": []}, "evidence": []}

    async def agent_probe(settings, index, report, output):
        called.append(True)

    monkeypatch.setattr(knowledge_probe, "PolicyIndex", Index)
    monkeypatch.setattr(knowledge_probe, "agent_probe", agent_probe)
    monkeypatch.setattr(knowledge_probe.Settings, "from_env",
                        lambda: SimpleNamespace(runtime_dir=tmp_path, app_db=object()))
    output = tmp_path / "result.json"
    assert not await knowledge_probe.run(output, agent=True, questions_path=path)
    report = json.loads(output.read_text())
    assert called and queries == [("报名？", "meituan_rider"), ("报名？\n装备呢？", "meituan_rider")]
    assert report["cases"][0]["retrieval_review"]["verdict"] == "pending"
    assert report["cases"][0]["agent_review"]["business_coverage"] == "pending"
    assert report["accuracy_evaluation"] == "not_performed"
    with pytest.raises(ValueError, match="新的输出路径"):
        await knowledge_probe.run(output, questions_path=path)


async def test_turn_evidence_is_not_lost_on_failure_or_leaked_into_next_turn():
    events = []

    class Store:
        def events(self, sid, after=0, limit=3000):
            return [e for e in events if e["seq"] > after][:limit]

    class Conversation:
        id = "isolated_test"

        async def message(self, message):
            events.append({"seq": len(events) + 1, "type": "tool_result", "data": {
                "name": "search_policy", "result": {"status": "no_data", "query": message}}})
            if message == "failure":
                raise TimeoutError("private provider detail")
            return "没有资料，待核实。"

    manager = SimpleNamespace(store=Store())
    conversation = Conversation()
    first = {"user_message": "first"}
    await knowledge_probe.record_agent_turn(manager, conversation, first)
    second = {"user_message": "failure"}
    with pytest.raises(TimeoutError):
        await knowledge_probe.record_agent_turn(manager, conversation, second)
    assert [e["seq"] for e in first["retrieval_events"]] == [1]
    assert [e["seq"] for e in second["retrieval_events"]] == [2]
    assert second["tool_events"][0]["data"]["result"]["status"] == "no_data"


async def test_recheck_preserves_frozen_set_hash_and_all_followups(tmp_path, monkeypatch):
    cases = [{"id": "first", "scenario": "meituan_rider", "question": "报名？",
              "followups": ["装备呢？"]},
             {"id": "second", "scenario": "meituan_rider", "question": "材料？"}]
    queries = []

    def search(query, scenario):
        queries.append(query)
        return {"status": "no_data", "data": {"clauses": []}, "evidence": []}

    monkeypatch.setattr(knowledge_probe, "load_questions", lambda path: cases)
    monkeypatch.setattr(knowledge_probe, "PolicyIndex", lambda root, *, database: SimpleNamespace(
        status=lambda: {"status": "ready"}, search=search))
    monkeypatch.setattr(knowledge_probe.Settings, "from_env",
                        lambda: SimpleNamespace(runtime_dir=tmp_path, app_db=object()))
    output = tmp_path / "recheck.json"
    await knowledge_probe.run(output, case_ids=["first"])
    report = json.loads(output.read_text())
    assert queries == ["报名？", "报名？\n装备呢？"]
    assert report["selected_case_ids"] == ["first"]
    assert report["question_set_sha256"] == hashlib.sha256(
        json.dumps(cases, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    with pytest.raises(ValueError, match="原题集"):
        await knowledge_probe.run(tmp_path / "unknown.json", case_ids=["unknown"])
