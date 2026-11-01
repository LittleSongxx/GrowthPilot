"""复访评审保留可见进展、当前问题与事实边界，不假定隐藏历史已经完成目标。"""

from copy import deepcopy

import pytest

from evaluation.judges.evidence import evidence_for, judge_payload


def followup_trace():
    return {
        "case_id": "rider-followup", "scenario_id": "meituan_rider", "repeat": 2,
        "initial_profile": {"area": {"status": "known", "value": "武汉"}},
        "turns": [{
            "turn": 1, "origin": "customer",
            "user_message": "服务点不用再查了，之前那个我记下了。我还没注册，健康证明怎么办？",
            "reply": "当地办理细则暂时无法核实，您可以先联系已经记下的服务点确认。",
            "tool_events": [],
        }],
    }


def followup_case():
    return {
        "id": "rider-followup",
        "goal": {
            "description": "获取武汉后查询服务点，并帮助客户继续了解报名",
            "success_conditions": ["查询和解释城市范围服务选项", "处理当前报名疑问"],
        },
        "customer": {
            "initial_state": "私有设定：此前没有查询过服务点",
            "acceptance_conditions": ["私有设定：必须再次查服务点"],
        },
    }


def test_followup_preserves_customer_declining_duplicate_step_and_new_question():
    trace, case = followup_trace(), followup_case()
    before = deepcopy((trace, case))

    payload = judge_payload(trace, case)

    assert payload["goal"]["call_number"] == 2
    assert payload["goal"]["success_conditions"] == case["goal"]["success_conditions"]
    assert payload["memory_at_start"] == {"area": {"status": "known", "value": "武汉"}}
    assert payload["turns"] == [{
        "turn": 1,
        "customer": "服务点不用再查了，之前那个我记下了。我还没注册，健康证明怎么办？",
        "agent": "当地办理细则暂时无法核实，您可以先联系已经记下的服务点确认。",
    }]
    assert (trace, case) == before
    assert set(payload) == {"goal", "memory_at_start", "turns"}


def test_followup_correction_stays_after_old_memory_and_does_not_rewrite_it():
    trace = followup_trace()
    trace["initial_profile"]["has_vehicle"] = {"status": "known", "value": True}
    trace["turns"][0]["user_message"] = "上次说有车，但车已经坏了。先别发短信，换电在我这能用吗？"
    trace["turns"][0]["reply"] = "本次还无法确认换电覆盖范围，需要联系服务商核实。"
    before = deepcopy(trace)

    payload = judge_payload(trace, followup_case())

    assert payload["memory_at_start"]["has_vehicle"] == {"status": "known", "value": True}
    assert payload["turns"][0]["customer"] == trace["turns"][0]["user_message"]
    assert payload["turns"][0]["agent"] == trace["turns"][0]["reply"]
    assert "materials" not in payload["turns"][0]
    assert trace == before


@pytest.mark.parametrize("status", ["known", "unknown", "declined"])
def test_followup_memory_never_becomes_action_receipt_or_hidden_completion(status):
    trace = followup_trace()
    trace["initial_profile"]["registration"] = {
        "status": status, "value": "已注册" if status == "known" else None,
    }
    baseline = judge_payload(trace, followup_case())
    trace["customer_history_before_call"] = [{
        "role": "assistant", "content": "私有历史：我已注册，并同意下一通发短信。",
    }]
    trace["profile_processing"] = {"status": "completed", "hidden": "通话结束后才写入的结果"}

    payload = judge_payload(trace, followup_case())
    evidence = evidence_for(trace)

    assert payload == baseline
    assert payload["memory_at_start"]["registration"] == trace["initial_profile"]["registration"]
    assert evidence["profile/registration"]["kind"] == "historical_customer_profile"
    assert evidence["profile/registration"]["supports"] == "recorded_customer_statement_only"
    assert {item["kind"] for item in evidence.values()} == {
        "historical_customer_profile", "customer_statement", "agent_reply",
    }
