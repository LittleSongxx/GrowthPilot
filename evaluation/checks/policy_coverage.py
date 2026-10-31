"""固定业务问题的本地检索检查，保留原文供逐条审阅，不输出问答准确率。

uv run python -m evaluation.checks.policy_coverage --output .run/checks/policy-coverage.json
可加 --scenario didi_driver。只查询已建本地索引，不抓网页、不调用业务 Agent。
审阅者填写每题 review 的 supported/partial/unsupported、理由和证据 ID 后，
可用 summarize(report['cases']) 重算摘要；锚点筛选永远不能替代这一步。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import unicodedata
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from backend.paths import resolve_project_path

from ..common.io import dump

QUESTION_PATH = Path(__file__).parent / "data/policy_coverage.yaml"
LIMITATIONS = [
    "正文锚点仅筛选候选，不能判断否定、条件、时效或是否完整回答；不是准确率。",
    "没有候选可能是知识缺失、召回失败或措辞差异；须阅读全部返回原文再判定。",
    "业务题的隐私来源不计入候选；隐私专项单独检查，不用隐私条款数量代替业务覆盖。",
    "本检查不生成回答；个人审批、利率、收入与到账承诺仍需实际 Agent 检查。",
]


def load_questions(path=QUESTION_PATH):
    """题目与语料独立保存；不要因为当前检索不命中就删题或改窄要求。"""
    questions = yaml.safe_load(resolve_project_path(path).read_text())["questions"]
    seen = set()
    for question in questions:
        for field in ("id", "scenario", "topic", "question", "expected_evidence"):
            if not isinstance(question.get(field), str) or not question[field].strip():
                raise ValueError(f"题目缺少非空 {field}")
        if question["id"] in seen:
            raise ValueError(f"重复题目：{question['id']}")
        seen.add(question["id"])
        if question.get("kind", "business") not in {"business", "privacy", "personal_boundary"}:
            raise ValueError("未知题目类型")
        groups = question.get("anchors")
        if (
            not isinstance(groups, list)
            or not groups
            or any(
                not isinstance(group, list)
                or not group
                or any(not isinstance(term, str) or not term.strip() for term in group)
                for group in groups
            )
        ):
            raise ValueError("anchors 必须是非空字符串组列表")
        if question.get("kind") == "personal_boundary" and not question.get("must_not_claim"):
            raise ValueError("个人边界题必须声明不能外推的结论")
    return questions


def inspect_retrieval(question, result):
    """结构检查与候选筛选，不自动判断事实是否支持答案。"""
    data = result.get("data", {})
    clauses = data.get("clauses", [])
    issues = []
    if not isinstance(clauses, list):
        return {"state": "evidence_invalid", "issues": ["clauses_not_list"], "candidates": []}
    if result.get("status") != "success":
        unavailable = data.get("reason") in {
            "knowledge_not_indexed",
            "scenario_not_indexed",
            "index_rebuild_required",
        }
        return {
            "state": "evidence_invalid"
            if clauses
            else "index_unavailable"
            if unavailable
            else "candidate_gap",
            "reason": data.get("reason"),
            "issues": ["unsuccessful_result_has_clauses"] if clauses else [],
            "candidates": [],
        }
    if not clauses:
        issues.append("success_without_clauses")
    if not data.get("corpus_version"):
        issues.append("missing_corpus_version")
    if data.get("scenario_id") != question["scenario"]:
        issues.append("result_scenario_mismatch")
    candidates, privacy_top3 = [], []
    privacy_question = question.get("kind") == "privacy"
    for rank, clause in enumerate(clauses, 1):
        if not isinstance(clause, dict):
            issues.append(f"rank_{rank}:not_a_clause")
            continue
        valid = True
        for field in (
            "id",
            "document_id",
            "title",
            "text",
            "url",
            "fetched_at",
            "content_hash",
            "document_type",
        ):
            if not isinstance(clause.get(field), str) or not clause[field].strip():
                issues.append(f"rank_{rank}:missing_{field}")
                valid = False
        url = urlsplit(str(clause.get("url", "")))
        if url.scheme != "https" or not url.hostname:
            issues.append(f"rank_{rank}:invalid_source_url")
            valid = False
        if clause.get("scenario_id") != question["scenario"]:
            issues.append(f"rank_{rank}:cross_scenario_evidence")
            valid = False
        if clause.get("id") not in result.get("evidence", []):
            issues.append(f"rank_{rank}:unbound_evidence_id")
            valid = False
        privacy = clause.get("document_type") == "privacy_policy"
        if privacy and rank <= 3:
            privacy_top3.append(clause.get("id"))
        # 只匹配真正的正文，标题/检索词/人工 scope_note 都不能伪装成命中。
        text = unicodedata.normalize("NFKC", str(clause.get("text", ""))).casefold()
        matched = [
            [term for term in group if unicodedata.normalize("NFKC", term).casefold() in text]
            for group in question["anchors"]
        ]
        if valid and privacy == privacy_question and all(matched):
            candidates.append({"id": clause["id"], "rank": rank, "matched_anchors": matched})
    return {
        "state": "evidence_invalid"
        if issues
        else "candidate_found"
        if candidates
        else "candidate_gap",
        "issues": issues,
        "candidates": candidates,
        "privacy_in_top3": privacy_top3,
        "warnings": ["privacy_in_business_top3"] if privacy_top3 and not privacy_question else [],
        "scenario_isolation": "violated"
        if any("scenario" in issue for issue in issues)
        else "observed",
    }


def summarize(cases):
    """报告计数和审阅结论，不把 candidate_found 命名为通过或覆盖率。"""
    return {
        "questions": len(cases),
        "scenarios": dict(Counter(case["scenario"] for case in cases)),
        "screening": dict(Counter(case["screening"]["state"] for case in cases)),
        "evidence_review": dict(Counter(case["review"]["verdict"] for case in cases)),
        "candidate_gaps": [
            {key: case[key] for key in ("id", "scenario", "topic")}
            for case in cases
            if case["screening"]["state"] == "candidate_gap"
        ],
        "privacy_pollution": [case["id"] for case in cases if case["screening"].get("warnings")],
        "reviewed_gaps": [
            {key: case[key] for key in ("id", "scenario", "topic", "review")}
            for case in cases
            if case["review"]["verdict"] in {"partial", "unsupported"}
        ],
    }


def run(index, questions):
    cases = []
    for question in questions:
        case = {
            **question,
            "review": {"verdict": "pending", "reason": "", "evidence_ids": []},
        }
        started = time.monotonic()
        try:
            case["retrieval"] = index.search(question["question"], question["scenario"])
            case["screening"] = inspect_retrieval(question, case["retrieval"])
        except Exception as exc:  # noqa: BLE001 - 保留其他用例，不暴露服务供应商细节。
            case["screening"] = {"state": "execution_error", "error_category": type(exc).__name__}
        case["elapsed_seconds"] = round(time.monotonic() - started, 3)
        cases.append(case)
    return {
        "created_at": datetime.now(UTC).isoformat(),
        "question_set_sha256": hashlib.sha256(
            json.dumps(questions, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest(),
        "accuracy_evaluation": "not_performed",
        "agent_boundary_check": "not_run",
        "limitations": LIMITATIONS,
        "cases": cases,
        "summary": summarize(cases),
    }


def main():
    from backend.agent_harness.rag.index import PolicyIndex
    from backend.settings import Settings

    questions = load_questions()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=resolve_project_path, required=True)
    parser.add_argument("--scenario", choices=sorted({q["scenario"] for q in questions}))
    args = parser.parse_args()
    if args.output.exists():
        parser.error("请使用新的输出路径，保留已有报告和审阅结论。")
    if args.scenario:
        questions = [q for q in questions if q["scenario"] == args.scenario]
    settings = Settings.from_env()
    report = run(
        PolicyIndex(settings.runtime_dir / "knowledge", database=settings.app_db), questions
    )
    report["question_file_sha256"] = hashlib.sha256(QUESTION_PATH.read_bytes()).hexdigest()
    dump(args.output, report)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    # 退出码只表示执行/证据完整性；candidate_found 仍必须逐条阅读后才能判覆盖。
    return int(
        any(
            case["screening"]["state"]
            in {"execution_error", "evidence_invalid", "index_unavailable"}
            for case in report["cases"]
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
