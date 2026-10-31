"""保留业务契约与证据边界回归，不再加载旧固定客户脚本。"""

import json

import pytest

from backend.database import DatabaseConfig
from backend.settings import Settings
from evaluation.checks.contracts import experiments
from evaluation.judges.evidence import evidence_for
from evaluation.judges.schema import QUALITY_DIMENSIONS
from evaluation.runner import run_suite


def example_trace():
    return {"case_id": "rider-policy", "scenario_id": "meituan_rider", "turns": [{
        "turn": 1, "user_message": "需要报名费吗？", "reply": "资料说明不收取报名费。",
        "tool_events": [{"seq": 2, "type": "tool_result", "data": {
            "name": "search_policy", "state": "success", "result": {
                "status": "success", "evidence": ["policy-1"], "data": {
                    "clauses": [{"id": "policy-1", "scenario_id": "meituan_rider",
                                 "text": "不收取报名费。", "title": "官方说明",
                                 "url": "https://example.org/policy",
                                 "scope_note": "编造的月收入一万元"}],
                },
            },
        }}],
    }]}


def example_case():
    return {
        "id": "rider-policy", "goal": {
            "type": "consultation", "description": "理解报名费用",
            "success_conditions": ["区分报名费用与其他费用，不强推注册"],
        },
        "customer": {"initial_state": "尚未报名，只咨询费用"},
    }


def example_review(trace):
    return {
        dimension: {
            "score": 5, "reason": "回答了客户咨询的问题，政策原文支持费用说明。",
        }
        for dimension in QUALITY_DIMENSIONS
    }


async def test_four_scenario_offline_catalog_contracts_execute_without_models(tmp_path):
    report = await experiments(tmp_path)
    assert report["passed"] and report["model_requests"] == 0
    assert report["dialogue_quality"] == "not_evaluated"
    assert report["external_conversion"] == "not_measured"
    assert len(report["scenarios"]) == 4
    assert report["checks"]["memory_persistence_and_isolation"]["cross_process_persistence"]
    assert "cases" not in report and "experiments" not in report


@pytest.mark.parametrize("suite", ["contracts", "business-coverage"])
def test_offline_contract_commands_do_not_require_provider_credentials(tmp_path, suite, capsys):
    settings = Settings(tmp_path, tmp_path / "runtime", DatabaseConfig.from_env(), agent_api_key="")
    assert run_suite(suite, settings, run_id="offline-check") == 0
    output = tmp_path / ".run/checks/offline-check"
    summary = json.loads((output / "summary.json").read_text())
    assert summary["passed"] and summary["model_requests"] == 0
    assert summary["quality_evaluated"] is False and summary["suite"] == suite
    assert not (tmp_path / "evaluation/reports").exists()
    assert (output / "report.json").is_file()
    assert "passed" in capsys.readouterr().out
    with pytest.raises(FileExistsError):
        run_suite(suite, settings, run_id="offline-check")
    with pytest.raises(ValueError, match="始终离线"):
        run_suite(suite, settings, execute=True, run_id="unexpected-execute")


@pytest.mark.parametrize("tool", ["estimate_work_budget", "estimate_order_contribution"])
def test_budget_result_is_conditional_evidence_and_failed_calculation_is_not(tool):
    trace = example_trace()
    event = {"seq": 7, "type": "tool_result", "data": {
        "name": tool, "state": "success", "result": {
            "status": "success", "data": {"calculation": {
                "assumptions": {"monthly_income": 3000}, "surplus": 2000,
            }},
        },
    }}
    trace["turns"][0]["tool_events"].append(event)
    package = evidence_for(trace)
    assert package["source/7/calculation"]["kind"] == "business_calculation"
    assert "assumptions.monthly_income: 3000" in package["source/7/calculation"]["text"]
    event["data"]["result"]["status"] = "invalid_input"
    assert "source/7/calculation" not in evidence_for(trace)
