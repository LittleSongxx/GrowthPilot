"""验证原生目录加载和技能正文的按需加载。"""

from types import SimpleNamespace

import pytest
from agentscope.skill import LocalSkillLoader
from agentscope.state import AgentState
from agentscope.tool import Toolkit

from backend.agent_harness import load_scenario
from backend.agent_harness.skills import skill_loaders
from backend.paths import portable_path
from tests.paths import ROOT


@pytest.mark.parametrize(
    "scenario_id", ["didi_driver", "meituan_rider", "meituan_merchant", "financial_consultation"]
)
async def test_declared_standard_skills_load_through_native_directories(scenario_id):
    scenario = load_scenario(ROOT / "backend/scenarios" / scenario_id / "scenario.yaml")
    loaders = skill_loaders(scenario)
    assert len(loaders) == len(scenario["skills"])
    assert all(isinstance(loader, LocalSkillLoader) for loader in loaders)
    loaded = [skill for loader in loaders for skill in await loader.list_skills()]
    assert {skill.name for skill in loaded} == {item["id"] for item in scenario["skills"]}
    assert all(skill.description and skill.markdown for skill in loaded)
    assert all(
        item["ref"].endswith("/SKILL.md") and "sha256" not in item for item in scenario["skills"]
    )
    for skill, item in zip(loaded, scenario["skills"], strict=True):
        assert skill.dir == portable_path((scenario.root / item["ref"]).resolve().parent)
    toolkit = Toolkit(skills_or_loaders=loaders)
    metadata = await toolkit.get_skill_instructions()
    assert str(ROOT) not in metadata
    for skill in loaded:
        assert skill.name in metadata and skill.description in metadata
        assert skill.markdown not in metadata
        response = await toolkit.builtin_skill_viewer.tool(
            skill=skill.name, _agent_state=AgentState()
        )
        assert skill.markdown in "\n".join(block.text for block in response.content)
    assert {row["function"]["name"] for row in await toolkit.get_tool_schemas()} == {"Skill"}


async def test_new_standard_directory_is_lazy_and_does_not_register_extra_tools(tmp_path):
    directory = tmp_path / "visit_followup"
    directory.mkdir()
    path = directory / "SKILL.md"
    body = (
        "---\nname: visit_followup\ndescription: 到店咨询后续沟通\n---\n只在客户愿意时说明下一步。"
    )
    path.write_text(body)
    # 临时场景与已安装场景遵循相同的真实目录约定。
    scenario = SimpleNamespace(
        root=tmp_path,
        data={"skills": [{"ref": "visit_followup/SKILL.md"}]},
        resources={"visit_followup/SKILL.md": body},
    )
    toolkit = Toolkit(skills_or_loaders=skill_loaders(scenario))
    metadata = await toolkit.get_skill_instructions()
    assert "visit_followup" in metadata and "到店咨询后续沟通" in metadata
    assert "只在客户愿意" not in metadata
    schemas = await toolkit.get_tool_schemas()
    assert {schema["function"]["name"] for schema in schemas} == {"Skill"}
    chunk = await toolkit.builtin_skill_viewer.tool(
        skill="visit_followup", _agent_state=AgentState()
    )
    assert "只在客户愿意时说明下一步" in str(chunk)


async def test_new_loader_reads_updated_skill_without_changing_saved_resources(tmp_path):
    path = tmp_path / "SKILL.md"
    original = "---\nname: followup\ndescription: 后续沟通\n---\n原有步骤。"
    path.write_text(original)
    scenario = SimpleNamespace(
        root=tmp_path,
        data={"skills": [{"ref": "SKILL.md"}]},
        resources={"SKILL.md": original},
    )
    loader = skill_loaders(scenario)[0]
    assert (await loader.list_skills())[0].markdown == "原有步骤。"
    updated = original.replace("原有步骤", "更新步骤")
    path.write_text(updated)
    # 新加载器读取当前维护的源文件；历史资源包保持不变。
    assert scenario.resources["SKILL.md"] == original
    fresh = SimpleNamespace(root=tmp_path, data=scenario.data, resources={"SKILL.md": updated})
    assert (await skill_loaders(fresh)[0].list_skills())[0].markdown == "更新步骤。"
