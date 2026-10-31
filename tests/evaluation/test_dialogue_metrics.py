"""验证等级评分、缺测保护、真实用量范围及客户请求的首 token 时延。"""

from copy import deepcopy

import pytest

from evaluation.dialogue.metrics import LATENCY_VERSION, agent_tokens, summarize
from evaluation.judges.schema import JUDGE_VERSION, QUALITY_DIMENSIONS


def case(case_id="case-1", scenario="meituan_rider", goal_type="progression"):
    return {"id": case_id, "scenario_id": scenario, "goal": {"type": goal_type}}


def trace(case_id="case-1", repeat=1, *, score=5, tokens=30, elapsed=2, first_token=1):
    return {
        "case_id": case_id, "repeat": repeat, "status": "completed",
        "judge_version": JUDGE_VERSION,
        "turns": [{
            "turn": 1, "origin": "customer", "user_message": "请说明一下",
            "reply": "有依据的业务回复", "events": [], "elapsed_seconds": elapsed,
            "first_token_seconds": first_token,
            "usage_events": [{"type": "usage", "data": {
                "input_tokens": tokens - 10, "output_tokens": 10,
            }}],
        }],
        "review": {dimension: {
            "score": score, "reason": "根据本通实际表现评分",
        } for dimension in QUALITY_DIMENSIONS},
    }


def no_score(item, dimension, null_reason="unassessable"):
    item["review"][dimension].update(score=None, null_reason=null_reason)
    return item


def test_six_metrics_keep_integer_grades_and_show_distributions():
    first, second = trace(), trace(repeat=2, score=2, tokens=50, elapsed=10, first_token=3)
    summary = summarize([first, second], [case()])
    assert summary["status"] == "completed"
    assert summary["latency_version"] == LATENCY_VERSION == "agent-first-token-v1"
    assert summary["metrics"] == {
        **dict.fromkeys(QUALITY_DIMENSIONS, 3.5),
        "average_agent_tokens": 40,
        "agent_first_token_latency_p50_seconds": 2,
        "agent_first_token_latency_p95_seconds": pytest.approx(2.9),
    }
    assert summary["counts"]["expected_runs"] == 2
    for dimension in QUALITY_DIMENSIONS:
        detail = summary["quality"][dimension]
        assert detail["score_distribution"] == {"1": 0, "2": 1, "3": 0, "4": 0, "5": 1}
        assert detail["coverage"] == 1
        assert detail["low_score_cases"] == [{
            "case_id": "case-1", "repeat": 2, "score": 2,
            "reason": "根据本通实际表现评分",
        }]


def test_profile_missing_history_is_unknown_without_changing_quality_scores():
    summary = summarize([trace(), trace(repeat=2)], [case()])
    profile = summary["profile_processing"]
    assert summary["status"] == "completed"
    assert summary["metrics"]["goal_completion"] == 5
    assert profile["status"] == "unknown"
    assert profile["unknown_runs"] == profile["state_counts"]["missing"] == 2
    assert profile["incomplete_runs"] == profile["state_counts"]["failed"] == 0
    assert profile["coverage"] == 0
    assert profile["issues"][0]["subsequent_repeats"] == [2]
    assert profile["issues"][1]["subsequent_repeats"] == []


def test_completed_or_skipped_profiles_are_normal_and_grouped_by_scenario():
    rider, driver = trace(), trace("driver-1")
    rider["profile_processing"] = {"status": "completed", "attempts": 2}
    driver["profile_processing"] = {"status": "skipped", "attempts": 0}
    summary = summarize([rider, driver], [case(), case("driver-1", "didi_driver")], repeats=1)
    profile = summary["profile_processing"]
    assert profile["status"] == "completed"
    assert profile["successful_runs"] == 2 and profile["coverage"] == 1
    assert profile["issues"] == []
    assert summary["by_scenario"]["meituan_rider"]["profile_processing"]["state_counts"]["completed"] == 1
    assert summary["by_scenario"]["didi_driver"]["profile_processing"]["state_counts"]["skipped"] == 1
    assert summary["by_goal_type"]["progression"]["profile_processing"] == profile


def test_profile_failures_keep_safe_categories_and_deduplicate_later_call_risks():
    first, second, third = trace(), trace(repeat=2), trace(repeat=3)
    first["profile_processing"] = {"status": "failed", "attempts": 3, "error": "ValidationError"}
    second["profile_processing"] = {
        "status": "timeout", "last_state": {"status": "retrying", "attempts": 2, "error": "TimeoutError"},
    }
    third["profile_processing"] = {"status": "completed"}
    summary = summarize([first, second, third], [case()], repeats=3)
    profile = summary["profile_processing"]
    assert summary["status"] == "completed"
    assert summary["metrics"]["goal_completion"] == 5
    assert profile["status"] == "incomplete"
    assert profile["incomplete_runs"] == 2 and profile["unknown_runs"] == 0
    assert profile["coverage"] == 1
    assert profile["potentially_affected_subsequent_runs"] == 2
    assert profile["issues"][0]["subsequent_repeats"] == [2, 3]
    assert profile["issues"][0]["error_category"] == "ValidationError"
    assert profile["issues"][1]["last_status"] == "retrying"
    assert profile["issues"][1]["attempts"] == 2


@pytest.mark.parametrize("status", ["error", "interrupted", "unavailable", "running", "cancelled"])
def test_other_profile_incomplete_states_are_not_silently_accepted(status):
    item = trace()
    item["profile_processing"] = {"status": status, "error_category": "RuntimeError"}
    result = summarize([item], [case()], repeats=1)["profile_processing"]
    assert result["status"] == "incomplete"
    assert result["state_counts"][status] == result["incomplete_runs"] == 1
    assert result["issues"][0]["status"] == status


def test_profile_summary_never_copies_arbitrary_error_bodies_or_unknown_statuses():
    first, second = trace(), trace(repeat=2)
    first["profile_processing"] = {
        "status": "failed", "error": "Bearer secret-key https://provider/private?key=secret",
    }
    second["profile_processing"] = {"status": ["secret-state"], "attempts": -1}
    summary = summarize([first, second], [case()])["profile_processing"]
    assert summary["issues"][0]["error_category"] == "unclassified_error"
    assert summary["issues"][1]["status"] == "unknown"
    assert "attempts" not in summary["issues"][1]
    assert "secret" not in str(summary)


def test_missing_call_does_not_create_a_fake_profile_failure_or_trace_link():
    summary = summarize([], [case()], repeats=1)["profile_processing"]
    assert summary["status"] == "unknown"
    assert summary["state_counts"]["missing"] == 1
    assert summary["state_counts"]["failed"] == 0
    assert summary["issues"][0]["trace_recorded"] is False


@pytest.mark.parametrize("status", [
    "agent_error", "timeout", "customer_error", "judge_error", "infrastructure_error", "interrupted",
])
def test_incomplete_calls_never_keep_normal_high_quality_scores(status):
    failed = trace(repeat=2)
    failed["status"] = status
    summary = summarize([trace(), failed], [case()])
    assert summary["status"] == "incomplete"
    assert summary["errors"] == {status: 1}
    for dimension in QUALITY_DIMENSIONS:
        assert summary["metrics"][dimension] is None
        detail = summary["quality"][dimension]
        assert detail["unassessable_runs"] == 1
        assert detail["observed_mean"] == 5
        assert detail["coverage"] == 0.5
    assert summary["metrics"]["average_agent_tokens"] == 30
    assert summary["metrics"]["agent_first_token_latency_p50_seconds"] == 1


def test_missing_repeat_stays_in_expected_counts_and_blocks_formal_metrics():
    summary = summarize([trace()], [case()])
    assert summary["status"] == "incomplete"
    assert summary["counts"]["expected_runs"] == 2
    assert summary["counts"]["recorded_runs"] == 1
    assert summary["errors"] == {"missing_run": 1}
    assert all(value is None for value in summary["metrics"].values())
    assert summary["quality"]["goal_completion"]["observed_mean"] == 5
    assert summary["latency"]["unobserved_runs"] == 1
    assert summary["latency"]["observed_p50_seconds"] == 1


@pytest.mark.parametrize("dimension", ["problem_resolution", "factual_reliability"])
def test_not_applicable_is_excluded_without_becoming_unknown_or_full_marks(dimension):
    first = no_score(trace(), dimension, "not_applicable")
    result = summarize([first, trace(repeat=2, score=3)], [case()])
    assert result["status"] == "completed"
    assert result["metrics"][dimension] == 3
    detail = result["quality"][dimension]
    assert detail["expected_runs"] == 2
    assert detail["applicable_runs"] == detail["assessed_runs"] == 1
    assert detail["not_applicable_runs"] == 1
    assert detail["unassessable_runs"] == 0
    assert detail["coverage"] == 1
    all_na = summarize([first], [case()], repeats=1)
    assert all_na["status"] == "completed"
    assert all_na["metrics"][dimension] is None
    assert all_na["quality"][dimension]["observed_mean"] is None
    assert all_na["quality"][dimension]["coverage"] is None


@pytest.mark.parametrize("dimension", ["goal_completion", "communication_quality"])
def test_required_quality_dimensions_cannot_be_removed_as_not_applicable(dimension):
    result = summarize([no_score(trace(), dimension, "not_applicable")], [case()], repeats=1)
    assert result["quality"][dimension]["unassessable_runs"] == 1
    assert result["quality"][dimension]["not_applicable_runs"] == 0


@pytest.mark.parametrize("invalid", [True, 0, 6, 3.5, "5", None])
def test_invalid_grades_remain_unassessable(invalid):
    item = trace()
    item["review"]["goal_completion"]["score"] = invalid
    result = summarize([item], [case()], repeats=1)
    assert result["metrics"]["goal_completion"] is None
    assert result["quality"]["goal_completion"]["unassessable_runs"] == 1
    assert result["metrics"]["communication_quality"] == 5


def test_old_binary_reviews_and_unversioned_grades_are_not_converted():
    old = trace()
    old.pop("judge_version")
    old["review"] = {"goal": {"verdict": "pass"}, "facts": []}
    unversioned = trace(repeat=2)
    unversioned.pop("judge_version")
    result = summarize([old, unversioned], [case()])
    for dimension in QUALITY_DIMENSIONS:
        assert result["metrics"][dimension] is None
        assert result["quality"][dimension]["unassessable_runs"] == 2


def test_unknown_dimension_does_not_erase_other_complete_results():
    unknown = no_score(trace(repeat=2, score=3, tokens=50), "factual_reliability")
    result = summarize([trace(), unknown], [case()])
    assert result["status"] == "incomplete"
    assert result["metrics"]["goal_completion"] == 4
    assert result["metrics"]["problem_resolution"] == 4
    assert result["metrics"]["communication_quality"] == 4
    assert result["metrics"]["factual_reliability"] is None
    assert result["metrics"]["average_agent_tokens"] == 40
    assert result["quality"]["factual_reliability"]["observed_mean"] == 5
    assert result["quality"]["factual_reliability"]["coverage"] == 0.5


def test_unknown_token_usage_stays_unknown_and_background_cost_is_separate():
    item = trace()
    item["profile_usage_events"] = [{"data": {"input_tokens": 9999, "output_tokens": 9999}}]
    item["customer_calls"] = [{"usage": {"total_tokens": 5000}}]
    item["judge_usage"] = {"total_tokens": 5000}
    assert agent_tokens(item) == 30
    item["turns"][0]["usage_events"][0]["data"]["input_tokens"] = None
    assert agent_tokens(item) is None
    summary = summarize([item], [case()], repeats=1)
    assert summary["counts"]["agent_token_unknown_runs"] == 1
    assert summary["status"] == "incomplete"
    assert summary["metrics"]["average_agent_tokens"] is None
    assert summary["metrics"]["factual_reliability"] == 5


def test_unfinished_model_request_prevents_partial_token_total_being_complete():
    item = trace()
    item["turns"][0]["events"] = [
        {"type": "framework", "data": {"type": "MODEL_CALL_START"}},
        {"type": "framework", "data": {"type": "MODEL_CALL_START"}},
    ]
    assert agent_tokens(item) is None
    item["turns"][0]["events"][-1]["data"]["source"] = "end_call"
    assert agent_tokens(item) == 30


def test_latency_excludes_opening_failed_turns_and_background_waits():
    item = trace()
    item["elapsed_seconds"] = 900
    item["customer_calls"] = [{"elapsed_seconds": 80}]
    item["judge_usage"] = {"elapsed_seconds": 90}
    item["turns"].insert(0, {
        "turn": 0, "origin": "host_control", "reply": "您好", "elapsed_seconds": 200,
        "first_token_seconds": 100,
        "usage_events": [],
    })
    item["turns"].extend([
        {"turn": 2, "origin": "customer", "reply": "未正常交付", "elapsed_seconds": 300,
         "first_token_seconds": 150,
         "error_category": "TimeoutError", "usage_events": []},
        {"turn": 3, "origin": "customer", "reply": "", "elapsed_seconds": 400,
         "first_token_seconds": 200,
         "usage_events": []},
    ])
    result = summarize([item], [case()], repeats=1)
    assert result["metrics"]["agent_first_token_latency_p50_seconds"] == 1
    assert result["metrics"]["agent_first_token_latency_p95_seconds"] == 1
    assert result["latency"]["eligible_reply_turns"] == 1
    assert result["latency"]["failed_reply_turns"] == 2
    assert result["latency"]["excluded_opening_turns"] == 1


@pytest.mark.parametrize("first_token", [None, -1, True, "2", float("nan"), float("inf")])
def test_missing_or_invalid_latency_blocks_formal_percentiles_without_fake_zero(first_token):
    result = summarize([trace(), trace(repeat=2, first_token=first_token)], [case()])
    assert result["status"] == "incomplete"
    assert result["metrics"]["agent_first_token_latency_p50_seconds"] is None
    assert result["metrics"]["agent_first_token_latency_p95_seconds"] is None
    assert result["metrics"]["goal_completion"] == 5
    assert result["latency"]["observed_p50_seconds"] == 1
    assert result["latency"]["coverage"] == 0.5
    assert result["latency"]["unknown_reply_turns"] == 1


def test_old_complete_response_time_is_never_used_as_first_token_time():
    old = trace(repeat=2, elapsed=10)
    old["turns"][0].pop("first_token_seconds")
    result = summarize([trace(first_token=0.5), old], [case()])
    assert result["status"] == "incomplete"
    assert result["metrics"]["agent_first_token_latency_p50_seconds"] is None
    assert "agent_response_latency_p50_seconds" not in result["metrics"]
    assert result["latency"]["observed_p50_seconds"] == 0.5
    assert result["latency"]["coverage"] == 0.5


@pytest.mark.parametrize("elapsed", [None, -1, True, "2", float("nan"), float("inf")])
def test_independently_measured_first_token_does_not_require_complete_time(elapsed):
    result = summarize([trace(first_token=0.5, elapsed=elapsed)], [case()], repeats=1)
    assert result["status"] == "completed"
    assert result["metrics"]["agent_first_token_latency_p50_seconds"] == 0.5
    assert result["latency"]["coverage"] == 1


def test_first_token_cannot_arrive_after_complete_reply():
    result = summarize([trace(first_token=3, elapsed=2)], [case()], repeats=1)
    assert result["status"] == "incomplete"
    assert result["metrics"]["agent_first_token_latency_p50_seconds"] is None
    assert result["latency"]["measured_reply_turns"] == 0
    assert result["latency"]["unknown_reply_turns"] == 1


def test_real_zero_latency_and_complete_time_boundary_are_valid():
    result = summarize(
        [trace(first_token=0, elapsed=0), trace(repeat=2, first_token=2, elapsed=2)], [case()],
    )
    assert result["status"] == "completed"
    assert result["metrics"]["agent_first_token_latency_p50_seconds"] == 1
    assert result["latency"]["coverage"] == 1


def test_unknown_turn_origin_cannot_be_assumed_to_be_a_customer_request():
    item = trace()
    item["turns"][0].pop("origin")
    result = summarize([item], [case()], repeats=1)
    assert result["status"] == "incomplete"
    assert result["metrics"]["agent_first_token_latency_p50_seconds"] is None
    assert result["latency"]["unknown_reply_turns"] == 1
    assert result["latency"]["eligible_reply_turns"] == 0


def test_only_an_opening_has_no_response_latency_score():
    item = trace()
    item["turns"][0]["origin"] = "host_control"
    result = summarize([item], [case()], repeats=1)
    assert result["status"] == "completed"
    assert result["metrics"]["agent_first_token_latency_p50_seconds"] is None
    assert result["latency"]["eligible_reply_turns"] == 0
    assert result["latency"]["coverage"] is None


def test_scenario_and_goal_type_groups_use_their_predeclared_cases():
    result = summarize(
        [trace(score=2, elapsed=40, first_token=4),
         trace("case-2", score=5, elapsed=80, first_token=8)],
        [case(goal_type="refusal"), case("case-2", "didi_driver")], repeats=1,
    )
    assert result["metrics"]["goal_completion"] == 3.5
    for section, group in [("by_scenario", "meituan_rider"), ("by_goal_type", "refusal")]:
        assert result[section][group]["latency_version"] == LATENCY_VERSION
        assert result[section][group]["metrics"]["goal_completion"] == 2
        assert result[section][group]["metrics"]["agent_first_token_latency_p50_seconds"] == 4
        assert result[section][group]["quality"]["goal_completion"]["expected_runs"] == 1
    assert result["by_scenario"]["didi_driver"]["metrics"]["goal_completion"] == 5
    assert result["by_goal_type"]["progression"]["metrics"]["goal_completion"] == 5


def test_missing_group_run_blocks_only_its_group_and_overall_scores():
    result = summarize([trace()], [case(), case("case-2", "didi_driver")], repeats=1)
    assert result["metrics"]["goal_completion"] is None
    assert result["by_scenario"]["meituan_rider"]["metrics"]["goal_completion"] == 5
    assert result["by_scenario"]["didi_driver"]["quality"]["goal_completion"]["coverage"] == 0


def test_repeated_or_unplanned_runs_cannot_inflate_scores():
    with pytest.raises(ValueError, match="重复运行"):
        summarize([trace(), deepcopy(trace())], [case()], repeats=1)
    with pytest.raises(ValueError, match="计划外"):
        summarize([trace("unknown")], [case()], repeats=1)


def test_empty_unobserved_dialogue_keeps_quality_unknown_and_actual_usage_zero():
    missing_dialogue = trace(repeat=2)
    missing_dialogue.update(status="customer_error", turns=[], review={})
    result = summarize([trace(), missing_dialogue], [case()])
    assert result["status"] == "incomplete"
    assert result["quality"]["factual_reliability"]["unassessable_runs"] == 1
    assert result["metrics"]["average_agent_tokens"] == 15
    assert result["known_agent_tokens"] == 30
