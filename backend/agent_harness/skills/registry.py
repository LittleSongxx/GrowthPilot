"""按场景清单使用 AgentScope 原生 Skill 加载器。"""

from dataclasses import replace

from agentscope.skill import LocalSkillLoader

from backend.paths import portable_path


class _PortableSkillLoader(LocalSkillLoader):
    """保留内部文件定位，只让给模型的 Skill 目录使用项目展示路径。"""

    async def list_skills(self):
        skills = await super().list_skills()
        return [replace(skill, dir=portable_path(skill.dir)) for skill in skills]


def skill_loaders(scenario):
    return [
        _PortableSkillLoader(str((scenario.root / item["ref"]).resolve().parent), scan_subdir=False)
        for item in scenario.data["skills"]
    ]
