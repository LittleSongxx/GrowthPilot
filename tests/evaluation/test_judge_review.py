"""验证裁判输入、简洁评分协议与错误留档，不调用真实模型。"""

import copy
import json
import re
from types import SimpleNamespace

import pytest

from evaluation.judges.evidence import evidence_for, judge_payload
from evaluation.judges.review import JUDGE_PROMPT, JUDGE_PROMPT_PATH, review_trace, validate_review
from evaluation.judges.schema import JUDGE_RESPONSE_DIMENSIONS, JUDGE_VERSION, QUALITY_DIMENSIONS


def policy_event(seq=10):
    return {
        "seq": seq, "type": "tool_result",
        "data": {
            "name": "search_policy", "state": "success",
            "result": {
                "status": "success", "evidence": ["policy-1"],
                "data": {"clauses": [{
                    "id": "policy-1", "scenario_id": "meituan_rider",
                    "text": "不收取报名费。装备费用需另行确认。",
                    "url": "https://example.org/policy", "scope_note": "隐藏整理提示",
                }]},
            },
        },
    }


def sample_trace():
    return {
        "case_id": "rider", "scenario_id": "meituan_rider",
        "turns": [
            {"turn": 0, "origin": "host_control", "user_message": "内部开场指令",
             "reply": "您好，方便了解骑手报名吗？", "tool_events": []},
            {"turn": 1, "origin": "customer", "user_message": "报名要收费吗？",
             "reply": "资料说不收取报名费。装备费用需另行确认。",
             "tool_events": [policy_event()],
             "business_audit_events": [{"data": {"hidden": "不能给评审的内部审计"}}],
             "skill_events": [{"data": {"text": "不能作为政策的技能正文"}}],
             "events": [{"data": {"hidden": "不能复制完整事件"}}]},
        ],
    }


def sample_case():
    return {
        "id": "rider",
        "goal": {"type": "resolve_concern", "description": "解释报名费和装备费用",
                 "success_conditions": ["明确两类费用区别"]},
        "customer": {
            "profile": "只供客户模型使用的角色说明",
            "initial_state": "尚未注册", "acceptance_conditions": ["先解释费用"],
            "refusal_conditions": ["强推注册时拒绝"],
            "disclosure_rules": ["只供客户模型使用的披露指令"],
        },
    }


def sample_review():
    return {
        dimension: {"score": 5, "reason": "回答区分了两类费用，并明确未确认项"}
        for dimension in JUDGE_RESPONSE_DIMENSIONS
    }


def expected_review(review=None):
    return {JUDGE_RESPONSE_DIMENSIONS[key]: row for key, row in (review or sample_review()).items()}


def test_prompt_json_example_matches_the_actual_response_contract():
    examples = re.findall(r"```json\s*\n(.*?)\n```", JUDGE_PROMPT, re.DOTALL)
    assert len(examples) == 1
    review = json.loads(examples[0])
    before = copy.deepcopy(review)

    assert validate_review(review, sample_trace()) == expected_review(review)
    assert review == before
    assert any(row["score"] is None for row in review.values())
    assert any(type(row["score"]) is int for row in review.values())


def test_payload_has_three_parts_without_hidden_customer_rules_or_internal_events():
    trace = sample_trace()
    trace["turns"][1]["tool_events"].append(policy_event(seq=11))
    before = copy.deepcopy(trace)
    payload = judge_payload(trace, sample_case())
    serialized = json.dumps(payload, ensure_ascii=False)

    assert set(payload) == {"goal", "memory_at_start", "turns"}
    assert payload["goal"] == {
        "description": "解释报名费和装备费用", "success_conditions": ["明确两类费用区别"],
        "call_number": 1,
    }
    assert payload["memory_at_start"] == {}
    assert payload["turns"][0] == {"turn": 0, "agent": "您好，方便了解骑手报名吗？"}
    assert payload["turns"][1]["customer"] == "报名要收费吗？"
    assert len(payload["turns"][1]["materials"]) == 1
    for hidden in ("内部开场指令", "不能给评审的内部审计", "不能作为政策的技能正文",
                   "不能复制完整事件", "隐藏整理提示", "只供客户模型使用", "尚未注册",
                   "acceptance_conditions", "refusal_conditions", "review_tasks", "reply_ref"):
        assert hidden not in serialized
    assert trace == before


def test_followup_payload_contains_actual_start_memory_but_not_hidden_customer_history():
    trace = sample_trace()
    trace.update(
        repeat=2, session_id="second-call",
        initial_profile={"area": {"value": "上海", "status": "known", "hidden": "不复制内部元数据"}},
        customer_history_before_call=[{"role": "assistant", "content": "上次已收到短信"}],
        business_context={
            "app_name": "骑手 App", "agent_role": "报名客服",
            "tool_capabilities": [{"name": "send_download_sms", "description": "客户同意后可发送短信"}],
            "private_config": "不可提供的配置",
        },
    )
    payload = judge_payload(trace, sample_case())
    assert payload["memory_at_start"] == {"area": {"value": "上海", "status": "known"}}
    assert payload["goal"]["call_number"] == 2
    assert payload["goal"]["business_context"] == {
        key: trace["business_context"][key]
        for key in ("app_name", "agent_role")
    }
    for hidden in ("不复制内部元数据", "上次已收到短信", "second-call", "不可提供的配置",
                   "tool_capabilities", "客户同意后可发送短信"):
        assert hidden not in json.dumps(payload, ensure_ascii=False)


def test_tool_instructions_and_call_control_stay_in_trace_without_losing_business_materials():
    trace = sample_trace()
    trace["business_context"] = {"app_name": "报名入口", "agent_role": "报名客服"}
    receipt = {"message_id": "sms-confirmed", "app_name": "报名入口", "delivery_status": "sent"}
    trace["turns"][1]["tool_events"].append({
        "seq": 12, "type": "tool_result", "data": {
            "name": "send_download_sms", "state": "success", "result": {
                "status": "success", "evidence": ["sms-confirmed"], "data": receipt,
            },
        },
    })
    expected = judge_payload(trace, sample_case())
    trace["business_context"]["tool_capabilities"] = [
        {"name": "end_call", "description": "必须调用结束工具，不能仅口头告别。"},
        {"name": "send_download_sms", "description": "发送成功后仍须等待客户反馈。"},
    ]
    trace["turns"][1]["tool_events"].append({
        "seq": 13, "type": "tool_result", "data": {
            "name": "end_call", "state": "success", "result": {
                "status": "success", "data": {"message": "祝您顺利，再见。"},
            },
        },
    })
    before = copy.deepcopy(trace)

    payload = judge_payload(trace, sample_case())

    assert payload == expected
    assert trace == before
    materials = payload["turns"][1]["materials"]
    assert materials[0]["text"] == "不收取报名费。装备费用需另行确认。"
    assert materials[0]["source"]["url"] == "https://example.org/policy"
    assert materials[1] == {
        "text": "message_id: sms-confirmed\napp_name: 报名入口\ndelivery_status: sent",
        "tool": "send_download_sms",
    }


def test_sources_stay_at_first_visible_turn_and_different_scope_stays_distinct():
    trace = sample_trace()
    trace["turns"][1]["tool_events"] = []
    later = policy_event(seq=11)
    later["data"]["result"]["data"]["clauses"][0]["region"] = "杭州"
    trace["turns"].extend([
        {"turn": 2, "user_message": "请核对资料", "reply": "查到了。", "tool_events": [policy_event()]},
        {"turn": 3, "user_message": "再查一下", "reply": "另一地区的资料也查到了。",
         "tool_events": [policy_event(seq=12), later]},
    ])
    turns = judge_payload(trace, sample_case())["turns"]
    assert "materials" not in turns[1]
    assert len(turns[2]["materials"]) == len(turns[3]["materials"]) == 1
    assert turns[3]["materials"][0]["source"]["region"] == "杭州"


def test_policy_scope_survives_projection_without_internal_index_fields():
    trace = sample_trace()
    clause = trace["turns"][1]["tool_events"][0]["data"]["result"]["data"]["clauses"][0]
    clause.update(
        title="杭州2023年驾驶员条件", region="杭州", section="驾驶员准入",
        partial_sentence=True, published_at="2023-07-17", valid_from="2023-07-17",
        valid_until="2028-07-16", document_type="local_rule", business_type="网约车",
        document_id="internal-document", content_hash="internal-hash", fetched_at="internal-time",
    )
    material = judge_payload(trace, sample_case())["turns"][1]["materials"][0]
    assert material["text"] == clause["text"]
    for key in ("title", "region", "section", "partial_sentence", "published_at",
                "valid_from", "valid_until", "document_type", "business_type"):
        assert material["source"][key] == clause[key]
    for removed in ("scope_note", "document_id", "content_hash", "fetched_at"):
        assert removed not in material["source"]


def test_business_material_preserves_product_and_location_limits_seen_by_agent():
    trace = sample_trace()
    trace["turns"][1]["tool_events"] = [{
        "seq": 15, "type": "tool_result", "data": {
            "name": "query_product_options", "state": "success", "result": {
                "status": "success", "data": {
                    "requested_product_id": "customer-product",
                    "effective_product_id": "generic-product",
                    "requested_location": "上海浦东", "matched_location": "上海",
                    "requested_filters": {"purpose": "先了解费用"},
                    "assumed_filters": [{"purpose": "通用条件"}],
                    "unverified_conditions": ["尚未核验账户资格，不代表客户已符合要求"],
                    "items": [{"id": "generic-product", "annual_rate_percent": 12}],
                    "unrelated_internal_value": "不得复制整个结果对象",
                },
            },
        },
    }]
    materials = judge_payload(trace, sample_case())["turns"][1]["materials"]
    text = "\n".join(material["text"] for material in materials)
    for required in ("requested_product_id: customer-product", "effective_product_id: generic-product",
                     "requested_location: 上海浦东", "matched_location: 上海", "通用条件",
                     "尚未核验账户资格，不代表客户已符合要求", "annual_rate_percent: 12"):
        assert required in text
    assert "不得复制整个结果对象" not in text


def test_financial_sms_receipt_is_beside_delivered_reply_and_intermediate_text_is_excluded():
    trace = {"scenario_id": "financial_consultation", "turns": [{
        "turn": 3, "user_message": "短信可以发，但注册要先交申请费吗？",
        "reply": "短信已经发出去了，您留意一下手机。",
        "events": [{"type": "assistant_text", "data": {"text": "注册不收申请费。"}}],
        "tool_events": [{
            "seq": 319, "type": "tool_result", "data": {
                "name": "send_download_sms", "state": "success", "result": {
                    "status": "success", "evidence": ["sms-success"],
                    "data": {"message_id": "sms-success", "app_name": "度小满 App",
                             "delivery_status": "sent"},
                },
            },
        }],
    }]}
    payload = judge_payload(trace, sample_case())
    turn = payload["turns"][0]
    assert turn["agent"] == trace["turns"][0]["reply"]
    assert turn["materials"][0]["tool"] == "send_download_sms"
    assert "delivery_status: sent" in turn["materials"][0]["text"]
    assert "注册不收申请费" not in json.dumps(payload, ensure_ascii=False)


@pytest.mark.parametrize("mutation", ["failed", "wrong_scenario", "unbound"])
def test_invalid_retrieval_is_not_supplied_as_policy_material(mutation):
    trace = sample_trace()
    data = trace["turns"][1]["tool_events"][0]["data"]
    if mutation == "failed":
        data["state"] = "failed"
    elif mutation == "wrong_scenario":
        data["result"]["data"]["clauses"][0]["scenario_id"] = "didi_driver"
    else:
        data["result"]["evidence"] = []
    materials = judge_payload(trace, sample_case())["turns"][1].get("materials", [])
    assert all("不收取报名费" not in material["text"] for material in materials)


@pytest.mark.parametrize("state,status", [("success", "denied"), ("failed", "unknown")])
def test_unsuccessful_tool_preserves_limits_without_promoting_leftover_success_data(state, status):
    trace = sample_trace()
    trace["turns"][1]["tool_events"] = [{
        "seq": 20, "type": "tool_result", "data": {
            "name": "estimate_financing_cost", "state": state,
            "result": {"status": status, "evidence": ["stale-receipt"], "data": {
                "reason": "amount_outside_range", "amount_range_yuan": {"min": 1000, "max": 50000},
                "message_id": "stale-receipt", "delivery_status": "sent",
                "calculation": {"interest": 100}, "items": [{"id": "stale-item"}],
            }},
        },
    }]
    material = judge_payload(trace, sample_case())["turns"][1]["materials"][0]
    assert "amount_range_yuan.max: 50000" in material["text"]
    assert f"status: {status}" in material["text"]
    for leftover in ("stale-receipt", "delivery_status", "interest", "stale-item"):
        assert leftover not in material["text"]
    assert not any(row["kind"] == "business_calculation" for row in evidence_for(trace).values())


@pytest.mark.parametrize("score", [1, 2, 3, 4, 5])
def test_every_integer_grade_is_valid(score):
    review = sample_review()
    for row in review.values():
        row["score"] = score
    assert validate_review(review, sample_trace()) == expected_review(review)


def test_short_keys_map_to_existing_dimensions_without_rewriting_the_model_result():
    review = sample_review()
    for index, row in enumerate(review.values(), start=1):
        row["score"] = index
        row["reason"] = f"第{index}项的独立理由"
    before = copy.deepcopy(review)
    result = validate_review(review, sample_trace())
    assert tuple(result) == QUALITY_DIMENSIONS
    assert result == {
        "goal_completion": {"score": 1, "reason": "第1项的独立理由"},
        "problem_resolution": {"score": 2, "reason": "第2项的独立理由"},
        "factual_reliability": {"score": 3, "reason": "第3项的独立理由"},
        "communication_quality": {"score": 4, "reason": "第4项的独立理由"},
    }
    assert review == before
    result["goal_completion"]["reason"] = "结果单独保存"
    assert review == before


@pytest.mark.parametrize("score", [True, False, 0, 6, 3.0, "3", []])
def test_non_integer_or_out_of_range_grades_are_rejected(score):
    review = sample_review()
    review["goal"]["score"] = score
    with pytest.raises(ValueError, match="1～5"):
        validate_review(review, sample_trace())


def test_not_applicable_and_unassessable_remain_distinct():
    review = sample_review()
    review["facts"].update(
        score=None, null_reason="not_applicable", reason="本通只有问候，没有业务事实",
    )
    review["answer"].update(
        score=None, null_reason="unassessable", reason="客户发言记录缺失，无法判断问题",
    )
    result = validate_review(review, sample_trace())
    assert result["factual_reliability"]["null_reason"] == "not_applicable"
    assert result["problem_resolution"]["null_reason"] == "unassessable"
    assert result["problem_resolution"]["score"] is None


@pytest.mark.parametrize("dimension", ["goal", "tone"])
def test_goal_and_communication_cannot_be_marked_not_applicable(dimension):
    review = sample_review()
    review[dimension].update(score=None, null_reason="not_applicable")
    with pytest.raises(ValueError, match="仅问题解决度"):
        validate_review(review, sample_trace())


@pytest.mark.parametrize("dimension,null_reason", [
    ("answer", "not_applicable"), ("facts", "not_applicable"),
    *((dimension, "unassessable") for dimension in JUDGE_RESPONSE_DIMENSIONS),
])
def test_null_scores_keep_the_model_decision_without_extra_output_fields(dimension, null_reason):
    review = sample_review()
    review[dimension].update(score=None, null_reason=null_reason)
    before = copy.deepcopy(review)
    assert validate_review(review, sample_trace()) == expected_review(review)
    assert review == before


@pytest.mark.parametrize("mutation", [
    "missing_dimension", "long_keys",
    "missing_score", "missing_reason", "empty_reason", "non_string_reason", "row_not_object",
    "null_without_reason", "invalid_null_reason",
])
def test_review_shape_is_explicit(mutation):
    review = sample_review()
    if mutation == "missing_dimension":
        review.pop("goal")
    elif mutation == "long_keys":
        review = expected_review(review)
    elif mutation == "missing_score":
        review["goal"].pop("score")
    elif mutation == "missing_reason":
        review["goal"].pop("reason")
    elif mutation == "empty_reason":
        review["goal"]["reason"] = " "
    elif mutation == "non_string_reason":
        review["goal"]["reason"] = 3
    elif mutation == "row_not_object":
        review["goal"] = 5
    elif mutation == "null_without_reason":
        review["goal"]["score"] = None
    elif mutation == "invalid_null_reason":
        review["goal"].update(score=None, null_reason="unknown")
    with pytest.raises((ValueError, TypeError)):
        validate_review(review, sample_trace())


def test_empty_trace_cannot_receive_a_quality_score():
    trace = {"turns": []}
    with pytest.raises(ValueError, match="没有实际回复"):
        judge_payload(trace, sample_case())
    with pytest.raises(ValueError, match="没有实际回复"):
        validate_review(sample_review(), trace)


def fake_client(monkeypatch, content, finish_reason="stop", error=None, reasoning_content=None,
                responses=None):
    import openai

    monkeypatch.setattr("evaluation.judges.review.JUDGE_RETRY_DELAY_SECONDS", 0)
    calls = {"requests": [], "created": 0, "closed": 0}

    class Client:
        def __init__(self, **kwargs):
            calls["created"] += 1
            calls["connection"] = kwargs
            calls["max_retries"] = kwargs["max_retries"]
            calls["timeout"] = kwargs["timeout"]
            self.chat = SimpleNamespace(completions=self)

        async def create(self, **kwargs):
            calls["requests"].append(kwargs)
            current = responses[len(calls["requests"]) - 1] if responses else {}
            current_error = current.get("error", error)
            if current_error:
                raise current_error
            usage = current.get("usage", {
                "prompt_tokens": 1000, "completion_tokens": 200,
                "completion_tokens_details": {"reasoning_tokens": 80},
            })
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    finish_reason=current.get("finish_reason", finish_reason),
                    message=SimpleNamespace(
                        content=current.get("content", content), reasoning_content=reasoning_content,
                    ),
                )],
                usage=SimpleNamespace(model_dump=lambda: usage) if usage is not None else None,
            )

        async def close(self):
            calls["closed"] += 1

    monkeypatch.setattr(openai, "AsyncOpenAI", Client)
    return calls


def settings():
    return SimpleNamespace(
        judge_model="deepseek-flash", judge_temperature=None, judge_max_tokens=16384,
        judge_extra_body={"thinking": {"type": "enabled"}}, judge_reasoning_effort="high",
        agent_api_key="private-key-marker", agent_base_url="https://example.org",
        judge_api_key="private-judge-key-marker", judge_base_url="https://judge.example.org",
    )


async def test_valid_review_uses_markdown_prompt_and_saves_actual_input_for_audit(monkeypatch):
    calls = fake_client(monkeypatch, json.dumps(sample_review(), ensure_ascii=False))
    diagnostic = {"local_diagnostic": "不进入评审模型"}
    result = await review_trace(settings(), sample_trace(), sample_case(), diagnostic)
    assert result == expected_review() and diagnostic["status"] == "valid"
    assert diagnostic["usage"]["prompt_tokens"] == 1000
    assert diagnostic["judge_version"] == JUDGE_VERSION
    assert calls["created"] == calls["closed"] == 1
    assert calls["max_retries"] == 0 and len(calls["requests"]) == 1
    messages = calls["requests"][0]["messages"]
    assert [message["role"] for message in messages] == ["system", "user"]
    assert messages[0]["content"] == JUDGE_PROMPT == JUDGE_PROMPT_PATH.read_text(encoding="utf-8")
    assert "# 评分标准" in JUDGE_PROMPT and "# 输出" in JUDGE_PROMPT
    assert json.loads(messages[1]["content"]) == diagnostic["input"] == judge_payload(sample_trace(), sample_case())
    assert "不进入评审模型" not in json.dumps(messages, ensure_ascii=False)


async def test_thinking_request_omits_temperature_and_saves_reasoning_only_in_diagnostics(monkeypatch):
    reasoning = "测试专用思考标记"
    calls = fake_client(monkeypatch, json.dumps(sample_review()), reasoning_content=reasoning)
    diagnostic = {}
    result = await review_trace(settings(), sample_trace(), sample_case(), diagnostic)
    request = calls["requests"][0]
    assert request["extra_body"] == {"thinking": {"type": "enabled"}}
    assert request["reasoning_effort"] == "high" and "temperature" not in request
    assert request["max_tokens"] == 16384 and calls["timeout"] == 180
    assert diagnostic["thinking"] is True and diagnostic["reasoning_effort"] == "high"
    assert diagnostic["temperature"] is None and diagnostic["temperature_used"] is False
    assert diagnostic["configured_temperature"] is None
    assert diagnostic["reasoning_content"] == reasoning
    assert diagnostic["usage"]["completion_tokens_details"]["reasoning_tokens"] == 80
    assert reasoning not in json.dumps(diagnostic["input"], ensure_ascii=False)
    assert reasoning not in json.dumps(result, ensure_ascii=False)
    assert reasoning not in json.dumps(request["messages"], ensure_ascii=False)


async def test_disabled_thinking_preserves_temperature_without_reasoning_effort(monkeypatch):
    calls = fake_client(monkeypatch, json.dumps(sample_review()))
    configured = settings()
    configured.judge_extra_body = {"thinking": {"type": "disabled"}}
    configured.judge_reasoning_effort = None
    configured.judge_temperature = 0.3
    diagnostic = {}
    await review_trace(configured, sample_trace(), sample_case(), diagnostic)
    request = calls["requests"][0]
    assert request["extra_body"] == {"thinking": {"type": "disabled"}}
    assert request["temperature"] == 0.3 and "reasoning_effort" not in request
    assert diagnostic["thinking"] is False and diagnostic["reasoning_effort"] is None
    assert diagnostic["temperature_used"] is True and diagnostic["temperature"] == 0.3


@pytest.mark.parametrize("model", ["gpt-4.1-mini", "qwen-plus"])
async def test_judge_can_use_an_independent_compatible_connection(monkeypatch, model):
    calls = fake_client(monkeypatch, json.dumps(sample_review()))
    configured = settings()
    configured.judge_model = model
    configured.judge_extra_body = {}
    configured.judge_reasoning_effort = None
    configured.judge_temperature = 0
    configured.judge_api_key = "independent-judge-secret"
    configured.judge_base_url = "https://judge.example/v1"
    diagnostic = {}

    result = await review_trace(configured, sample_trace(), sample_case(), diagnostic)

    assert result == expected_review()
    assert calls["connection"]["api_key"] == configured.judge_api_key
    assert calls["connection"]["base_url"] == configured.judge_base_url
    request = calls["requests"][0]
    assert request["model"] == model
    assert "thinking" not in request.get("extra_body", {})
    assert "reasoning_effort" not in request
    assert configured.agent_api_key not in json.dumps(diagnostic)
    assert configured.judge_api_key not in json.dumps(diagnostic)


async def test_judge_standard_reasoning_uses_its_own_token_and_temperature_configuration(monkeypatch):
    calls = fake_client(monkeypatch, json.dumps(sample_review()))
    configured = settings()
    configured.judge_model = "gpt-5-mini"
    configured.judge_extra_body = {}
    configured.judge_max_tokens_param = "max_completion_tokens"

    await review_trace(configured, sample_trace(), sample_case())

    request = calls["requests"][0]
    assert request["reasoning_effort"] == "high"
    assert request["max_completion_tokens"] == configured.judge_max_tokens
    assert "max_tokens" not in request
    assert "temperature" not in request
    assert "extra_body" not in request


async def test_judge_inherits_current_agent_connection_when_role_values_are_empty(monkeypatch):
    calls = fake_client(monkeypatch, json.dumps(sample_review()))
    configured = settings()
    configured.judge_api_key = configured.judge_base_url = configured.judge_model = ""
    configured.agent_model = "inherited-agent-model"
    diagnostic = {}

    await review_trace(configured, sample_trace(), sample_case(), diagnostic)

    assert calls["connection"]["api_key"] == configured.agent_api_key
    assert calls["connection"]["base_url"] == configured.agent_base_url
    assert calls["requests"][0]["model"] == configured.agent_model
    assert diagnostic["model"] == configured.agent_model


async def test_short_response_keeps_raw_model_output_in_diagnostics(monkeypatch):
    trace, review = sample_trace(), sample_review()
    review["facts"].update(score=None, null_reason="unassessable", reason="资料不足，无法判断")
    content = json.dumps(review, ensure_ascii=False)
    calls = fake_client(monkeypatch, content)
    diagnostic = {}
    result = await review_trace(settings(), trace, sample_case(), diagnostic)
    assert result == expected_review(review) and diagnostic["content"] == content
    assert diagnostic["status"] == "valid" and len(calls["requests"]) == 1
    assert json.loads(diagnostic["content"]) == review
    assert "facts" in review and "factual_reliability" in result


@pytest.mark.parametrize("failure,stage", [
    ("score", "validation"), ("shape", "validation"), ("json", "validation"),
    ("truncated", "completion"), ("empty", "completion"), ("network", "request"),
])
async def test_errors_retry_with_a_bound_and_keep_every_attempt_without_fabricated_score(monkeypatch, failure, stage):
    review = sample_review()
    if failure == "score":
        review["facts"]["score"] = 6
    content = json.dumps(review, ensure_ascii=False)
    if failure == "shape":
        content = "[]"
    elif failure == "json":
        content = "{"
    elif failure == "empty":
        content = ""
    calls = fake_client(
        monkeypatch, content, "length" if failure == "truncated" else "stop",
        TimeoutError("private-provider-marker") if failure == "network" else None,
    )
    diagnostic = {}
    with pytest.raises((ValueError, TimeoutError)):
        await review_trace(settings(), sample_trace(), sample_case(), diagnostic)
    assert diagnostic["status"] == "error" and diagnostic["error_stage"] == stage
    assert len(calls["requests"]) == 3 and calls["closed"] == 1
    assert diagnostic["attempt_count"] == 3 and diagnostic["retry_count"] == 2
    assert len(diagnostic["attempts"]) == 3
    assert all(row["status"] == "error" for row in diagnostic["attempts"])
    assert "score" not in diagnostic and diagnostic["input"] == judge_payload(sample_trace(), sample_case())
    assert "private-provider-marker" not in json.dumps(diagnostic)
    assert "private-key-marker" not in json.dumps(diagnostic)
    if failure != "network":
        assert diagnostic["content"] == content and diagnostic["usage"]["completion_tokens"] == 600
        assert all(row["content"] == content for row in diagnostic["attempts"])


async def test_empty_agent_reply_is_an_input_error_before_model_request(monkeypatch):
    calls = fake_client(monkeypatch, "")
    trace, diagnostic = sample_trace(), {}
    trace["turns"][0]["reply"] = " "
    with pytest.raises(ValueError, match="实际回复为空"):
        await review_trace(settings(), trace, sample_case(), diagnostic)
    assert calls["created"] == 0 and not calls["requests"]
    assert diagnostic["error_stage"] == "input"


def test_extra_fields_are_projected_out_without_rewriting_required_values():
    review = sample_review()
    expected = expected_review(copy.deepcopy(review))
    review.update(type="json_object", overall=5)
    review["goal"].update(verdict="pass", evidence=[], null_reason="unassessable")
    before = copy.deepcopy(review)

    assert validate_review(review, sample_trace()) == expected
    assert review == before


@pytest.mark.parametrize("wrapper", ["fence", "single_quotes", "trailing_comma", "missing_comma"])
async def test_json_repair_salvages_syntax_without_another_model_request(monkeypatch, wrapper):
    review = sample_review()
    review["goal"]["score"] = 1
    content = json.dumps(review, ensure_ascii=False)
    if wrapper == "fence":
        content = f"结果如下：\n```json\n{content}\n```"
    elif wrapper == "single_quotes":
        content = repr(review)
    elif wrapper == "trailing_comma":
        content = content[:-1] + ",}"
    else:
        content = content.replace('}, "answer"', '} "answer"')
    calls = fake_client(monkeypatch, content)
    diagnostic = {}

    result = await review_trace(settings(), sample_trace(), sample_case(), diagnostic)

    assert result == expected_review(review)
    assert len(calls["requests"]) == 1
    assert diagnostic["content"] == content
    assert diagnostic["parsing_method"] == "json_repair"
    assert diagnostic["repaired_output"] == review
    assert isinstance(diagnostic["repair_log"], list)
    assert diagnostic["attempts"][0]["repaired_output"] == review
    assert diagnostic["usage_complete"] is True


async def test_valid_json_uses_standard_parser_and_records_ignored_fields(monkeypatch):
    review = sample_review()
    expected = expected_review(copy.deepcopy(review))
    review["type"] = "json_object"
    review["facts"]["evidence"] = ["解释费用的原文"]
    content = json.dumps(review)
    calls = fake_client(monkeypatch, content)
    diagnostic = {}

    def unexpected_repair(*args, **kwargs):
        pytest.fail("合法 JSON 不应进入修复器")

    monkeypatch.setattr("evaluation.judges.review.repair_json", unexpected_repair)
    assert await review_trace(settings(), sample_trace(), sample_case(), diagnostic) == expected
    assert len(calls["requests"]) == 1
    assert diagnostic["parsing_method"] == "json"
    assert diagnostic["ignored_fields"] == ["type", "facts.evidence"]
    assert "repaired_output" not in diagnostic
    assert diagnostic["content"] == content


@pytest.mark.parametrize("broken", [
    '"score": "5"', '"score": true', '"score": 5.0', '"score": 6', '"score":',
    '"missing_score": 5',
])
async def test_repair_never_invents_or_coerces_a_score(monkeypatch, broken):
    content = json.dumps(sample_review()).replace('"score": 5', broken, 1)
    content = content[:-1] + ",}"
    calls = fake_client(monkeypatch, content)
    diagnostic = {}

    with pytest.raises(ValueError):
        await review_trace(settings(), sample_trace(), sample_case(), diagnostic)

    assert len(calls["requests"]) == 3
    assert diagnostic["status"] == "error"
    assert diagnostic["error_stage"] == "validation"
    assert diagnostic["parsing_method"] == "json_repair"
    assert all(row["content"] == content for row in diagnostic["attempts"])


@pytest.mark.parametrize("broken", ['"reason": ""', '"reason":', '"missing_reason": "解释"'])
async def test_repair_never_invents_a_missing_reason(monkeypatch, broken):
    content = json.dumps(sample_review(), ensure_ascii=False)
    content = content.replace('"reason": "回答区分了两类费用，并明确未确认项"', broken, 1)
    content = content[:-1] + ",}"
    fake_client(monkeypatch, content)
    diagnostic = {}

    with pytest.raises(ValueError):
        await review_trace(settings(), sample_trace(), sample_case(), diagnostic)

    assert diagnostic["status"] == "error" and diagnostic["attempt_count"] == 3


async def test_retry_recovers_with_all_attempts_and_usage_preserved(monkeypatch):
    valid = sample_review()
    valid["goal"]["score"] = 1
    invalid = copy.deepcopy(valid)
    invalid["facts"]["score"] = 7
    broken, good = json.dumps(invalid), json.dumps(valid)
    calls = fake_client(monkeypatch, good, responses=[
        {"content": broken}, {"content": good},
    ])
    diagnostic = {}

    assert await review_trace(settings(), sample_trace(), sample_case(), diagnostic) == expected_review(valid)
    assert calls["requests"][0] == calls["requests"][1]
    assert diagnostic["status"] == "valid" and "error_stage" not in diagnostic
    assert diagnostic["attempt_count"] == 2 and diagnostic["retry_count"] == 1
    assert [row["status"] for row in diagnostic["attempts"]] == ["error", "valid"]
    assert [row["content"] for row in diagnostic["attempts"]] == [broken, good]
    assert diagnostic["usage"] == {
        "prompt_tokens": 2000, "completion_tokens": 400,
        "completion_tokens_details": {"reasoning_tokens": 160},
    }
    assert diagnostic["usage_complete"] is True


async def test_network_retry_recovers_without_claiming_missing_usage_is_complete(monkeypatch):
    content = json.dumps(sample_review())
    calls = fake_client(monkeypatch, content, responses=[
        {"error": TimeoutError("private-provider-marker")}, {"content": content},
    ])
    diagnostic = {}

    assert await review_trace(settings(), sample_trace(), sample_case(), diagnostic) == expected_review()
    assert len(calls["requests"]) == 2
    assert diagnostic["usage"]["prompt_tokens"] == 1000
    assert diagnostic["usage_complete"] is False
    assert diagnostic["attempts"][0]["error_category"] == "TimeoutError"
    assert "private-provider-marker" not in json.dumps(diagnostic)


@pytest.mark.parametrize("status_code,retry_count", [(400, 0), (401, 0), (429, 2), (503, 2)])
async def test_service_errors_only_retry_transient_status_codes(monkeypatch, status_code, retry_count):
    import httpx
    import openai

    error = openai.APIStatusError(
        "private-provider-marker",
        response=httpx.Response(status_code, request=httpx.Request("POST", "https://example.org")),
        body=None,
    )
    calls = fake_client(monkeypatch, "", error=error)
    diagnostic = {}

    with pytest.raises(openai.APIStatusError):
        await review_trace(settings(), sample_trace(), sample_case(), diagnostic)

    assert len(calls["requests"]) == retry_count + 1
    assert diagnostic["retry_count"] == retry_count
    assert diagnostic["retryable"] is bool(retry_count)
    assert "private-provider-marker" not in json.dumps(diagnostic)


async def test_cancelled_request_is_saved_and_not_retried(monkeypatch):
    import asyncio

    calls = fake_client(monkeypatch, "", error=asyncio.CancelledError())
    diagnostic = {}

    with pytest.raises(asyncio.CancelledError):
        await review_trace(settings(), sample_trace(), sample_case(), diagnostic)

    assert len(calls["requests"]) == calls["closed"] == 1
    assert diagnostic["status"] == "interrupted"
    assert diagnostic["attempts"][0]["status"] == "interrupted"


@pytest.mark.parametrize("score", ["5 - 2", "1 5", "5 ignored"])
async def test_repair_cannot_discard_part_of_the_original_score_expression(monkeypatch, score):
    content = json.dumps(sample_review()).replace('"score": 5', f'"score": {score}', 1)
    calls = fake_client(monkeypatch, content)
    diagnostic = {}

    with pytest.raises(ValueError, match="原始评分包含多余表达式"):
        await review_trace(settings(), sample_trace(), sample_case(), diagnostic)

    assert len(calls["requests"]) == 3
    assert diagnostic["error_stage"] == "validation"
    assert diagnostic["repaired_output"]["goal"]["score"] in {1, 5}
    assert all(row["status"] == "error" for row in diagnostic["attempts"])


async def test_missing_comma_after_a_score_and_score_mentions_in_reasons_remain_repairable(monkeypatch):
    review = sample_review()
    review["goal"]["reason"] = '理由中的 "score": 5 - 2 只是引用示例'
    content = json.dumps(review, ensure_ascii=False).replace('"score": 5,', '"score": 5', 1)
    calls = fake_client(monkeypatch, content)
    diagnostic = {}

    assert await review_trace(settings(), sample_trace(), sample_case(), diagnostic) == expected_review(review)
    assert len(calls["requests"]) == 1 and diagnostic["parsing_method"] == "json_repair"


async def test_cancellation_during_backoff_marks_the_run_interrupted_and_keeps_failure(monkeypatch):
    import asyncio

    calls = fake_client(monkeypatch, "[]")
    diagnostic = {}

    async def cancel_wait(delay):
        raise asyncio.CancelledError()

    monkeypatch.setattr("evaluation.judges.review.asyncio.sleep", cancel_wait)
    with pytest.raises(asyncio.CancelledError):
        await review_trace(settings(), sample_trace(), sample_case(), diagnostic)

    assert len(calls["requests"]) == calls["closed"] == 1
    assert diagnostic["status"] == "interrupted"
    assert diagnostic["error_stage"] == "retry_wait"
    assert diagnostic["attempts"][0]["status"] == "error"
    assert diagnostic["attempts"][0]["content"] == "[]"
    assert diagnostic["usage"]["completion_tokens"] == 200
