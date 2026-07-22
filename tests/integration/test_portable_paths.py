"""确保可交付报告和日志保留定位线索，同时不暴露作者的本机目录。"""

import hashlib
import io
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.paths import (
    PortablePathFormatter,
    install_path_safe_logging,
    portable_data,
    portable_path,
    redact_local_paths,
)
from evaluation.common.io import dump


def test_project_paths_are_stable_between_checkouts(tmp_path):
    first = tmp_path / "author checkout" / "growth-agent"
    second = tmp_path / "student checkout" / "growth-agent"
    relative = Path("data/knowledge/catalog.json")

    assert portable_path(first / relative, first) == "/growth-agent/data/knowledge/catalog.json"
    assert portable_path(second / relative, second) == portable_path(first / relative, first)
    assert portable_path(relative, first) == portable_path(first / relative, first)
    assert portable_path(first, first) == "/growth-agent"
    assert portable_path("/growth-agent/data/knowledge/catalog.json", first) == (
        "/growth-agent/data/knowledge/catalog.json"
    )


def test_paths_from_another_machine_do_not_expose_usernames(tmp_path):
    assert portable_path("/Users/example/projects/growth-agent/data/catalog.json", tmp_path) == (
        "/growth-agent/data/catalog.json"
    )
    assert portable_path(r"C:\Users\example\growth-agent\data\catalog.json", tmp_path) == (
        "/growth-agent/data/catalog.json"
    )
    assert portable_path("/home/example/private/report.json", tmp_path) == "report.json"


def test_redaction_preserves_public_urls_and_handles_project_parent_spaces(tmp_path):
    root = tmp_path / "author checkout" / "growth-agent"
    text = (
        f'File "{root}/backend/cli.py", line 1\n'
        "外部资料 file:///Users/example/private/example.pdf\n"
        "公开网页 https://example.com/home/example/page\n"
        "相对引用 data/knowledge/catalog.json"
    )

    assert redact_local_paths(text, root) == (
        'File "/growth-agent/backend/cli.py", line 1\n'
        "外部资料 example.pdf\n"
        "公开网页 https://example.com/home/example/page\n"
        "相对引用 data/knowledge/catalog.json"
    )


def test_saved_evaluation_json_redacts_nested_paths_without_mutating_inputs(tmp_path, monkeypatch):
    from backend import paths

    root = tmp_path / "author" / "growth-agent"
    monkeypatch.setattr(paths, "PROJECT_ROOT", root)
    original = {
        "snapshots": [str(root / "data/knowledge/catalog.json")],
        "nested": {"source": root / "evaluation/datasets/corpus/meituan_rider.jsonl"},
        "url": "https://example.com/home/example/policy",
    }
    output = tmp_path / "report.json"
    dump(output, original)
    saved = json.loads(output.read_text(encoding="utf-8"))

    assert saved == portable_data(original, root)
    assert saved["snapshots"] == ["/growth-agent/data/knowledge/catalog.json"]
    assert saved["nested"]["source"] == "/growth-agent/evaluation/datasets/corpus/meituan_rider.jsonl"
    assert str(root) not in output.read_text(encoding="utf-8")
    assert original["snapshots"][0] == str(root / "data/knowledge/catalog.json")


def test_formatter_redacts_exception_frames_and_message(tmp_path, monkeypatch):
    from backend import paths

    root = tmp_path / "author checkout" / "growth-agent"
    monkeypatch.setattr(paths, "PROJECT_ROOT", root)
    def fail_in_project():
        raise ValueError("读取失败")

    fail_in_project.__code__ = fail_in_project.__code__.replace(
        co_filename=str(root / "backend/worker.py")
    )
    try:
        fail_in_project()
    except ValueError:
        record = logging.LogRecord(
            "growth", logging.ERROR, str(root / "backend/cli.py"), 1,
            "执行失败：%s", (root / "data/knowledge/catalog.json",), sys.exc_info(),
        )

    rendered = PortablePathFormatter("%(message)s").format(record)
    assert "/growth-agent/data/knowledge/catalog.json" in rendered
    assert 'File "/growth-agent/backend/worker.py"' in rendered
    assert str(root) not in rendered
    assert "ValueError: 读取失败" in rendered


def test_rag_snapshot_paths_are_portable_and_content_hash_is_unchanged(tmp_path):
    from backend.agent_harness.rag.index import _json
    from evaluation.rag.runner import _load_frozen_chunks

    root = tmp_path / "growth-agent"
    chunk = {"id": "example", "scenario_id": "meituan_rider", "text": "固定教学语料"}
    snapshot = root / "evaluation/datasets/corpus/meituan_rider.jsonl"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(json.dumps(chunk, ensure_ascii=False) + "\n", encoding="utf-8")
    version = hashlib.sha256(_json(chunk).encode()).hexdigest()

    chunks, references = _load_frozen_chunks(
        root, [{"scenario_id": "meituan_rider", "corpus_version": version}],
    )

    assert chunks == [chunk]
    assert references == ["/growth-agent/evaluation/datasets/corpus/meituan_rider.jsonl"]


def test_agentscope_logger_redacts_exception_without_changing_handler_settings(
    tmp_path, monkeypatch,
):
    from agentscope._logging import logger

    from backend import paths

    root = tmp_path / "author checkout" / "growth-agent"
    monkeypatch.setattr(paths, "PROJECT_ROOT", root)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter("%(levelname)s | %(message)s"))
    monkeypatch.setattr(logger, "handlers", [handler])
    monkeypatch.setattr(logger, "propagate", False)
    monkeypatch.setattr(logger, "level", logging.INFO)

    install_path_safe_logging()
    formatter = handler.formatter
    install_path_safe_logging()
    assert handler.formatter is formatter
    assert handler.level == logging.WARNING
    assert logger.level == logging.INFO
    assert logger.propagate is False

    def fail_in_project():
        raise ValueError("依赖库执行失败")

    fail_in_project.__code__ = fail_in_project.__code__.replace(
        co_filename=str(root / "backend/worker.py")
    )
    logger.info("此消息应被 handler 级别过滤")
    try:
        fail_in_project()
    except ValueError:
        logger.exception("AgentScope 执行失败：%s", root / "data/knowledge/catalog.json")

    rendered = stream.getvalue()
    assert rendered.startswith("ERROR | AgentScope 执行失败：/growth-agent/")
    assert 'File "/growth-agent/backend/worker.py"' in rendered
    assert "ValueError: 依赖库执行失败" in rendered
    assert str(root) not in rendered
    assert "应被 handler 级别过滤" not in rendered


def test_loguru_sink_redacts_paths_and_exception_after_formatting(tmp_path, monkeypatch, capsys):
    from loguru import logger

    from backend import paths

    root = tmp_path / "author checkout" / "growth-agent"
    monkeypatch.setattr(paths, "PROJECT_ROOT", root)
    install_path_safe_logging()
    install_path_safe_logging()
    logger.warning("模型缓存：{}", root / ".run/knowledge/models")

    def fail_in_project():
        raise ValueError("模型加载失败")

    fail_in_project.__code__ = fail_in_project.__code__.replace(
        co_filename=str(root / "backend/model.py")
    )
    try:
        fail_in_project()
    except ValueError:
        logger.exception("FastEmbed 读取失败：{}", root / ".run/knowledge/models/model.onnx")

    rendered = capsys.readouterr().err
    assert rendered.count("模型缓存：/growth-agent/.run/knowledge/models") == 1
    assert "WARNING" in rendered and "ERROR" in rendered
    assert 'File "/growth-agent/backend/model.py"' in rendered
    assert "ValueError: 模型加载失败" in rendered
    assert str(root) not in rendered


def test_evaluation_rejects_private_paths_before_freezing_inputs(tmp_path, monkeypatch):
    from evaluation import runner

    root = tmp_path / "growth-agent"
    dataset = [{
        "scenario_id": "meituan_rider",
        "version": "test-version",
        "cases": [{"id": "test-case", "question": str(root / "data/private.json")}],
    }]
    monkeypatch.setattr(runner, "load_rag_datasets", lambda *_: dataset)

    def forbidden(*_args, **_kwargs):
        pytest.fail("包含本机路径的题集不能进入冻结或执行阶段")

    monkeypatch.setattr(runner, "make_manifest", forbidden)
    with pytest.raises(ValueError, match="题集包含本机绝对路径"):
        runner.run_suite("rag", SimpleNamespace(project_root=root), run_id="private-path")

    assert not (root / "evaluation/reports").exists()
    assert not (root / ".run/evaluation-previews").exists()
    assert dataset[0]["cases"][0]["question"] == str(root / "data/private.json")
