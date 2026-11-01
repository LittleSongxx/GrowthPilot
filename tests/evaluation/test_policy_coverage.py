"""覆盖筛选必须展示缺口与原始证据，不能制造准确率。"""

from collections import Counter
from copy import deepcopy

import pytest

from evaluation.checks.policy_coverage import inspect_retrieval, load_questions, run, summarize


def question(kind="business"):
    return {
        "id": "registration-fee",
        "scenario": "meituan_rider",
        "topic": "报名费用",
        "question": "骑手报名要交钱吗？",
        "kind": kind,
        "anchors": [["报名", "注册"], ["费用", "免费"]],
        "expected_evidence": "区分报名费用和装备费用，原文未说明时保留未知。",
    }


def clause(**changes):
    return {
        "id": "official-faq:0",
        "document_id": "official-faq",
        "scenario_id": "meituan_rider",
        "title": "骑手常见问题",
        "section": "报名",
        "text": "骑手报名免费，其他费用请核对具体项目。",
        "url": "https://official.example/faq",
        "fetched_at": "2026-10-27T00:00:00Z",
        "content_hash": "sha256-original-document",
        "document_type": "official_faq",
        **changes,
    }


def retrieval(*clauses):
    return {
        "status": "success",
        "data": {
            "scenario_id": "meituan_rider",
            "corpus_version": "corpus-sha256",
            "clauses": list(clauses),
        },
        "evidence": [item["id"] for item in clauses],
        "retryable": False,
    }


def test_fixed_questions_cover_business_decisions_and_independent_privacy_cases():
    questions = load_questions()
    assert len(questions) == len({item["id"] for item in questions}) == 40
    assert Counter(item["scenario"] for item in questions) == {
        "didi_driver": 12,
        "financial_consultation": 12,
        "meituan_rider": 8,
        "meituan_merchant": 8,
    }
    privacy = [item for item in questions if item.get("kind") == "privacy"]
    assert len(privacy) == 4 and len({item["scenario"] for item in privacy}) == 4
    boundaries = [item for item in questions if item.get("kind") == "personal_boundary"]
    assert len(boundaries) >= 4
    assert all(item["must_not_claim"] for item in boundaries)


def test_only_raw_text_can_match_not_query_title_or_curated_scope():
    result = retrieval(clause(text="请咨询客服。", title="骑手报名免费", scope_note="报名免费"))
    checked = inspect_retrieval(question(), result)
    assert checked["state"] == "candidate_gap"
    assert not checked["candidates"]
    partial = inspect_retrieval(question(), retrieval(clause(text="这是注册入口。")))
    assert partial["state"] == "candidate_gap"


def test_privacy_cannot_replace_business_evidence_even_when_words_match():
    result = retrieval(clause(document_type="privacy_policy"))
    business = inspect_retrieval(question(), result)
    assert business["state"] == "candidate_gap"
    assert business["warnings"] == ["privacy_in_business_top3"]
    privacy = inspect_retrieval(question("privacy"), result)
    assert privacy["state"] == "candidate_found"
    assert not privacy["warnings"]
    # 反过来，通用常见问题也不能充当所要求的隐私政策来源。
    assert inspect_retrieval(question("privacy"), retrieval(clause()))["state"] == "candidate_gap"


def test_business_candidate_does_not_hide_privacy_pollution_in_top_results():
    result = retrieval(
        clause(id="privacy:0", document_type="privacy_policy"),
        clause(),
    )
    checked = inspect_retrieval(question(), result)
    assert checked["state"] == "candidate_found"
    assert checked["candidates"][0]["id"] == "official-faq:0"
    assert checked["privacy_in_top3"] == ["privacy:0"]
    assert checked["warnings"]


def test_cross_scenario_match_is_invalid_and_cannot_count_as_candidate():
    checked = inspect_retrieval(question(), retrieval(clause(scenario_id="meituan_merchant")))
    assert checked["state"] == "evidence_invalid"
    assert checked["scenario_isolation"] == "violated"
    assert not checked["candidates"]


@pytest.mark.parametrize("field", ["text", "url", "content_hash", "document_type"])
def test_success_status_without_complete_source_is_not_accepted(field):
    item = clause()
    item.pop(field)
    checked = inspect_retrieval(question(), retrieval(item))
    assert checked["state"] == "evidence_invalid"
    assert not checked["candidates"]


def test_missing_corpus_and_unbound_evidence_remain_visible():
    result = retrieval(clause())
    result["data"].pop("corpus_version")
    result["evidence"] = []
    checked = inspect_retrieval(question(), result)
    assert checked["state"] == "evidence_invalid"
    assert "missing_corpus_version" in checked["issues"]
    assert "rank_1:unbound_evidence_id" in checked["issues"]


def test_no_data_is_a_gap_not_an_implicit_success_or_execution_failure():
    result = {"status": "no_data", "data": {"clauses": []}, "evidence": []}
    assert inspect_retrieval(question(), result)["state"] == "candidate_gap"


@pytest.mark.parametrize(
    "reason", ["knowledge_not_indexed", "scenario_not_indexed", "index_rebuild_required"]
)
def test_index_precondition_failure_is_not_reported_as_a_business_knowledge_gap(reason):
    result = {"status": "no_data", "data": {"clauses": [], "reason": reason}, "evidence": []}
    checked = inspect_retrieval(question(), result)
    assert checked["state"] == "index_unavailable" and checked["reason"] == reason
    summary = summarize([question() | {"screening": checked, "review": {"verdict": "pending"}}])
    assert summary["candidate_gaps"] == []


def test_anchor_match_keeps_evidence_review_pending_and_preserves_original_text():
    # 否定表达仍可能匹配这些锚点，因此筛选明确不能保证语义正确。
    original = retrieval(clause(text="现有资料没有说明报名是否免费。"))

    class Index:
        def search(self, query, scenario):
            assert query == question()["question"] and scenario == "meituan_rider"
            return deepcopy(original)

    report = run(Index(), [question()])
    case = report["cases"][0]
    assert case["screening"]["state"] == "candidate_found"
    assert case["review"]["verdict"] == "pending"
    assert case["retrieval"] == original
    assert report["accuracy_evaluation"] == "not_performed"
    assert report["agent_boundary_check"] == "not_run"
    assert "passed" not in report and "accuracy" not in report["summary"]
    case["review"] = {
        "verdict": "unsupported",
        "reason": "原文明确没有说明费用，不能回答为免费。",
        "evidence_ids": ["official-faq:0"],
    }
    summary = summarize(report["cases"])
    assert summary["evidence_review"] == {"unsupported": 1}
    assert summary["reviewed_gaps"][0]["review"] == case["review"]


def test_one_search_failure_does_not_hide_remaining_questions():
    class Index:
        def search(self, query, scenario):
            if query == "失败查询":
                raise RuntimeError("private implementation detail")
            return retrieval(clause())

    report = run(Index(), [question() | {"id": "error", "question": "失败查询"}, question()])
    assert report["summary"]["screening"] == {"execution_error": 1, "candidate_found": 1}
    assert report["summary"]["evidence_review"] == {"pending": 2}
    assert report["cases"][0]["screening"] == {
        "state": "execution_error",
        "error_category": "RuntimeError",
    }
