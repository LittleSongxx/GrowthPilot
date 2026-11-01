"""验证报告忠实展示四维评分、缺测与低分证据，不把诊断结果包装成完整成绩。"""

from copy import deepcopy

from evaluation.common.io import digest
from evaluation.common.report import QUALITY_NAMES, write_report
from evaluation.dialogue.metrics import LATENCY_VERSION
from evaluation.judges.schema import JUDGE_VERSION
from evaluation.runner import _invalidate_summary


def dialogue_summary():
    """构造一组不完整但具有实际评分和时延记录的汇总。"""
    quality = {
        key: {
            "mean": None, "observed_mean": 2,
            "score_distribution": {"1": 0, "2": 1, "3": 0, "4": 0, "5": 0},
            "assessed_runs": 1, "not_applicable_runs": 0, "unassessable_runs": 1,
            "expected_runs": 2, "applicable_runs": 2, "coverage": 0.5,
            "low_score_cases": [{
                "case_id": "rider-one", "repeat": 1, "score": 2,
                "reason": "漏答费用 | 仍需核实\n没有下一步",
            }],
        }
        for key in QUALITY_NAMES
    }
    return {
        "status": "incomplete",
        "latency_version": LATENCY_VERSION,
        "metrics": {
            **dict.fromkeys(QUALITY_NAMES), "average_agent_tokens": 202,
            "agent_first_token_latency_p50_seconds": 1.25,
            "agent_first_token_latency_p95_seconds": 2.5,
        },
        "quality": quality,
        "counts": {
            "expected_runs": 2, "recorded_runs": 2,
            "agent_token_known_runs": 2, "agent_token_unknown_runs": 0,
        },
        "latency": {
            "measured_reply_turns": 3, "unknown_reply_turns": 0,
            "failed_reply_turns": 1, "unobserved_runs": 0, "coverage": 1,
            "observed_p50_seconds": 1.25, "observed_p95_seconds": 2.5,
        },
    }


def render_dialogue(tmp_path, summary):
    manifest = {
        "expected_cases": 1, "repeats": 2, "expected_trials": 2,
        "concurrency": 16,
        "scoring": {"version": JUDGE_VERSION, "prompt_snapshot": "judge_prompt.md"},
    }
    write_report(tmp_path, "dialogue", summary, manifest)
    return (tmp_path / "report.md").read_text()


def test_dialogue_report_displays_six_metrics_with_coverage_and_clickable_low_scores(tmp_path):
    summary = dialogue_summary()
    summary["by_scenario"] = {"meituan_rider": deepcopy(summary)}
    summary["by_goal_type"] = {"progression": deepcopy(summary)}
    report = render_dialogue(tmp_path, summary)
    for name in QUALITY_NAMES.values():
        assert f"| 全部 | {name} | 未判定 | 2.00 | 0／1／0／0／0 | 1 | 0 | 1 | 50.0% |" in report
        assert f"| 美团骑手 | {name} |" in report
        assert f"| 目标：推进下一步 | {name} |" in report
    assert "| 全部 | 2 | 2 | 202.0 | 1.25 | 2.50 |" in report
    assert "| 全部 | 2／0 | 3／0 | 1 | 0 | 100.0% |" in report
    assert "首 token P50（秒） | 首 token P95（秒）" in report
    assert f"本次汇总时延版本：{LATENCY_VERSION}" in report
    assert "流式文本块近似首 token" in report
    assert "elapsed_seconds" in report and "仅供诊断" in report
    trace_name = digest(["rider-one", 1])[:20] + ".json"
    assert f"[rider-one · 第 1 通](traces/{trace_name})" in report
    assert "漏答费用 \\| 仍需核实 没有下一步" in report
    assert "[查看本次评分 prompt](judge_prompt.md)" in report
    assert "../../judges/prompt.md" not in report
    assert "| 目标达成率 |" not in report and "| 事实有据率 |" not in report


def test_legacy_response_latency_is_not_rendered_as_first_token_latency(tmp_path):
    summary = dialogue_summary()
    summary.pop("latency_version")
    summary["metrics"].pop("agent_first_token_latency_p50_seconds")
    summary["metrics"].pop("agent_first_token_latency_p95_seconds")
    summary["metrics"].update(
        agent_response_latency_p50_seconds=2.32,
        agent_response_latency_p95_seconds=9.58,
    )
    report = render_dialogue(tmp_path, summary)
    assert "| 全部 | 2 | 2 | 202.0 | 未判定 | 未判定 |" in report
    assert "2.32" not in report and "9.58" not in report
    assert "本次汇总时延版本：旧版或未记录" in report
    assert "明细沿用原汇总口径，不代表已采集 TTFT" in report
    assert "| 全部 | 2／0 | 3／0 | 1 | 0 | 100.0% |" in report


def test_first_token_metrics_require_matching_latency_version(tmp_path):
    summary = dialogue_summary()
    summary["latency_version"] = "unknown-version"
    report = render_dialogue(tmp_path, summary)
    assert "| 全部 | 2 | 2 | 202.0 | 未判定 | 未判定 |" in report
    assert "本次汇总时延版本：unknown-version" in report


def test_all_not_applicable_quality_is_not_rendered_as_full_score(tmp_path):
    summary = dialogue_summary()
    summary["quality"]["factual_reliability"].update(
        mean=None, observed_mean=None,
        score_distribution={str(value): 0 for value in range(1, 6)},
        assessed_runs=0, not_applicable_runs=2, unassessable_runs=0,
        applicable_runs=0, coverage=None, low_score_cases=[],
    )
    report = render_dialogue(tmp_path, summary)
    assert "| 全部 | 事实可靠性 | 不适用 | 不适用 | 0／0／0／0／0 | 0 | 2 | 0 | 未判定 |" in report


def test_invalidated_report_withdraws_means_but_keeps_diagnostic_evidence(tmp_path):
    summary = dialogue_summary()
    _invalidate_summary(summary, ["source_resources_changed"])
    report = render_dialogue(tmp_path, summary)
    assert "不发布正式指标" in report and "正式均分与观察均分均已撤回" in report
    assert "| 全部 | 目标完成度 | 未判定 | 未判定 | 0／1／0／0／0 |" in report
    assert "| 全部 | 2 | 2 | 未判定 | 未判定 | 未判定 |" in report
    assert "漏答费用" in report and "traces/" in report


def test_rag_report_preserves_retrieval_metrics_and_has_no_dialogue_rubric(tmp_path):
    summary = {
        "status": "completed", "planned": 1, "scored": 1,
        "metrics": {"recall_at_8": 1, "precision_at_8": 0.25, "mrr_at_8": 0.5},
    }
    write_report(tmp_path, "rag", summary, {"expected_cases": 1, "expected_trials": 1})
    report = (tmp_path / "report.md").read_text()
    assert "| 全部 | 1 | 1 | 100.0% | 25.0% | 0.5000 |" in report
    assert "对话质量" not in report and "评分版本" not in report


def test_profile_report_separates_missing_observations_from_real_failures(tmp_path):
    summary = dialogue_summary()
    summary["profile_processing"] = {
        "status": "incomplete", "coverage": 0.5,
        "state_counts": {"failed": 1, "missing": 1},
        "potentially_affected_subsequent_runs": 1,
        "issues": [
            {"case_id": "rider-one", "repeat": 1, "status": "failed",
             "reason": "画像提取或写入已失败", "error_category": "ValidationError", "attempts": 3,
             "trace_recorded": True, "subsequent_repeats": [2]},
            {"case_id": "rider-one", "repeat": 2, "status": "missing",
             "reason": "未采集画像处理状态，无法判断是否成功",
             "trace_recorded": False, "subsequent_repeats": []},
        ],
    }
    summary["by_scenario"] = {"meituan_rider": deepcopy(summary)}
    report = render_dialogue(tmp_path, summary)
    assert "**画像处理完整性：incomplete**" in report
    assert "| 全部 | incomplete | 0／0 | 1／0 | 0／0 | 0／0 | 1／0 | 50.0% |" in report
    assert "| 美团骑手 | incomplete |" in report
    assert "不修改对话质量分或整体指标状态" in report
    assert "类别：ValidationError | 3 | 第 2 通 |" in report
    first = digest(["rider-one", 1])[:20] + ".json"
    second = digest(["rider-one", 2])[:20] + ".json"
    assert f"[rider-one · 第 1 通](traces/{first}) | failed |" in report
    assert f"traces/{second}" not in report
    assert "| rider-one · 第 2 通 | missing | 未采集画像处理状态，无法判断是否成功" in report
    assert "需检查的后续计划通话（去重）：1 通" in report


def test_legacy_profile_summary_is_unknown_instead_of_failed(tmp_path):
    report = render_dialogue(tmp_path, dialogue_summary())
    assert "**画像处理完整性：unknown**" in report
    assert "缺少统计不等于实际处理失败" in report


def test_judge_retry_report_describes_reused_agent_measurements(tmp_path):
    summary = dialogue_summary()
    write_report(tmp_path, "dialogue", summary, {
        "operation": "judge_retry", "expected_cases": 1, "repeats": 2, "expected_trials": 2,
        "concurrency": 16, "scoring": {"version": JUDGE_VERSION},
        "judge_retry": {
            "concurrency": 3, "selected_trials": 1, "completed_trials": 1,
            "failed_trials": 0, "retained_valid_trials": 1, "unavailable_trials": 0,
        },
    })
    report = (tmp_path / "report.md").read_text()
    assert "裁判请求并发上限：3" in report
    assert "选择补评：1 通；补评成功：1 通；补评失败：0 通；沿用有效评分：1 通" in report
    assert "复用原对话、通话记忆、Agent 用量、首 token 时延和画像处理状态" in report
    assert "[源运行报告与原始证据](source/report.md)" in report
    assert "客户并发上限" not in report
    assert "整轮评测使用新建独立数据库" not in report
