"""验证系统提示的当前业务身份、客户资料边界与占位符替换。"""

import copy
import json
import re
from string import Template

import pytest

from backend.agent_harness import prompts
from backend.agent_harness.scenario import load_scenario
from backend.channels.sms import APP_DOWNLOADS
from tests.paths import ROOT

SCENARIO_IDS = ["meituan_rider", "meituan_merchant", "didi_driver", "financial_consultation"]


def customer_block(rendered):
    """读取模型实际收到的唯一 JSON 块，确认其中仅包含客户资料。"""
    blocks = re.findall(r"```json[ \t]*\n(.*?)\n```", rendered, flags=re.DOTALL)
    assert len(blocks) == 1, "系统提示只需一个当前客户资料的 JSON 块"
    return json.loads(blocks[0])


@pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
def test_template_injects_only_the_current_business_identity_and_customer(scenario_id):
    scenarios = {
        name: load_scenario(ROOT / "backend/scenarios" / name / "scenario.yaml")
        for name in SCENARIO_IDS
    }
    scenario = scenarios[scenario_id]
    identity = scenario["identity"]
    customer = {"profile": {"concern": {"value": "了解使用步骤"}}}

    rendered = prompts.system_prompt(scenario, customer)

    assert identity["role"] in rendered
    assert identity["background"] in rendered
    assert identity["app_name"] in rendered
    assert identity["app_name"] == APP_DOWNLOADS[scenario_id][0]
    assert customer_block(rendered) == customer
    for other_id, other in scenarios.items():
        if other_id != scenario_id:
            assert other["identity"]["role"] not in rendered
            assert other["identity"]["background"] not in rendered
            assert other["identity"]["app_name"] not in rendered
    for item in scenario["tools"] + scenario["skills"]:
        assert item["id"] not in rendered


def test_template_has_only_business_placeholders_without_skill_authoring_instructions():
    assert set(Template(prompts.SYSTEM_PROMPT).get_identifiers()) == {
        "role",
        "app_name",
        "customer_background",
        "customer_context",
    }
    assert "<!--" not in prompts.SYSTEM_PROMPT
    assert "skill" not in prompts.SYSTEM_PROMPT.lower()


def test_context_projection_hides_internal_data_without_mutating_inputs():
    scenario = {
        "identity": {
            "role": "业务增长客服",
            "app_name": "业务 App",
            "background": "客户希望了解业务办理流程。",
            "workspace_id": "hidden-scenario-workspace",
        },
        "scenario": {"id": "hidden-scenario-id", "version": "hidden-scenario-version"},
        "tools": [{"id": "hidden-tool-id"}],
        "skills": [{"id": "hidden-skill-id", "ref": "hidden-skill-path"}],
        "resources": {"private.md": "hidden-resource-body"},
        "evaluation": {"hidden_concerns": ["hidden-evaluation-detail"]},
        "business_data": {"ref": "hidden-catalog.json"},
    }
    customer = {
        "customer_id": "customer-1",
        "contact_source": "hidden-contact-source",
        "history": [{"concern": "了解费用", "workspace_id": "hidden-customer-workspace"}],
        "reference": {
            "data": {
                "source_type": "local_catalog",
                "profile_id": "hidden-catalog-profile",
                "amount": 100,
                "eligibility_filters": {"city": "__generic__"},
            }
        },
        "policy": {"text": "政策正文", "source": "公开政策来源"},
    }
    original_scenario = copy.deepcopy(scenario)
    original_customer = copy.deepcopy(customer)

    rendered = prompts.system_prompt(scenario, customer)
    customer_context = customer_block(rendered)

    assert customer_context == {
        "customer_id": "customer-1",
        "history": [{"concern": "了解费用"}],
        "reference": {"data": {"amount": 100, "eligibility_filters": {"city": "通用条件"}}},
        "policy": customer["policy"],
    }
    assert "hidden-" not in rendered
    assert scenario == original_scenario
    assert customer == original_customer


def test_context_values_preserve_placeholder_text_and_json_characters(monkeypatch):
    text = (
        "金额 $100，变量 ${role} / ${app_name} / ${customer_background} / "
        "${customer_context} / $unknown / $$，"
        'JSON {"city": "北京"}，路径 C:\\资料\n下一行仍是客户原话。'
    )
    identity = {"role": "角色 " + text, "app_name": "应用 " + text, "background": "背景 " + text}
    customer = {"concern": text, "history": [{"message": text}]}
    monkeypatch.setattr(
        prompts,
        "SYSTEM_PROMPT",
        "${role}\n${app_name}\n${customer_background}\n${customer_context}",
    )

    rendered = prompts.system_prompt({"identity": identity}, customer)

    assert rendered == "\n".join(
        [
            identity["role"],
            identity["app_name"],
            identity["background"],
            json.dumps(customer, ensure_ascii=False, indent=2),
        ]
    )


def test_unknown_template_variable_fails_instead_of_reaching_the_model(monkeypatch):
    monkeypatch.setattr(prompts, "SYSTEM_PROMPT", prompts.SYSTEM_PROMPT + "\n${unknown_context}")
    scenario = {
        "identity": {"role": "业务增长客服", "app_name": "业务 App", "background": "潜在客户"}
    }

    with pytest.raises(KeyError, match="unknown_context"):
        prompts.system_prompt(scenario, {})
