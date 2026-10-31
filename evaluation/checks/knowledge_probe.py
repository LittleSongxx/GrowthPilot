"""四场景真实知识检索冒烟；--agent 再验证真实模型的原生工具调用。

uv run python -m evaluation.checks.knowledge_probe --output .run/checks/knowledge-check.json [--agent]
默认只使用本地向量模型；--agent 调用已配置的文本模型。此报告不代表问答准确率。
"""

import argparse
import asyncio
import hashlib
import json
import secrets
import time
from dataclasses import replace
from datetime import UTC, datetime

import yaml

from backend.agent_harness.rag.index import PolicyIndex
from backend.database import temporary_database
from backend.paths import resolve_project_path
from backend.settings import Settings, model_connection

from ..common.io import dump
from ..common.trace import record_agent_turn
from ..common.trace import session_events as _session_events  # noqa: F401 - 保留历史分页读取入口。

QUESTIONS = {
    "meituan_rider": "美团众包骑手报名要交报名费吗？需要自己准备电动车吗？",
    "meituan_merchant": "美团外卖开店需要交什么费用？开店免费是不是每笔订单也不收服务费？",
    "didi_driver": "加入滴滴做网约车司机，对驾驶证和驾驶经历有哪些要求？",
    "financial_consultation": "度小满服务信息公示怎么说明提前还款费用？这能代表我个人现在的费用吗？",
}


def load_questions(path=None):
    """外部题集只给用户话语与审阅要求；要求不会发送给业务 Agent。"""
    if path is None:
        return [
            {"id": scenario, "scenario": scenario, "question": question}
            for scenario, question in QUESTIONS.items()
        ]
    questions = yaml.safe_load(resolve_project_path(path).read_text())["questions"]
    if not isinstance(questions, list) or not questions:
        raise ValueError("questions 必须是非空列表")
    seen = set()
    for case in questions:
        for field in ("id", "scenario", "question"):
            if not isinstance(case.get(field), str) or not case[field].strip():
                raise ValueError(f"题目缺少非空 {field}")
        if case["id"] in seen or case["scenario"] not in QUESTIONS:
            raise ValueError("重复题目 ID 或未知场景")
        seen.add(case["id"])
        followups = case.get("followups", [])
        if not isinstance(followups, list) or any(
            not isinstance(turn, str) or not turn.strip() for turn in followups
        ):
            raise ValueError("followups 必须是非空字符串组成的列表")
        if len("\n".join([case["question"], *followups])) > 1000:
            raise ValueError("累计用户话语超过检索长度限制")
    return questions


def has_evidence(result, scenario):
    data = result.get("data", {})
    clauses = data.get("clauses", [])
    return bool(
        result.get("status") == "success"
        and data.get("corpus_version")
        and clauses
        and all(
            clause.get("scenario_id") == scenario
            and clause.get("text")
            and clause.get("title")
            and str(clause.get("url", "")).startswith("https://")
            and clause.get("id") in result.get("evidence", [])
            for clause in clauses
        )
    )


def save(output, report):
    dump(output, report)


async def agent_probe(settings, index, report, output):
    from backend.agent_harness import BackendService, load_scenario
    from backend.agent_harness.prompts import PROMPT_VERSION
    from backend.agent_harness.runtime.session_manager import SessionManager

    connection = model_connection(settings)
    report.update(model=connection.model, prompt_version=PROMPT_VERSION)
    if not connection.api_key:
        report["agent_error_category"] = "MissingModelCredential"
        return
    with temporary_database(settings.app_db, prefix="growth_knowledge") as database:
        isolated = replace(
            settings,
            app_db=database,
            session_secret=secrets.token_urlsafe(24),
        )
        services = {
            scenario: BackendService(
                isolated.app_db,
                load_scenario(
                    settings.project_root / f"backend/scenarios/{scenario}/scenario.yaml"
                ),
            )
            for scenario in {case["scenario"] for case in report["cases"]}
        }
        manager = SessionManager(isolated, services)
        manager._policy_index = index
        try:
            for case in report["cases"]:
                scenario, started = case["scenario"], time.monotonic()
                result = case["agent"] = {"passed": False, "turns": []}
                conversation = None
                try:
                    conversation = await manager.create(scenario, "knowledge-probe-" + case["id"])
                    for number, message in enumerate(
                        [case["question"], *case.get("followups", [])], 1
                    ):
                        turn = {"turn": number, "user_message": message}
                        if number == 1:
                            turn["user_message"] = "方便交流。" + message
                        result["turns"].append(turn)
                        await record_agent_turn(manager, conversation, turn)
                        save(output, report)
                    result["reply"] = result["turns"][-1]["reply"]
                    result["retrieval_events"] = [
                        event for turn in result["turns"] for event in turn["retrieval_events"]
                    ]
                    results = [
                        event["data"]
                        for event in result["retrieval_events"]
                        if event["type"] == "tool_result"
                    ]
                    result["passed"] = (
                        bool(result["reply"])
                        and any(
                            event.get("state") == "success"
                            and has_evidence(event.get("result", {}), scenario)
                            for event in results
                        )
                        and all(event.get("state") == "success" for event in results)
                    )
                    result["scenario_snapshot"] = conversation.ctx.snapshot_hash
                except Exception as exc:  # noqa: BLE001 - 保留用例失败情况，不暴露服务供应商请求信息。
                    # 原始供应商异常可能包含请求信息；报告只保留异常类型。
                    result["error_category"] = type(exc).__name__
                finally:
                    if conversation:
                        await conversation.stop("knowledge_probe_complete")
                result["elapsed_seconds"] = round(time.monotonic() - started, 3)
                save(output, report)
                print(scenario, "agent_passed=" + str(result["passed"]), flush=True)
        finally:
            await manager.close()


async def run(output, agent=False, questions_path=None, case_ids=None):
    output = resolve_project_path(output)
    if output.exists():
        raise ValueError("请使用新的输出路径，保留已有报告。")
    questions = load_questions(questions_path)
    question_set_hash = hashlib.sha256(
        json.dumps(questions, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    if case_ids:
        if len(set(case_ids)) != len(case_ids) or set(case_ids) - {c["id"] for c in questions}:
            raise ValueError("复测 case ID 重复或不在原题集内")
        questions = [case for case in questions if case["id"] in case_ids]
    settings = Settings.from_env()
    index = PolicyIndex(settings.runtime_dir / "knowledge", database=settings.app_db)
    report = {
        "pipeline": "policy-retrieval-agent" if agent else "policy-retrieval",
        "created_at": datetime.now(UTC).isoformat(),
        "accuracy_evaluation": "not_performed",
        "passed_scope": "仅检索证据结构与工具调用冒烟；不是业务覆盖或回答正确性",
        "question_set_sha256": question_set_hash,
        "selected_case_ids": [case["id"] for case in questions],
        "question_file_sha256": hashlib.sha256(resolve_project_path(questions_path).read_bytes()).hexdigest()
        if questions_path else None,
        "index_before": index.status(),
        "retrieval_query_method": "首轮原问；后续轮拼接截至当轮的用户话语，不使用审阅要求或未来话语",
        "cases": [],
    }
    for question in questions:
        scenario = question["scenario"]
        started = time.monotonic()
        case = {**question, "retrieval_passed": False, "retrieval_turns": [],
                "retrieval_review": {"verdict": "pending", "reason": "", "evidence_ids": []},
                "agent_review": {"business_coverage": "pending", "faithfulness": "pending",
                                 "unknown_next_step": "pending", "reason": ""}}
        report["cases"].append(case)
        try:
            messages = [case["question"], *case.get("followups", [])]
            for turn_number in range(1, len(messages) + 1):
                query = "\n".join(messages[:turn_number])
                retrieved = await asyncio.to_thread(index.search, query, scenario)
                case["retrieval_turns"].append(
                    {"turn": turn_number, "query": query, "result": retrieved}
                )
            case["retrieval"] = case["retrieval_turns"][0]["result"]
            case["retrieval_passed"] = all(
                has_evidence(turn["result"], scenario) for turn in case["retrieval_turns"]
            )
        except Exception as exc:  # noqa: BLE001 - 保留全部用例，错误仅报告类别。
            case["error_category"] = type(exc).__name__
        case["retrieval_seconds"] = round(time.monotonic() - started, 3)
        save(output, report)
        print(scenario, "retrieval_passed=" + str(case["retrieval_passed"]), flush=True)
    retrieval_passed = all(case["retrieval_passed"] for case in report["cases"])
    # no_data 是待审阅的覆盖缺口，不能阻止真实 Agent 的补查/未知处理检查。
    if agent:
        await agent_probe(settings, index, report, output)
    report["index_after"] = index.status()
    report["corpus_unchanged"] = report["index_before"] == report["index_after"]
    report["passed"] = retrieval_passed and report["corpus_unchanged"] and (
        not agent or all(case.get("agent", {}).get("passed", False) for case in report["cases"])
    )
    save(output, report)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=resolve_project_path, required=True)
    parser.add_argument("--agent", action="store_true", help="调用真实文本模型，使用临时业务数据库")
    parser.add_argument("--questions", type=resolve_project_path, help="冻结 YAML 题集，支持 followups 多轮用户话语")
    parser.add_argument("--case", action="append", dest="case_ids",
                        help="只复测指定原题 ID，可重复使用；保留完整题集哈希及全部追问")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("请使用新的输出路径，保留已有报告。")
    raise SystemExit(0 if asyncio.run(
        run(args.output, args.agent, args.questions, args.case_ids)
    ) else 1)
