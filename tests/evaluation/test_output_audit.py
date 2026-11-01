"""内部标签留在审计中；业务事实保持可用，无需套用固定免责声明。"""

import copy
import json
from types import SimpleNamespace

import pytest

from evaluation.checks.business_coverage import assess_visible
from evaluation.checks.output_audit import (
    INTERNAL_LANGUAGE_FRAGMENTS,
    audit_text,
    audit_trace,
    audit_visible,
)
from evaluation.judges.evidence import BUSINESS_TOOLS, evidence_for, judge_payload
from evaluation.judges.review import review_trace
from evaluation.judges.schema import JUDGE_RESPONSE_DIMENSIONS
from tests.evaluation.test_dialogue import example_case, example_review, example_trace


@pytest.mark.parametrize("text", [
    *[f"内部{word}环境。" for word in INTERNAL_LANGUAGE_FRAGMENTS],
    "我给您一个虚拟电话。", "站点为虚构站点。", "结果来自 generic_fallback。",
    "请记住 catalog_generic_financing。", "来源是 local_catalog。",
])
def test_exact_internal_language_is_detected_in_final_reply(text):
    assert audit_text(text)


@pytest.mark.parametrize("text", [
    INTERNAL_LANGUAGE_FRAGMENTS[0] + "培训商户可以咨询入驻条件。",
    INTERNAL_LANGUAGE_FRAGMENTS[1] + "还款有助于了解费用构成。",
    "请在账户余额页面核对收款信息。", "可以先测算还款，了解费用构成。", "电话核验以页面提示为准。",
    "按年利率12%、三个月单利计算，利息300元。", "地址和联系电话暂未提供，不能确认距离。",
    "站点联系电话13800123221，地址见刚才查询结果。", "是否能通过审核，需要以平台实际结果为准。",
])
def test_legitimate_business_concepts_and_cost_assumptions_are_allowed(text):
    assert not audit_text(text)


def test_opaque_handles_are_allowed_only_in_structured_id_fields():
    value = {"data": {"items": [{"id": "catalog_station_1", "name": "长沙服务站"}],
                       "effective_product_id": "catalog_flexible_loan"}}
    assert not audit_visible(value, business_result=True)
    value["data"]["items"][0]["name"] = "catalog_station_1 是站点名称"
    assert audit_visible(value, business_result=True)
    assert audit_text("您的方案是 catalog_flexible_loan。")


def test_nested_internal_fields_fail_but_policy_source_metadata_is_not_forbidden():
    result = {"data": {"items": [{"name": "服务点", "pricing_basis": {"profile_id": "city_a"}}]}}
    assert {row["rule"] for row in audit_visible(result, business_result=True)} >= {"internal_field"}
    policy = {"data": {"clauses": [{"text": "账户余额以页面说明为准。", "source_type": "official"}]}}
    assert not audit_visible(policy)


def test_visible_projection_must_preserve_money_contacts_conditions_and_counts():
    raw = {"status": "success", "data": {"items": [{
        "id": "rider_station_1", "name": "服务站", "address": "北辰路18号", "phone": "13800123221",
        "monthly_rent_yuan": 600, "deposit_yuan": 1000, "conditions": ["至少租一个月"],
    }]}}
    assert all(row["passed"] for row in assess_visible(raw, raw))
    visible = copy.deepcopy(raw)
    visible["data"]["items"][0].pop("deposit_yuan")
    assert not all(row["passed"] for row in assess_visible(raw, visible))
    visible = copy.deepcopy(raw)
    visible["data"]["items"][0]["phone"] = "13847438231"
    assert not all(row["passed"] for row in assess_visible(raw, visible))


def hidden_audit_trace():
    return {"scenario_id": "meituan_rider", "case_id": "visible-evidence", "turns": [{
        "turn": 1, "user_message": "租车押金多少？", "reply": "押金1000元，至少租一个月。",
        "tool_events": [{"seq": 3, "type": "tool_result", "data": {
            "name": "search_vehicle_rentals", "state": "success", "result": {
                "status": "success", "data": {"items": [{"id": "bike_1", "deposit_yuan": 1000,
                    "conditions": ["至少租一个月"]}]},
            },
        }}],
        "business_audit_events": [{"seq": 2, "type": "business_query_audit", "data": {
            "name": "search_vehicle_rentals", "result": {"status": "success", "data": {
                "source_type": "local_catalog", "items": [{"id": "bike_1", "deposit_yuan": 1000,
                    "hidden_note": "隐藏诱饵：免费换电和十万元保底收入。"}],
            }},
        }}],
    }]}


def test_hidden_audit_never_supplies_claim_text_or_enters_judge_input():
    trace = hidden_audit_trace()
    evidence = evidence_for(trace)
    assert evidence["source/3/0"]["kind"] == "business_result"
    assert "1000" in evidence["source/3/0"]["text"]
    assert "免费换电" not in json.dumps(evidence, ensure_ascii=False)
    payload = judge_payload(trace, example_case())
    assert "business_audit_events" not in json.dumps(payload)
    assert "免费换电" not in json.dumps(payload, ensure_ascii=False)
    assert trace["turns"][0]["business_audit_events"]
    assert audit_trace(trace, BUSINESS_TOOLS)["passed"]


def test_output_audit_flags_leaking_reply_without_adding_quality_metrics():
    trace = example_trace()
    trace["turns"][0]["reply"] += INTERNAL_LANGUAGE_FRAGMENTS[0] + "数据"
    assert not audit_trace(trace, BUSINESS_TOOLS)["passed"]


async def test_fake_judge_request_never_receives_raw_audit(monkeypatch):
    import openai

    trace = example_trace()
    trace["turns"][0]["business_audit_events"] = hidden_audit_trace()["turns"][0]["business_audit_events"]
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=self)

        async def create(self, **kwargs):
            captured.update(kwargs)
            review = example_review(trace)
            response = {key: review[dimension] for key, dimension in JUDGE_RESPONSE_DIMENSIONS.items()}
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                content=json.dumps(response, ensure_ascii=False)))])

        async def close(self):
            pass

    monkeypatch.setattr(openai, "AsyncOpenAI", FakeClient)
    settings = SimpleNamespace(judge_api_key="unused", judge_base_url="https://unused.example",
                               judge_model="unused", judge_temperature=0, judge_max_tokens=1000,
                               judge_reasoning_effort=None, judge_extra_body={})
    result = await review_trace(settings, trace, example_case())
    assert result["goal_completion"]["score"] == 5
    sent = json.loads(captured["messages"][1]["content"])
    assert "business_audit_events" not in json.dumps(sent)
    assert "免费换电" not in json.dumps(sent, ensure_ascii=False)


def test_verbatim_customer_inputs_and_official_text_are_not_internal_copy():
    result = {"data": {"items": [{"id": "offer_1", "name": "培训门店服务"}],
                       "requested_filters": {"store_category": INTERNAL_LANGUAGE_FRAGMENTS[0] + "设备销售"}}}
    assert not audit_visible(result, business_result=True)
    assert not audit_text("您经营培训商户，可以先了解门店入驻条件。")
    trace = {"turns": [{"turn": 1, "reply": "可以先了解门店入驻条件。", "tool_events": [
        {"seq": 1, "type": "tool_result", "data": {"name": "search_policy", "result": {
            "data": {"clauses": [{"text": "账户余额与个人信息应按业务用途管理。"}]}}}},
        {"seq": 2, "type": "tool_result", "data": {"name": "read_memory", "result": {
            "data": {"quote": "我经营培训设备"}}}},
    ]}]}
    assert audit_trace(trace, BUSINESS_TOOLS)["passed"]
