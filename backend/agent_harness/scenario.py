"""加载场景及其会话所需的资源。"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

import yaml

from backend.paths import resolve_project_path

from .runtime.snapshots import digest


@dataclass(frozen=True)
class Scenario:
    data: dict
    root: Path
    snapshot_hash: str
    resources: dict

    @property
    def id(self):
        return self.data["scenario"]["id"]

    @property
    def version(self):
        return self.data["scenario"]["version"]

    @property
    def fields(self):
        return {field["id"]: field for field in self.data["customer_fields"]}

    def __getitem__(self, key):
        return self.data[key]

    def agent_view(self):
        """提供业务上下文，不包含评测数据或本地资源路径。"""
        context = copy.deepcopy({k: v for k, v in self.data.items() if k != "evaluation"})
        if "business_data" in context:
            del context["business_data"]["ref"]
        context["skills"] = [
            {k: v for k, v in skill.items() if k not in ("ref", "sha256")}
            for skill in context["skills"]
        ]
        return context


def load_scenario(path):
    path = resolve_project_path(path)
    root = path.parent
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    tool_ids = [item["id"] for item in data["tools"]]
    skill_ids = [item["id"] for item in data["skills"]]
    if len(set(tool_ids)) != len(tool_ids) or len(set(skill_ids)) != len(skill_ids):
        raise ValueError("场景中的工具与 Skill 标识不能重复")
    for skill in data["skills"]:
        missing = set(skill.get("required_tools", [])) - set(tool_ids)
        if missing:
            raise ValueError(f"Skill {skill['id']} 缺少场景工具：{', '.join(sorted(missing))}")
    refs = [skill["ref"] for skill in data["skills"]]
    refs += [ref for name, ref in data.get("evaluation", {}).items() if name.endswith("_ref")]
    if "business_data" in data:
        refs.append(data["business_data"]["ref"])
    if any(Path(ref).is_absolute() or PureWindowsPath(ref).is_absolute() for ref in refs):
        raise ValueError("场景资源 ref 必须使用相对于 scenario.yaml 的路径")
    resources = {ref: (root / ref).read_text(encoding="utf-8") for ref in refs}
    snapshot_hash = digest({"config": data, "resources": resources})
    return Scenario(data, root, snapshot_hash, resources)
