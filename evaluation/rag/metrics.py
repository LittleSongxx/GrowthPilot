"""三个标准检索指标；重复结果和不足返回条数都不能抬高分数。"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

CUTOFFS = (1, 3, 5, 8)
MAX_RESULTS = max(CUTOFFS)
METRIC_NAMES = tuple(name for k in CUTOFFS for name in (f"recall_at_{k}", f"precision_at_{k}")) + (
    "mrr_at_8",
)


def score(retrieved_ids: Sequence[str], relevant_ids: Iterable[str]) -> dict[str, float]:
    """只使用生产检索返回的前八个位置，计算片段级指标。

    重复片段只在第一次出现时计分，原始位置不压缩；不足 k 条按空位处理。
    这里的 MRR 截断于第八位，未命中为零，不代表无限检索深度下的 MRR。
    """
    gold = set(relevant_ids)
    if not gold or any(not isinstance(item, str) or not item.strip() for item in gold):
        raise ValueError("RAG 题目必须标注至少一个非空的相关片段 ID。")
    ranked = list(retrieved_ids[:MAX_RESULTS])
    if any(not isinstance(item, str) or not item.strip() for item in ranked):
        raise ValueError("检索结果中的片段 ID 必须是非空字符串。")

    seen = set()
    hits = []
    for item in ranked:
        hits.append(item in gold and item not in seen)
        seen.add(item)
    result = {}
    for k in CUTOFFS:
        count = sum(hits[:k])
        result[f"recall_at_{k}"] = count / len(gold)
        result[f"precision_at_{k}"] = count / k
    result["mrr_at_8"] = next((1 / rank for rank, hit in enumerate(hits, start=1) if hit), 0.0)
    return result


def aggregate(rows: Sequence[dict]) -> dict:
    """按全部计划案例做宏平均；执行失败保留零分，不能通过删掉失败提高均值。"""
    planned = len(rows)
    scored = sum(row.get("status") == "scored" for row in rows)
    errors = sum(row.get("status") == "error" for row in rows)
    return {
        "planned": planned,
        "scored": scored,
        "errors": errors,
        "not_run": planned - scored - errors,
        "metrics": {
            name: sum((row.get("metrics") or {}).get(name, 0.0) for row in rows) / planned
            for name in METRIC_NAMES
        }
        if planned
        else None,
    }
