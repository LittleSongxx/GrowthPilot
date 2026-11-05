"""用同一份统计数据生成便于学员阅读的报告。"""

from pathlib import Path

from backend.paths import redact_local_paths
from evaluation.dialogue.metrics import LATENCY_VERSION

from .io import digest, stored_json_path

SCENARIO_NAMES = {
    "meituan_rider": "美团骑手",
    "meituan_merchant": "美团商家",
    "didi_driver": "滴滴司机",
    "financial_consultation": "度小满",
}
GOAL_NAMES = {"progression": "推进下一步", "consultation": "解答咨询", "refusal": "尊重拒绝"}
QUALITY_NAMES = {
    "goal_completion": "目标完成度",
    "problem_resolution": "问题解决度",
    "factual_reliability": "事实可靠性",
    "communication_quality": "沟通体验",
}


def _number(value, *, percent=False, digits=1):
    if value is None:
        return "未判定"
    return f"{value:.1%}" if percent else f"{value:,.{digits}f}"


def _cell(value):
    """保持模型理由中的换行和竖线不会破坏报告表格。"""
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _quality_value(row, name):
    if row.get("expected_runs") and row.get("not_applicable_runs") == row["expected_runs"]:
        return "不适用"
    return _number(row.get(name), digits=2)


def _quality_rows(groups):
    rows = [
        "| 范围 | 维度 | 均分 | 观察均分 | 1／2／3／4／5 分数量 | 已评分 | 不适用 | 未知 | 覆盖率 |",
        "| --- | --- | ---: | ---: | --- | ---: | ---: | ---: | ---: |",
    ]
    for label, group in groups:
        for key, name in QUALITY_NAMES.items():
            row = group.get("quality", {}).get(key, {})
            distribution = "／".join(
                str(row.get("score_distribution", {}).get(str(score), 0))
                for score in range(1, 6)
            )
            rows.append(
                f"| {_cell(label)} | {name} | {_quality_value(row, 'mean')} | "
                f"{_quality_value(row, 'observed_mean')} | {distribution} | "
                f"{row.get('assessed_runs', 0)} | {row.get('not_applicable_runs', 0)} | "
                f"{row.get('unassessable_runs', 0)} | "
                f"{_number(row.get('coverage'), percent=True)} |"
            )
    return rows


def _engineering_rows(groups):
    rows = [
        "| 范围 | 计划通话 | 已记录 | 平均 Agent token | 首 token P50（秒） | 首 token P95（秒） |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, group in groups:
        metrics = group.get("metrics") or {}
        counts = group.get("counts", {})
        # 旧版完整回复耗时不能改标为首 token 时延，缺少当前口径时保留未知。
        latency_metrics = metrics if group.get("latency_version") == LATENCY_VERSION else {}
        rows.append(
            f"| {_cell(label)} | {counts.get('expected_runs', 0)} | "
            f"{counts.get('recorded_runs', 0)} | "
            f"{_number(metrics.get('average_agent_tokens'))} | "
            f"{_number(latency_metrics.get('agent_first_token_latency_p50_seconds'), digits=2)} | "
            f"{_number(latency_metrics.get('agent_first_token_latency_p95_seconds'), digits=2)} |"
        )
    rows += [
        "",
        "| 范围 | token 已知／未知通话 | 时延已测／缺失轮次 | 失败响应轮次 | 未完整观测通话 | 时延覆盖率 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, group in groups:
        counts = group.get("counts", {})
        latency = group.get("latency", {})
        rows.append(
            f"| {_cell(label)} | {counts.get('agent_token_known_runs', 0)}／"
            f"{counts.get('agent_token_unknown_runs', 0)} | "
            f"{latency.get('measured_reply_turns', 0)}／{latency.get('unknown_reply_turns', 0)} | "
            f"{latency.get('failed_reply_turns', 0)} | {latency.get('unobserved_runs', 0)} | "
            f"{_number(latency.get('coverage'), percent=True)} |"
        )
    return rows


def _low_score_rows(summary):
    rows = []
    for key, name in QUALITY_NAMES.items():
        for case in summary.get("quality", {}).get(key, {}).get("low_score_cases", []):
            filename = digest([case["case_id"], case["repeat"]])[:20] + ".json"
            label = f"{_cell(case['case_id'])} · 第 {case['repeat']} 通"
            rows.append(
                f"| [{label}](traces/{filename}) | {name} | {case['score']} | "
                f"{_cell(case['reason'])} |"
            )
    if not rows:
        return ["当前已评分记录中没有 1～2 分案例；未判定通话不视为高分。"]
    return ["| 案例与原始轨迹 | 维度 | 分数 | 理由 |", "| --- | --- | ---: | --- |", *rows]


def _profile_rows(groups, summary):
    """画像处理单列完整性；已有质量分和旧记录未知状态均保持原意。"""
    profile = summary.get("profile_processing")
    if not isinstance(profile, dict):
        return ["本次汇总未提供画像处理统计，无法判断是否成功；缺少统计不等于实际处理失败。"]
    rows = [
        "画像是在通话结束后，从客户原话中提取并保存的信息，供同一客户的后续通话使用。",
        "以下完整性单独统计，不修改对话质量分或整体指标状态；completed 表示已完成或无需提取。",
        "incomplete 表示观测到未完成或异常；unknown 表示缺少状态证据，不能直接认定失败。",
        "",
        "| 范围 | 画像完整性 | 完成／跳过 | 失败／收尾异常 | 超时／中断 | 无任务／其他未完成 | 缺失／未知 | 状态覆盖率 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, group in groups:
        item = group.get("profile_processing")
        if not isinstance(item, dict):
            continue
        counts = item.get("state_counts", {})
        unfinished = sum(counts.get(status, 0) for status in (
            "collecting", "pending", "running", "retrying", "cancelled",
        ))
        rows.append(
            f"| {_cell(label)} | {_cell(item.get('status', 'unknown'))} | "
            f"{counts.get('completed', 0)}／{counts.get('skipped', 0)} | "
            f"{counts.get('failed', 0)}／{counts.get('error', 0)} | "
            f"{counts.get('timeout', 0)}／{counts.get('interrupted', 0)} | "
            f"{counts.get('unavailable', 0)}／{unfinished} | "
            f"{counts.get('missing', 0)}／{counts.get('unknown', 0)} | "
            f"{_number(item.get('coverage'), percent=True)} |"
        )
    rows += [
        "",
        "跳过表示没有可提取的客户发言，属于正常处理；状态覆盖率只表示是否记录到状态，不是成功率。",
        (
            "画像未完成或状态未知时，后续通话可能未使用本通新增信息；"
            "这里提示记忆连续性风险，不代表已确认后续对话答错。"
        ),
        f"需检查的后续计划通话（去重）：{profile.get('potentially_affected_subsequent_runs', 0)} 通。",
    ]
    issues = profile.get("issues", [])
    if not issues:
        return [*rows, "当前画像状态均已确认完成或无需提取。"]
    rows += [
        "",
        "| 案例与原始轨迹 | 状态 | 原因／异常类别 | 已尝试次数 | 需检查的后续通话 |",
        "| --- | --- | --- | ---: | --- |",
    ]
    for issue in issues:
        label = f"{_cell(issue['case_id'])} · 第 {issue['repeat']} 通"
        if issue.get("trace_recorded"):
            filename = digest([issue["case_id"], issue["repeat"]])[:20] + ".json"
            label = f"[{label}](traces/{filename})"
        reason = issue.get("reason", "")
        if issue.get("error_category"):
            reason += "；类别：" + issue["error_category"]
        if issue.get("last_status"):
            reason += "；最后状态：" + issue["last_status"]
        subsequent = "、".join(f"第 {repeat} 通" for repeat in issue.get("subsequent_repeats", []))
        rows.append(
            f"| {label} | {_cell(issue.get('status', 'unknown'))} | {_cell(reason)} | "
            f"{issue.get('attempts', '未知')} | {subsequent or '无'} |"
        )
    return rows


def write_report(output, suite, summary, manifest):
    """不另外计算成绩，避免 JSON 与 Markdown 两套口径。"""
    lines = [
        "# RAG 检索评测" if suite == "rag" else "# 文本 Agent 端到端评测",
        "",
        f"运行：{Path(output).name}。状态：{summary.get('status', 'unknown')}。",
        (
            f"客户案例：{manifest['expected_cases']}；每位客户计划通话：{manifest['repeats']} 次；"
            f"计划通话：{manifest['expected_trials']} 次。"
            if suite == "dialogue"
            else f"独立案例：{manifest['expected_cases']}；计划执行：{manifest['expected_trials']} 次。"
        ),
        "",
        "数据来自公开业务资料与自建案例；模型辅助标注和模拟客户不代表真实线上客户。",
        "",
    ]
    if suite == "dialogue":
        scoring = manifest.get("scoring", {})
        prompt_link = scoring.get("prompt_snapshot", "../../judges/prompt.md")
        if manifest.get("operation") == "judge_retry":
            retry = manifest.get("judge_retry", {})
            lines += [
                "本次仅补跑失败裁判，复用原对话、通话记忆、Agent 用量、首 token 时延和画像处理状态。",
                "本次不运行客户或 Agent 对话，也不新建业务数据库或重新提取画像。",
                f"裁判请求并发上限：{retry.get('concurrency', '未记录')}。",
                (
                    f"选择补评：{retry.get('selected_trials', 0)} 通；"
                    f"补评成功：{retry.get('completed_trials', 0)} 通；"
                    f"补评失败：{retry.get('failed_trials', 0)} 通；"
                    f"沿用有效评分：{retry.get('retained_valid_trials', 0)} 通；"
                    f"缺少可补评记录：{retry.get('unavailable_trials', 0)} 通。"
                ),
                "以下按原计划汇总新旧有效评分；[源运行报告与原始证据](source/report.md)。",
            ]
        else:
            lines += [
                f"客户并发上限：{manifest['concurrency']}；同一客户的连续通话顺序执行。",
                "同一客户从第一次到最后一次通话持续占用一个并发名额。",
                "每条通话的画像提取收尾、judge 评审及资源释放均在并发名额内完成。",
                "整轮评测使用新建独立数据库；同一客户后续通话保留本轮生成的记忆，跨轮评测记忆清零。",
            ]
        lines += [
            "多次通话是同一客户的连续接触，不是相互独立的采样。",
            (
                "评测四项质量分：目标完成度、问题解决度、事实可靠性、沟通体验；"
                "工程指标：平均 Agent token 与首 token 时延（TTFT）P50／P95。"
            ),
            (
                f"评分版本：{scoring.get('version', '未记录')}；"
                f"[查看本次评分 prompt]({prompt_link})。"
            ),
            "旧版目标达成率、事实有据率不能与本版 1～5 分直接比较。",
            (
                f"首 token 时延口径：{LATENCY_VERSION}；"
                f"本次汇总时延版本：{summary.get('latency_version', '旧版或未记录')}。"
            ),
            "",
        ]
        if summary.get("status") != "preview":
            profile = summary.get("profile_processing", {})
            lines += [
                (
                    f"**画像处理完整性：{profile.get('status', 'unknown')}**；"
                    "与上述对话指标状态分开统计，异常与缺失明细见下方画像处理章节。"
                ),
                "",
            ]
        if summary.get("latency_version") != LATENCY_VERSION:
            lines += [
                (
                    "本次汇总未提供当前首 token 时延口径，首 token 指标记为未判定；"
                    "时延已测／缺失等明细沿用原汇总口径，不代表已采集 TTFT。"
                ),
                "",
            ]
    if summary.get("status") == "preview":
        lines += ["本次仅预览，没有运行检索或调用对话模型，不产生质量成绩。", ""]
    if summary.get("status") == "invalidated":
        lines += [
            "知识或代码资源未能确认一致，本轮不发布正式指标；保留原始记录供诊断。",
            "原因：" + "、".join(summary.get("invalidation_reasons", [])) + "。",
            "",
        ]
        if suite == "dialogue":
            lines += ["以下分布与低分条目仅为诊断记录，正式均分与观察均分均已撤回。", ""]
    if summary.get("status") != "preview" and suite == "rag":
        lines += [
            "| 范围 | 计划题数 | 已计分 | Recall@8 | Precision@8 | MRR@8 |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
        groups = [("全部", summary)] + [
            (SCENARIO_NAMES.get(sid, sid), group)
            for sid, group in summary.get("by_scenario", {}).items()
        ]
        for label, group in groups:
            metrics = group.get("metrics") or {}
            lines.append(
                f"| {label} | {group.get('planned', 0)} | {group.get('scored', 0)} | "
                f"{_number(metrics.get('recall_at_8'), percent=True)} | "
                f"{_number(metrics.get('precision_at_8'), percent=True)} | "
                f"{_number(metrics.get('mrr_at_8'), digits=4)} |"
            )
        lines += ["", "| k | Recall@k | Precision@k |", "| ---: | ---: | ---: |"]
        metrics = summary.get("metrics") or {}
        for k in (1, 3, 5, 8):
            lines.append(
                f"| {k} | {_number(metrics.get(f'recall_at_{k}'), percent=True)} | "
                f"{_number(metrics.get(f'precision_at_{k}'), percent=True)} |"
            )
        index = summary.get("index") or {}
        if index:
            lines += [
                "",
                (
                    f"知识索引：`{index.get('source', 'unknown')}`，namespace "
                    f"`{index.get('namespace', '')}`；复用现有索引："
                    f"{'是' if index.get('reused') else '否'}。"
                ),
            ]
        lines += [
            "",
            "按全部计划题目宏平均；执行失败保留分母。Precision@k 不足 k 条的空位按不相关处理。",
            "MRR 截止实际返回的前 8 条；无相关命中记为 0。三个指标仅评价检索。",
            f"执行异常：{summary.get('errors', 0)}；未执行：{summary.get('not_run', 0)}。",
            "",
            "[逐题结果与原始检索](rag_results.json)",
            "",
        ]
    elif summary.get("status") != "preview":
        groups = [("全部", summary)] + [
            (SCENARIO_NAMES.get(sid, sid), group)
            for sid, group in summary.get("by_scenario", {}).items()
        ]
        if summary.get("by_goal_type"):
            groups += [
                ("目标：" + GOAL_NAMES.get(kind, kind), group)
                for kind, group in summary["by_goal_type"].items()
            ]
        lines += [
            "## 对话质量（1～5 分）",
            "",
            "四个维度独立评分，不计算综合总分。未知项存在时不发布该项完整均分。",
            "观察均分只反映已评分记录；覆盖率为已评分通话数 ÷（计划通话数 − 不适用数）。",
            "不适用不计作满分；未知不计作低分，也不会悄悄排除以提高成绩。",
            "",
            *_quality_rows(groups),
            "",
            "## 资源与首 token 时延",
            "",
            *_engineering_rows(groups),
            "",
            "客户、裁判与画像提取用量单独保存；不进入平均 Agent token。",
            (
                "首 token 时延从本轮客户文本提交给 Agent 开始，"
                "到最终面向客户的回复首个非空文本块到达为止；流式文本块近似首 token。"
            ),
            (
                "此前的推理、工具执行和多次模型调用等待都计入；内部思考、工具参数、"
                "工具结果与最终被丢弃的中间说明不作为首 token。结束工具的告别文本按可用时间计。"
            ),
            (
                "排除主动开场、模拟客户生成、judge 评审和通话后画像提取；"
                "不包含 ASR／TTS，也不等于浏览器显示文字或客户听见首音的延迟。"
            ),
            "完整回复耗时仍保存在 trace 的 elapsed_seconds 中，仅供诊断，不替代首 token 时延。",
            "失败响应与缺失记录单独列出，不当作零时延。",
            "",
            "## 画像处理与连续通话记忆",
            "",
            *_profile_rows(groups, summary),
            "",
            "## 低分案例（1～2 分）",
            "",
            *_low_score_rows(summary),
            "",
            f"[完整对话与调用事件]({stored_json_path(Path(output) / 'traces.json').name})",
            "",
        ]
    lines += [
        "[机器可读统计](summary.json) · [冻结运行配置](manifest.json) · [本次案例](cases.json)",
        "",
    ]
    (Path(output) / "report.md").write_text(redact_local_paths("\n".join(lines)), encoding="utf-8")
