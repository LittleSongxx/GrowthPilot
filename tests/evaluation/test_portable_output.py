"""验证评测产物脱敏后仍能迁移、补跑和从相对路径读写。"""

import gzip
import json
import shutil
import sys
from dataclasses import replace

import pytest

from backend import paths
from evaluation.checks import contracts, knowledge_probe
from evaluation.common.io import digest, dump
from evaluation.judges import retry
from evaluation.rag.runner import _error_record
from tests.evaluation.test_judge_retry import source_run
from tests.evaluation.test_report import dialogue_summary, render_dialogue


def test_json_and_report_redact_nested_paths_without_mutating_inputs(tmp_path):
    local_file = "/Users/teacher/work/growth-agent/backend/scenarios/example.yaml"
    payload = {"error": f"无法读取 {local_file}", "nested": [{"source": local_file}]}
    before = digest(payload)
    output = tmp_path / "result.json"
    dump(output, payload)
    saved = json.loads(output.read_text())
    assert saved["nested"][0]["source"] == "/growth-agent/backend/scenarios/example.yaml"
    assert "/Users/" not in output.read_text()
    assert digest(payload) == before
    assert _error_record(ValueError(local_file))["message"] == saved["nested"][0]["source"]

    summary = dialogue_summary()
    for quality in summary["quality"].values():
        quality["low_score_cases"][0]["reason"] = f"参考资料缺失：{local_file}"
    report = render_dialogue(tmp_path, summary)
    assert "/Users/" not in report
    assert "/growth-agent/backend/scenarios/example.yaml" in report
    assert "(traces/" in report


@pytest.mark.parametrize("filename,content", [
    ("report.md", "日志位置：/Users/teacher/work/growth-agent/.run/application.log"),
    ("judge/original.json", '{"content":"\\u002fUsers\\u002fteacher\\u002fprivate.json"}'),
    ("judge/original.json.gz", '{"content":"\\u002fUsers\\u002fteacher\\u002fprivate.json"}'),
    ("judge/raw.txt", "File C:\\Users\\teacher\\project\\output.json"),
])
def test_judge_retry_rejects_nonportable_source_before_copying(tmp_path, filename, content):
    settings, source = source_run(tmp_path)
    artifact = source / filename
    artifact.parent.mkdir(parents=True, exist_ok=True)
    raw = content.encode("utf-8")
    if artifact.suffix == ".gz":
        raw = gzip.compress(raw, mtime=0)
    artifact.write_bytes(raw)
    with pytest.raises(ValueError, match="本机绝对路径"):
        retry.run_retry(settings, "original", run_id="blocked-copy")
    assert artifact.read_bytes() == raw
    assert not (tmp_path / ".run").exists()


def test_judge_retry_reads_relocated_report_and_preserves_frozen_hashes(tmp_path, capsys):
    original_root = tmp_path / "original-project"
    settings, source = source_run(original_root)
    relocated = tmp_path / "student-project"
    shutil.copytree(original_root, relocated)
    settings = replace(settings, project_root=relocated, runtime_dir=relocated / "runtime")
    assert retry.run_retry(settings, "original", run_id="portable-preview") == 0
    published = json.loads(capsys.readouterr().out)
    assert published["path"].startswith(
        "/growth-agent/.run/evaluation-previews/agent/original-agent_"
    )
    output = paths.resolve_project_path(published["path"], relocated)
    manifest = json.loads((output / "manifest.json").read_text())
    cases = json.loads((output / "cases.json").read_text())
    assert manifest["selected_cases_sha256"] == digest(cases)
    assert (output / manifest["retry_source"]["report_path"]).is_file()
    for name in manifest["retry_source"]["files_sha256"]:
        assert (output / "source" / name).read_bytes() == (source / name).read_bytes()
    for artifact in output.rglob("*"):
        if artifact.is_file():
            assert str(tmp_path) not in artifact.read_text(encoding="utf-8")


@pytest.mark.parametrize("output_path", [
    ".run/checks/contracts.json", "/growth-agent/.run/checks/contracts.json",
])
def test_check_cli_resolves_output_from_project_root(tmp_path, monkeypatch, capsys, output_path):
    project = tmp_path / "student-project"
    monkeypatch.setattr(paths, "PROJECT_ROOT", project)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["contracts", "--output", output_path])

    async def experiments(_directory):
        return {"passed": True, "source": project / "backend/example.yaml"}

    monkeypatch.setattr(contracts, "experiments", experiments)
    contracts.main()
    output = project / ".run/checks/contracts.json"
    assert json.loads(output.read_text())["source"] == "/growth-agent/backend/example.yaml"
    assert str(tmp_path) not in capsys.readouterr().out
    assert not (tmp_path / ".run").exists()


def test_check_questions_use_project_root_from_another_working_directory(tmp_path, monkeypatch):
    project = tmp_path / "student-project"
    dataset = project / "evaluation/questions.yaml"
    dataset.parent.mkdir(parents=True)
    dataset.write_text(
        "questions:\n  - id: example\n    scenario: meituan_rider\n    question: 如何报名？\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(paths, "PROJECT_ROOT", project)
    monkeypatch.chdir(tmp_path)
    relative = knowledge_probe.load_questions("evaluation/questions.yaml")
    displayed = knowledge_probe.load_questions("/growth-agent/evaluation/questions.yaml")
    assert relative == displayed
    assert relative[0]["question"] == "如何报名？"
