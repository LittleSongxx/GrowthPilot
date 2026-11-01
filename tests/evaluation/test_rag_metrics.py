"""核验标准分母、截断范围和重复证据的计分规则。"""

import pytest

from evaluation.rag.metrics import aggregate, score


def test_standard_metrics_use_the_same_original_ranking():
    result = score(["wrong", "a", "a", "b", "other", "x", "y", "z", "c"], ["a", "b", "c"])
    assert result == {
        "recall_at_1": 0,
        "precision_at_1": 0,
        "recall_at_3": 1 / 3,
        "precision_at_3": 1 / 3,
        "recall_at_5": 2 / 3,
        "precision_at_5": 2 / 5,
        "recall_at_8": 2 / 3,
        "precision_at_8": 2 / 8,
        "mrr_at_8": 1 / 2,
    }


def test_short_result_does_not_reduce_precision_denominator():
    result = score(["a"], ["a", "a"])
    assert result["recall_at_8"] == 1
    assert result["precision_at_8"] == 1 / 8
    assert result["mrr_at_8"] == 1


def test_duplicate_slots_do_not_improve_rank_or_pull_in_the_ninth_result():
    result = score(["wrong", "wrong", "a"], ["a"])
    assert result["mrr_at_8"] == 1 / 3
    assert result["precision_at_3"] == 1 / 3
    assert all(value == 0 for value in score(["wrong"] * 8 + ["a"], ["a"]).values())


@pytest.mark.parametrize("gold", [[], [""], [None]])
def test_missing_gold_cannot_be_scored_as_a_valid_question(gold):
    with pytest.raises(ValueError, match="相关片段 ID"):
        score([], gold)


def test_all_planned_cases_stay_in_the_aggregate_denominator():
    result = aggregate(
        [
            {"status": "scored", "metrics": score(["a"], ["a"])},
            {"status": "error", "metrics": score([], ["a"])},
            {"status": "not_run", "metrics": None},
        ]
    )
    assert (result["planned"], result["scored"], result["errors"], result["not_run"]) == (
        3,
        1,
        1,
        1,
    )
    assert result["metrics"]["recall_at_8"] == 1 / 3
    assert result["metrics"]["precision_at_8"] == 1 / 24
    assert aggregate([])["metrics"] is None
