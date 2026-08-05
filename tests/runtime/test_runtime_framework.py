"""针对固定版本的真实框架执行契约测试，不向服务供应商发送请求。"""

import threading

import pytest

from backend.agent_harness.agent import create_toolkit
from backend.agent_harness.scenario import Scenario


class ToolFixture:
    def __init__(self, root):
        from backend.agent_harness.tools import ToolContext, build_tools

        body = "---\nname: guide\ndescription: 流程指导\n---\n正文专用随机码：业务七号"
        (root / "SKILL.md").write_text(body)
        self.scenario = Scenario(
            root=root,
            snapshot_hash="fixture-snapshot",
            resources={"SKILL.md": body},
            data={
                "scenario": {"id": "meituan_rider", "version": "test"},
                "policies": {"tool_timeout_seconds": 10},
                "tools": [{"id": name} for name in ("search_policy",)],
                "skills": [{"id": "guide", "description": "流程指导", "ref": "SKILL.md"}],
            },
        )
        self.calls = []
        self.policy_calls = []
        self.context = ToolContext(
            search_policy=self.search_policy,
            catalog=lambda: None,
            authorize=lambda _name: None,
            emit=lambda _kind, _data: None,
        )
        self.tools = build_tools(self.context, self.scenario.data["tools"])

    def search_policy(self, query):
        self.policy_calls.append((query, self.scenario.id, threading.get_ident()))
        return {
            "status": "success",
            "data": {
                "query": query,
                "corpus_version": "test-corpus-version",
                "clauses": [{"text": "条款原文", "url": "https://www.meituan.com/policy"}],
            },
            "evidence": ["policy:source-1:chunk-2"],
            "retryable": False,
        }


@pytest.mark.asyncio
async def test_native_skill_is_lazy_and_business_only_without_custom_call_limit(tmp_path):
    from agentscope.state import AgentState

    runtime = ToolFixture(tmp_path)
    toolkit = create_toolkit(runtime.tools, runtime.scenario)
    schemas = await toolkit.get_tool_schemas()
    assert {x["function"]["name"] for x in schemas} == {
        "search_policy",
        "Skill",
    }
    metadata = await toolkit.get_skill_instructions()
    assert "业务七号" not in metadata
    viewer = toolkit.builtin_skill_viewer.tool
    for _ in range(10):
        chunk = await viewer(skill="guide", _agent_state=AgentState())
        assert "业务七号" in str(chunk)
        assert "HOST_BUDGET" not in str(chunk)
    assert not hasattr(runtime, "tool_count")


@pytest.mark.asyncio
async def test_functiontool_infers_signature_types_and_descriptions(tmp_path):
    from jsonschema import Draft202012Validator

    runtime = ToolFixture(tmp_path)
    toolkit = create_toolkit(runtime.tools, runtime.scenario)
    schemas = {x["function"]["name"]: x["function"] for x in await toolkit.get_tool_schemas()}
    assert not {"read_memory", "write_memory"} & schemas.keys()
    policy = schemas["search_policy"]["parameters"]
    assert set(policy["required"]) == {"query"}
    assert "客户的具体政策问题" in policy["properties"]["query"]["description"]
    validator = Draft202012Validator(policy)
    assert validator.is_valid({"query": "报名条件"})
    assert not validator.is_valid({"query": 123})


@pytest.mark.parametrize(
    "scenario_id",
    ["meituan_rider", "meituan_merchant", "didi_driver", "financial_consultation"],
)
async def test_each_scenario_registers_only_its_tools_without_host_parameters(
    tmp_path, scenario_id
):
    from backend.agent_harness.scenario import load_scenario
    from backend.agent_harness.tools import build_tools
    from tests.paths import ROOT

    scenario = load_scenario(ROOT / "backend/scenarios" / scenario_id / "scenario.yaml")
    context = ToolFixture(tmp_path).context
    toolkit = create_toolkit(build_tools(context, scenario["tools"]), scenario)
    schemas = {
        item["function"]["name"]: item["function"] for item in await toolkit.get_tool_schemas()
    }
    assert set(schemas) == {spec["id"] for spec in scenario["tools"]} | {"Skill"}
    for spec in scenario["tools"]:
        properties = schemas[spec["id"]]["parameters"]["properties"]
        assert (
            not {"context", "self", "customer_id", "scenario_id", "workspace_id"}
            & properties.keys()
        )


@pytest.mark.asyncio
async def test_policy_tool_is_one_query_with_host_scope_and_preserved_evidence(tmp_path):
    runtime = ToolFixture(tmp_path)
    toolkit = create_toolkit(runtime.tools, runtime.scenario)
    schemas = {x["function"]["name"]: x["function"] for x in await toolkit.get_tool_schemas()}
    assert "query_fact" not in schemas
    parameters = schemas["search_policy"]["parameters"]
    assert set(parameters["properties"]) == {"query"}
    assert parameters["required"] == ["query"]
    search = await toolkit.get_tool("search_policy")
    result = str(await search(query="杭州众包报名需要哪些材料？"))
    assert "条款原文" in result and "https://www.meituan.com/policy" in result
    assert "test-corpus-version" in result and "policy:source-1:chunk-2" in result
    query, scenario_id, thread_id = runtime.policy_calls[0]
    assert query == "杭州众包报名需要哪些材料？" and scenario_id == "meituan_rider"
    assert thread_id != threading.get_ident()
    for invalid in ({"query": " "}, {"query": "费用", "scenario_id": "didi_driver"}):
        assert "invalid_arguments:ValidationError" in str(await search(**invalid))
    assert len(runtime.policy_calls) == 1


@pytest.mark.asyncio
async def test_native_business_result_is_not_rewritten_and_unknown_arguments_are_denied(tmp_path):

    runtime = ToolFixture(tmp_path)
    toolkit = create_toolkit(runtime.tools, runtime.scenario)
    search = await toolkit.get_tool("search_policy")
    for _ in range(10):
        chunk = await search(query="报名条件")
        assert "host_budget" not in str(chunk)
        assert "条款原文" in str(chunk)
    assert len(runtime.policy_calls) == 10
    invalid = await search(query="报名条件", customer_id="other")
    assert "invalid_arguments:ValidationError" in str(invalid)
    invalid = await search(query=True)
    assert "invalid_arguments:ValidationError" in str(invalid)
    assert len(runtime.policy_calls) == 10


@pytest.mark.asyncio
async def test_new_typed_function_needs_no_schema_or_dispatcher_edit(tmp_path, monkeypatch):
    from backend.agent_harness.tools import build_tools
    from backend.agent_harness.tools.boundary import business_tool
    from backend.agent_harness.tools.registry import TOOL_FUNCTIONS

    @business_tool
    async def explain_limit(context, count: int = 2) -> dict:
        """读取可用数量。

        Args:
            count: 查询数量。
        """
        return {"status": "success", "data": {"count": count}}

    monkeypatch.setitem(TOOL_FUNCTIONS, "explain_limit", explain_limit)
    runtime = ToolFixture(tmp_path)
    runtime.scenario.data["tools"].append({"id": "explain_limit"})
    runtime.tools = build_tools(runtime.context, runtime.scenario.data["tools"])
    toolkit = create_toolkit(runtime.tools, runtime.scenario)
    schema = next(
        x["function"]
        for x in await toolkit.get_tool_schemas()
        if x["function"]["name"] == "explain_limit"
    )
    assert schema["parameters"]["properties"]["count"]["type"] == "integer"
    assert set(schema["parameters"]["properties"]) == {"count"}
    tool = await toolkit.get_tool("explain_limit")
    assert '"count": 3' in str(await tool(count=3))


def test_catalog_projection_keeps_business_constraints_and_raw_result_intact():
    from copy import deepcopy

    from backend.agent_harness.tools.boundary import model_projection

    raw = {
        "status": "success",
        "data": {
            "source_type": "local_catalog",
            "disclosure": "internal catalog disclosure",
            "match_type": "generic_fallback",
            "is_fallback": True,
            "generated": True,
            "template_provenance": {"template_id": "template-1"},
            "requested_location": "南华县",
            "matched_location": "南华县",
            "coverage_level": "generic",
            "requested_filters": {"purpose": "了解虚拟账户", "service_type": "商务专车"},
            "assumed_filters": [{"service_type": "__generic__"}],
            "items": [
                {
                    "id": "catalog_product_1",
                    "source_type": "local_catalog",
                    "source": {"id": "catalog-author"},
                    "pricing_basis": {"profile_parameter": "internal-only"},
                    "name": "费用方案",
                    "valid_until": "2027-11-01T00:00:00Z",
                    "address": None,
                    "phone": None,
                    "annual_rate_percent": 12,
                    "assumptions": ["无复利，不含逾期费用"],
                }
            ],
            "requested_product_id": "customer-product",
            "effective_product_id": "catalog_product_1",
            "calculation": {"product_id": "catalog_product_1", "interest_yuan": 300},
        },
        "evidence": [
            {
                "id": "catalog_product_1",
                "source_type": "local_catalog",
                "source": {"id": "catalog-author"},
            }
        ],
    }
    saved = deepcopy(raw)
    visible = model_projection(raw)
    assert raw == saved
    assert visible["data"]["requested_filters"] == raw["data"]["requested_filters"]
    assert visible["data"]["assumed_filters"] == [{"service_type": "通用条件"}]
    assert visible["data"]["calculation"] == raw["data"]["calculation"]
    assert visible["data"]["requested_product_id"] != visible["data"]["effective_product_id"]
    item = visible["data"]["items"][0]
    assert item["id"] == "catalog_product_1" and item["annual_rate_percent"] == 12
    assert item["assumptions"] == ["无复利，不含逾期费用"]
    assert item["address"] is None and item["phone"] is None
    assert item["valid_until"] == "2027-11-01T00:00:00Z"
    assert "source" not in item and "pricing_basis" not in item
    assert "source_type" not in visible["data"]
    assert "template_provenance" not in visible["data"]


def test_projection_preserves_official_evidence_and_customer_language():
    from backend.agent_harness.tools.boundary import model_projection

    policy = {
        "status": "success",
        "data": {
            "clauses": [
                {
                    "text": "可使用还款测算查看计划",
                    "source": {"title": "官方说明", "url": "https://example.com/policy"},
                    "source_type": "official",
                    "valid_until": "2027-01-01",
                }
            ]
        },
        "evidence": [{"source": {"id": "official-source"}, "title": "虚拟账户说明"}],
    }
    assert model_projection(policy) == policy
    memory = {"data": {"fields": [{"quote": "我想了解还款测算和账户安全", "value": "__generic__"}]}}
    assert model_projection(memory) == memory
