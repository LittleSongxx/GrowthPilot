import copy
import re

import pytest
import yaml

from evaluation.checks.voice_review import (
    CASE_SCENARIOS,
    REVIEW_VERSION,
    VOICE_PIPELINE,
    summarize_current_acceptance,
    summarize_observations,
)


def record(samples, case=f"{REVIEW_VERSION}-D01", session_id="voice-1"):
    return {
        "session_id": session_id,
        "channel": "voice",
        "review": {
            "case_id": case,
            "review_version": REVIEW_VERSION,
            "pipeline": VOICE_PIPELINE,
            "scenario_id": CASE_SCENARIOS[case],
            "session_id": session_id,
            "human_attestation": True,
            "origin": "human_self_report",
            "reviewer": "human",
            "notes": "Observed",
            "audio_device": "headset",
            "network": "local",
            "machine": "laptop",
            "result": "pass",
            "timing": samples,
        },
    }


def test_all_current_frontend_cases_cover_four_scenarios_and_can_be_aggregated():
    from tests.paths import ROOT

    frontend = (ROOT / "frontend/src/acceptance.ts").read_text()
    version = re.search(r"const reviewVersion = '([^']+)'", frontend).group(1)
    frontend_cases = {f"{version}-{case}" for case in re.findall(r"\['([DRMF]\d{2})','", frontend)}
    assert version == REVIEW_VERSION
    assert frontend_cases == set(CASE_SCENARIOS)
    records = [record([], case, f"voice-{case}") for case in CASE_SCENARIOS]
    result = summarize_observations(records)
    assert result["accepted_human_records"] == 19
    assert result["sessions"] == {
        "didi_driver": 10,
        "meituan_rider": 2,
        "meituan_merchant": 2,
        "financial_consultation": 5,
    }
    assert result["gates"]["all_cases_observed_pass"]
    assert all(values == ["pass"] for values in result["case_results"].values())
    assert result["historical_records"] == result["rejected_records"] == []
    # 功能观测不能冒充声学测量结果。
    assert result["metrics"]["first_audio_plain"]["count"] == 0
    assert result["status"] == "incomplete_or_failed"


@pytest.mark.parametrize("old_version", [None, "dialogue-growth-v1"])
def test_other_review_versions_remain_readable_without_counting(old_version):
    historical = record([])
    historical["review"]["case_id"] = "D01"
    if old_version is None:
        historical["review"].pop("review_version")
    else:
        historical["review"]["review_version"] = old_version
    original = copy.deepcopy(historical)
    result = summarize_observations([historical, record([], session_id="current")])
    assert result["accepted_human_records"] == 1
    assert result["historical_records"] == [original]
    assert historical == original
    assert result["sessions"]["didi_driver"] == 1


def test_current_id_needs_current_version_and_matching_scenario_and_pipeline():
    wrong_version = record([])
    wrong_version["review"]["review_version"] = "dialogue-growth-v1"
    wrong_scenario = record([])
    wrong_scenario["review"]["scenario_id"] = "meituan_rider"
    wrong_pipeline = record([])
    wrong_pipeline["review"]["pipeline"] = "native-realtime"
    old_id = record([])
    old_id["review"]["case_id"] = "D01"
    result = summarize_observations([wrong_version, wrong_scenario, wrong_pipeline, old_id])
    assert result["accepted_human_records"] == 0
    assert len(result["historical_records"]) == 2
    assert len(result["rejected_records"]) == 2


def test_no_observations_keeps_unmeasured_null_and_cannot_pass():
    result = summarize_observations([])
    assert result["status"] == "incomplete_or_failed"
    assert result["metrics"]["barge_in"]["p95_ms"] is None
    assert not any(result["gates"].values())


def test_proxy_and_duplicate_samples_cannot_satisfy_voice_gate():
    acoustic = {
        "sample_id": "a1",
        "kind": "first_audio",
        "duration_ms": 1400,
        "method": "calibrated_loopback",
        "evidence_ref": "annotated-local-observation-01",
        "valid": True,
        "with_tool": False,
    }
    proxy = {**acoustic, "sample_id": "proxy", "method": "transport_audio_start", "duration_ms": 1}
    result = summarize_observations([record([acoustic, acoustic, proxy])])
    assert result["metrics"]["first_audio_plain"]["count"] == 1
    assert len(result["rejected_timing_samples"]) == 2
    assert not result["gates"]["timed_replies_30"]
    assert result["status"] != "passed"


def test_unattested_review_never_counts_as_human():
    value = record([])
    value["review"]["human_attestation"] = False
    assert summarize_observations([value])["accepted_human_records"] == 0
    value["review"]["human_attestation"] = True
    value["channel"] = "text"
    assert summarize_observations([value])["accepted_human_records"] == 0


def test_owner_signoff_never_creates_acoustic_samples_or_unobserved_scenario(tmp_path):
    policy = tmp_path / "acceptance.yaml"
    data = {
        "schema_version": "2.0",
        "voice": {
            "mode": "owner_signoff",
            "required_scenarios": ["didi_driver"],
            "observations": [
                {
                    "review_version": REVIEW_VERSION,
                    "scenario_id": "didi_driver",
                    "result": "pass",
                    "source": "user_message",
                    "statement": "司机场景已体验通过",
                    "observed_on": "2026-10-11",
                }
            ],
        },
    }
    policy.write_text(yaml.safe_dump(data))
    result = summarize_current_acceptance([], policy)
    assert result["status"] == "accepted_by_owner"
    assert result["metrics"]["first_audio_plain"]["p95_ms"] is None
    assert result["accepted_in_app_records"] == 0
    data["voice"]["required_scenarios"].append("financial_consultation")
    policy.write_text(yaml.safe_dump(data))
    assert summarize_current_acceptance([], policy)["status"] == "pending_owner_acceptance"


def test_owner_signoff_does_not_accept_model_or_missing_statements(tmp_path):
    policy = tmp_path / "acceptance.yaml"
    policy.write_text(
        yaml.safe_dump(
            {
                "voice": {
                    "mode": "owner_signoff",
                    "required_scenarios": ["didi_driver"],
                    "observations": [
                        {
                            "review_version": REVIEW_VERSION,
                            "scenario_id": "didi_driver",
                            "result": "pass",
                            "source": "model_output",
                            "statement": "通过",
                            "observed_on": "2026-10-11",
                        }
                    ],
                },
            }
        )
    )
    assert summarize_current_acceptance([], policy)["status"] == "pending_owner_acceptance"


def test_same_pipeline_historical_owner_feedback_cannot_certify_new_review(tmp_path):
    policy = tmp_path / "acceptance.yaml"
    historical = {
        "pipeline": VOICE_PIPELINE,
        "scenario_id": "didi_driver",
        "result": "pass",
        "source": "user_message",
        "statement": "旧版司机场景已体验通过",
        "observed_on": "2026-10-11",
    }
    policy.write_text(
        yaml.safe_dump(
            {
                "voice": {
                    "pipeline": VOICE_PIPELINE,
                    "review_version": REVIEW_VERSION,
                    "mode": "owner_signoff",
                    "required_scenarios": ["didi_driver"],
                    "observations": [historical],
                }
            }
        )
    )
    result = summarize_current_acceptance([record([])], policy)
    assert result["status"] == "pending_owner_acceptance"
    assert result["owner_observations"] == []
    assert result["historical_owner_observations"] == [historical]
    assert result["accepted_in_app_records"] == 1


def test_current_acceptance_remains_pending_without_human_evidence():
    from tests.paths import ROOT

    result = summarize_current_acceptance([], ROOT / "evaluation/checks/config/acceptance.yaml")
    assert result["pipeline"] == "asr-agent-tts"
    assert result["status"] == "pending_owner_acceptance"
    assert result["owner_observations"] == []
    assert result["historical_owner_observations"] == []
