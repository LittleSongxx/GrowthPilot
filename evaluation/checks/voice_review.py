"""以只读方式汇总经人工明确确认的语音观测记录。

传输时间戳和合成语音探针结果不能用于判定声学指标达标。
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path

import yaml

from backend.database import connect, table_exists
from backend.paths import portable_data, resolve_project_path
from backend.settings import Settings

REVIEW_VERSION = "dialogue-growth-v2"
VOICE_PIPELINE = "asr-agent-tts"
LEGACY_CASE_SCENARIOS = {
    **{f"D{i:02d}": "didi_driver" for i in range(1, 11)},
    **{f"F{i:02d}": "financial_consultation" for i in range(1, 6)},
}
CASE_SCENARIOS = {
    f"{REVIEW_VERSION}-{case}": scenario
    for case, scenario in {
        **{f"D{i:02d}": "didi_driver" for i in range(1, 11)},
        **{f"R{i:02d}": "meituan_rider" for i in range(1, 3)},
        **{f"M{i:02d}": "meituan_merchant" for i in range(1, 3)},
        **{f"F{i:02d}": "financial_consultation" for i in range(1, 6)},
    }.items()
}
ACOUSTIC_METHODS = {"calibrated_loopback", "annotated_recording", "live_acoustic_measurement"}


def percentile(values, quantile):
    if not values:
        return None
    return sorted(values)[math.ceil(quantile * len(values)) - 1]


def summarize_observations(records):
    # 修订记录保留在来源审计中，但不能增加样本或会话计数。
    latest = {}
    rejected = []
    historical = []
    for item in records:
        data = item["review"]
        case = data.get("case_id")
        if data.get("review_version") != REVIEW_VERSION or case in LEGACY_CASE_SCENARIOS:
            # 保留原始观测，不将其重新解释为当前版本的用例。
            historical.append(item)
            continue
        if (
            case not in CASE_SCENARIOS
            or data.get("scenario_id") != CASE_SCENARIOS[case]
            or data.get("pipeline") != VOICE_PIPELINE
            or data.get("session_id") != item["session_id"]
            or item.get("channel") != "voice"
            or data.get("human_attestation") is not True
            or data.get("origin") != "human_self_report"
            or not all(
                str(data.get(k, "")).strip()
                for k in ("reviewer", "notes", "audio_device", "network", "machine")
            )
        ):
            rejected.append(
                {"session_id": item["session_id"], "reason": "incomplete_or_unbound_human_record"}
            )
            continue
        latest[(item["session_id"], case)] = data
    reviews = list(latest.values())
    groups = {"first_audio_plain": [], "first_audio_tool": [], "barge_in": [], "stop_button": []}
    failures = Counter()
    rejected_samples = []
    seen = set()
    for record in reviews:
        for sample in record.get("timing", []):
            key = (record["session_id"], sample.get("sample_id"))
            duration = sample.get("duration_ms")
            kind = sample.get("kind")
            reason = None
            if not key[1] or key in seen:
                reason = "missing_or_duplicate_sample_id"
            elif sample.get("method") not in ACOUSTIC_METHODS or not sample.get("evidence_ref"):
                reason = "missing_acoustic_evidence_or_proxy_only"
            elif kind not in {"first_audio", "barge_in", "stop_button"}:
                reason = "unknown_timing_kind"
            elif sample.get("valid") is not True:
                failures[kind + ("_timeout" if sample.get("timeout") else "_failed")] += 1
            elif (
                isinstance(duration, bool)
                or not isinstance(duration, (int, float))
                or not math.isfinite(duration)
                or duration < 0
            ):
                reason = "invalid_duration"
            elif kind == "first_audio" and type(sample.get("with_tool")) is not bool:
                reason = "tool_classification_required"
            else:
                group = (
                    ("first_audio_tool" if sample["with_tool"] else "first_audio_plain")
                    if kind == "first_audio"
                    else kind
                )
                groups[group].append(duration)
            if reason:
                rejected_samples.append(
                    {"session_id": key[0], "sample_id": key[1], "reason": reason}
                )
            else:
                seen.add(key)
    metrics = {
        key: {
            "count": len(values),
            "p50_ms": percentile(values, 0.5),
            "p95_ms": percentile(values, 0.95),
        }
        for key, values in groups.items()
    }
    passed_cases = {r["case_id"] for r in reviews if r.get("result") == "pass"}
    sessions = {
        scenario: len({r["session_id"] for r in reviews if r["scenario_id"] == scenario})
        for scenario in set(CASE_SCENARIOS.values())
    }
    # 同一会话可覆盖多个用例，但在 10/5 的验收数量要求中只能计为一次会话。
    timing_count = sum(len(groups[k]) for k in ("first_audio_plain", "first_audio_tool"))
    gates = {
        "all_cases_observed_pass": passed_cases == set(CASE_SCENARIOS)
        and all(r.get("result") == "pass" for r in reviews),
        "driver_sessions_10": sessions["didi_driver"] >= 10,
        "finance_sessions_5": sessions["financial_consultation"] >= 5,
        "timed_replies_30": timing_count >= 30,
        "active_interruptions_20": len(groups["barge_in"]) >= 20,
        "first_audio_thresholds": bool(groups["first_audio_plain"])
        and all(
            percentile(values, 0.5) <= 1500 and percentile(values, 0.95) <= 3000
            for key, values in groups.items()
            if key.startswith("first_audio") and values
        ),
        "barge_in_threshold": bool(groups["barge_in"])
        and percentile(groups["barge_in"], 0.95) <= 800,
        "stop_button_threshold": bool(groups["stop_button"])
        and percentile(groups["stop_button"], 0.95) <= 300,
    }
    return {
        "kind": "human_voice_acceptance",
        "review_version": REVIEW_VERSION,
        "pipeline": VOICE_PIPELINE,
        "status": "passed" if all(gates.values()) else "incomplete_or_failed",
        "gates": gates,
        "sessions": sessions,
        "case_results": {
            case: [r.get("result") for r in reviews if r["case_id"] == case]
            for case in CASE_SCENARIOS
        },
        "metrics": metrics,
        "failures_and_timeouts": dict(failures),
        "rejected_records": rejected,
        "historical_records": historical,
        "rejected_timing_samples": rejected_samples,
        "accepted_human_records": len(reviews),
        "measurement": "nearest-rank percentiles; only explicitly attested acoustic evidence; tool replies separated",
        "limitations": [
            "人工自述仍须核对原始证据与业务轨迹",
            "代理时标与合成语音不计入通过线",
            "不外推生产表现",
        ],
    }


def load_observations(database):
    """只读现有观测，不在验收读取过程中创建表或修改业务数据。"""
    with connect(database, read_only=True) as db:
        # 不存在的目标 schema 不能回退读取 public 中的同名表。
        exists = db.execute(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
            (database.schema,),
        ).fetchone()
        if not exists:
            return []
        if not table_exists(db, "runtime_events") or not table_exists(db, "runtime_sessions"):
            return []
        rows = db.execute(
            "SELECT e.session_id,e.data_json,s.channel FROM runtime_events e JOIN runtime_sessions s USING(session_id) WHERE e.type='client_telemetry' ORDER BY e.seq"
        ).fetchall()
        records = []
        for row in rows:
            payload = json.loads(row["data_json"])
            if payload.get("event") == "human_voice_review":
                records.append(
                    {
                        "session_id": row["session_id"],
                        "channel": row["channel"],
                        "review": payload["data"],
                    }
                )
        return records


def summarize_current_acceptance(records, policy_path):
    """应用负责人当前确定的验收范围，不虚构计时证据。"""
    policy_path = resolve_project_path(policy_path)
    if not policy_path.exists():
        return summarize_observations(records)
    policy = yaml.safe_load(policy_path.read_text())
    voice = policy.get("voice", {})
    pipeline = voice.get("pipeline")
    measured = summarize_observations(records)
    if voice.get("mode") != "owner_signoff":
        return measured
    required = voice.get("required_scenarios", [])
    valid = [
        row
        for row in voice.get("observations", [])
        if (not pipeline or row.get("pipeline") == pipeline)
        and row.get("review_version") == REVIEW_VERSION
        and voice.get("review_version", REVIEW_VERSION) == REVIEW_VERSION
        and row.get("source") == "user_message"
        and str(row.get("statement", "")).strip()
        and str(row.get("observed_on", "")).strip()
        and row.get("scenario_id") in set(CASE_SCENARIOS.values())
        and row.get("result") in {"pass", "fail"}
    ]
    latest = {row["scenario_id"]: row for row in valid}
    gates = {scenario: latest.get(scenario, {}).get("result") == "pass" for scenario in required}
    accepted = bool(gates) and all(gates.values())
    return {
        "kind": "human_voice_acceptance",
        "acceptance_policy": policy.get("schema_version"),
        "review_version": REVIEW_VERSION,
        "mode": "owner_signoff",
        "status": "accepted_by_owner" if accepted else "pending_owner_acceptance",
        "gates": gates,
        "pipeline": pipeline,
        "owner_observations": valid,
        "historical_owner_observations": [
            r for r in voice.get("observations", []) if r not in valid
        ],
        "fixed_session_count_required": False,
        "acoustic_measurements_required": False,
        "metrics": measured["metrics"],
        "sessions": measured["sessions"],
        "case_results": measured["case_results"],
        "accepted_in_app_records": measured["accepted_human_records"],
        "failures_and_timeouts": measured["failures_and_timeouts"],
        "rejected_records": measured["rejected_records"],
        "historical_records": measured["historical_records"],
        "rejected_timing_samples": measured["rejected_timing_samples"],
        "limitations": [
            "用户反馈记录的是其已体验的场景，不扩展为所有场景均已人工通过",
            "用户未提供精确会话、设备和声学测量，相关字段保持null",
            "工具范围、知识依据和自动回归仍独立验证",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description="只读汇总新项目人工语音验收")
    parser.add_argument("--schema", help="读取指定 PostgreSQL schema；连接凭据从环境变量获取")
    parser.add_argument("--output", type=resolve_project_path)
    parser.add_argument("--policy", type=resolve_project_path)
    args = parser.parse_args()
    database = Settings.from_env().app_db
    if args.schema:
        from dataclasses import replace

        database = replace(database, schema=args.schema)
    policy_path = (
        args.policy or Path(__file__).parent / "config/acceptance.yaml"
    )
    result = summarize_current_acceptance(load_observations(database), policy_path)
    text = json.dumps(portable_data(result), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        if args.output.exists():
            raise ValueError("报告已存在，请为新快照选择新名称")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
