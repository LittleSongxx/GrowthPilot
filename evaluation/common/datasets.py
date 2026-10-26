"""读取按业务分开的题集；选择子集时保留案例来源和知识版本。"""

import json
import re
from pathlib import Path

SCENARIOS = (
    "meituan_rider",
    "meituan_merchant",
    "didi_driver",
    "financial_consultation",
)


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串")


def _strings(value, name, *, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(f"{name} 必须是字符串列表")
    for item in value:
        _text(item, name)


def _load(project_root, kind, scenario):
    if scenario is not None and scenario not in SCENARIOS:
        raise ValueError(f"未知评测场景：{scenario}")
    scenarios = [scenario] if scenario else SCENARIOS
    datasets, seen = [], set()
    for sid in scenarios:
        path = Path(project_root) / "evaluation/datasets" / kind / f"{sid}.json"
        dataset = json.loads(path.read_text())
        expected_version = "rag-v1" if kind == "rag" else "dialogue-v3"
        if dataset.get("scenario_id") != sid or dataset.get("version") != expected_version:
            raise ValueError(f"题集场景或版本不匹配：{path.name}")
        if not isinstance(dataset.get("cases"), list) or not dataset["cases"]:
            raise ValueError(f"题集不能为空：{path.name}")
        if kind == "rag":
            _text(dataset.get("corpus_version"), "corpus_version")
        for case in dataset["cases"]:
            for field in ("id", "topic"):
                _text(case.get(field), field)
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", case["id"]):
                raise ValueError("案例 ID 只能包含字母、数字、短横线和下划线")
            if case["id"] in seen:
                raise ValueError(f"案例 ID 重复：{case['id']}")
            seen.add(case["id"])
            if kind == "rag":
                _validate_rag(case)
            else:
                _validate_dialogue(case)
        datasets.append(dataset)
    return datasets


def _validate_rag(case):
    for field in ("question", "question_family"):
        _text(case.get(field), field)
    gold = case.get("relevant_chunk_ids")
    _strings(gold, "relevant_chunk_ids", nonempty=True)
    if len(gold) != len(set(gold)):
        raise ValueError(f"相关证据 ID 重复：{case['id']}")
    evidence = case.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError(f"缺少原文标注：{case['id']}")
    for item in evidence:
        for field in ("chunk_id", "document_id", "content_hash", "quote"):
            _text(item.get(field), field)
    if {item["chunk_id"] for item in evidence} != set(gold):
        raise ValueError(f"相关证据与引用未逐项对应：{case['id']}")


def _validate_dialogue(case):
    if "messages" in case or "expected_skills" in case:
        raise ValueError("动态案例不接受固定客户台词或 Skill 加载要求")
    customer, goal = case.get("customer"), case.get("goal")
    if not isinstance(customer, dict) or not isinstance(goal, dict):
        raise TypeError("动态案例必须包含 customer 和 goal")
    for field in ("profile", "initial_state"):
        _text(customer.get(field), f"customer.{field}")
    for field in (
        "concerns",
        "acceptance_conditions",
        "refusal_conditions",
        "disclosure_rules",
    ):
        _strings(customer.get(field), f"customer.{field}")
    if goal.get("type") not in {"progression", "consultation", "refusal"}:
        raise ValueError("未知案例目标类型")
    _text(goal.get("description"), "goal.description")
    _strings(goal.get("success_conditions"), "goal.success_conditions", nonempty=True)
    if type(case.get("max_turns")) is not int or not 1 <= case["max_turns"] <= 20:
        raise ValueError("max_turns 必须是 1 到 20 的整数")


def load_rag_datasets(project_root, scenario=None):
    """只读题集，不加载检索模型，也不调用任何模型服务。"""
    return _load(project_root, "rag", scenario)


def load_dialogue_datasets(project_root, scenario=None):
    """客户设定与评分目标分别保存，由运行器按职责传入。"""
    return _load(project_root, "dialogue", scenario)


def select_cases(datasets, limit=None, case_ids=None):
    """limit 按场景限制；明确指定案例时拒绝未知或重复 ID。"""
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("limit 必须是正整数")
    if case_ids is not None and (not case_ids or len(case_ids) != len(set(case_ids))):
        raise ValueError("case_ids 不能为空或重复")
    all_ids = {case["id"] for dataset in datasets for case in dataset["cases"]}
    if case_ids and set(case_ids) - all_ids:
        raise ValueError("指定案例不属于所选场景题集")
    selected = []
    for dataset in datasets:
        cases = [case for case in dataset["cases"] if not case_ids or case["id"] in case_ids]
        if limit:
            cases = cases[:limit]
        for case in cases:
            row = {**case, "scenario_id": dataset["scenario_id"]}
            if "corpus_version" in dataset:
                row["corpus_version"] = dataset["corpus_version"]
            selected.append(row)
    return selected
