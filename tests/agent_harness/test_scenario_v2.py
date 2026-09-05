"""验证场景加载、资源冻结和仓库中维护的场景配置。"""

import pytest
import yaml

from backend.agent_harness.scenario import load_scenario
from backend.agent_harness.tools.registry import TOOL_FUNCTIONS
from tests.paths import ROOT


@pytest.fixture
def conversation_config(tmp_path):
    (tmp_path / "catalog.json").write_text('{"stations": []}', encoding="utf-8")
    (tmp_path / "process.md").write_text("# 引导下载和注册", encoding="utf-8")
    config = {
        "schema_version": "2.0",
        "scenario": {
            "id": "meituan_rider",
            "version": "6.0",
            "title": "骑手用增",
            "industry": "配送",
            "brand": "美团",
            "language": "zh-CN",
            "mode": "conversation",
        },
        "identity": {
            "role": "骑手助手",
            "background": "外呼用增服务",
            "opening": "您好",
            "tone": "简洁",
            "disclosure": "业务信息来自本地版本化目录",
        },
        "objective": {
            "description": "解答顾虑并引导下载注册",
            "stages": ["了解需求", "解答顾虑", "引导下载注册"],
            "completion_basis": "customer_statement",
        },
        "customer_fields": [{"id": "city", "type": "string", "description": "客户城市"}],
        "tools": [
            {"id": "search_policy", "adapter": "local"},
        ],
        "skills": [
            {
                "id": "process",
                "ref": "process.md",
                "version": "1",
                "description": "下载注册指导",
                "required_tools": ["search_policy"],
            }
        ],
        "policies": {
            "prohibited_promises": ["保证收入"],
            "tool_timeout_seconds": 10,
            "session_timeout_seconds": 300,
        },
        "business_data": {"ref": "catalog.json", "source_type": "local_catalog"},
    }
    return tmp_path, config


def load(config):
    root, document = config
    path = root / "scenario.yaml"
    path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    return load_scenario(path)


def test_conversation_loads_without_legacy_state_or_evaluation(conversation_config):
    scenario = load(conversation_config)
    assert scenario.id == "meituan_rider"
    assert "actions" not in scenario.data
    assert scenario["objective"]["completion_basis"] == "customer_statement"
    assert set(scenario.fields) == {"city"}
    assert {"facts", "goals", "external_state", "guidance"}.isdisjoint(scenario.data)


def test_catalog_content_is_frozen_and_changes_snapshot_hash(conversation_config):
    root, _ = conversation_config
    first = load(conversation_config)
    (root / "catalog.json").write_text('{"stations": [{"id": "beijing"}]}')
    second = load(conversation_config)
    assert first.resources["catalog.json"] == '{"stations": []}'
    assert second.snapshot_hash != first.snapshot_hash


def test_agent_view_keeps_source_disclosure_but_hides_resource_paths(conversation_config):
    root, data = conversation_config
    (root / "samples.json").write_text("[]")
    data["evaluation"] = {"samples_ref": "samples.json"}
    scenario = load(conversation_config)
    view = scenario.agent_view()
    assert view["business_data"] == {"source_type": "local_catalog"}
    assert "ref" not in view["skills"][0]
    assert "evaluation" not in view
    assert scenario["business_data"]["ref"] == "catalog.json"
    assert scenario.resources["samples.json"] == "[]"


def test_empty_evaluation_is_valid(conversation_config):
    conversation_config[1]["evaluation"] = {}
    assert load(conversation_config)


def test_skill_cannot_require_a_tool_absent_from_its_scenario(conversation_config):
    conversation_config[1]["skills"][0]["required_tools"].append("estimate_work_budget")
    with pytest.raises(ValueError, match="缺少场景工具.*estimate_work_budget"):
        load(conversation_config)


@pytest.mark.parametrize("collection", ["tools", "skills"])
def test_scenario_rejects_duplicate_capability_ids(conversation_config, collection):
    conversation_config[1][collection].append(conversation_config[1][collection][0].copy())
    with pytest.raises(ValueError, match="不能重复"):
        load(conversation_config)


@pytest.mark.parametrize(
    "scenario_id", ["didi_driver", "meituan_rider", "meituan_merchant", "financial_consultation"]
)
def test_checked_in_scenarios_have_consistent_references(scenario_id):
    scenario = load_scenario(ROOT / "backend/scenarios" / scenario_id / "scenario.yaml")
    assert scenario.id == scenario_id
    assert scenario.version
    assert scenario["identity"]["opening"]
    assert scenario["objective"]["description"]
    tool_ids = {tool["id"] for tool in scenario["tools"]}
    assert tool_ids <= TOOL_FUNCTIONS.keys()
    assert len(tool_ids) == len(scenario["tools"])
    assert len(scenario.fields) == len(scenario["customer_fields"])
    for tool in scenario["tools"]:
        assert set(tool.get("fields", [])) <= scenario.fields.keys()
    for skill in scenario["skills"]:
        assert set(skill["required_tools"]) <= tool_ids
        assert scenario.resources[skill["ref"]]
    assert scenario.resources[scenario["business_data"]["ref"]]
