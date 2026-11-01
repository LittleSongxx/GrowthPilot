"""验证正式报告分类、模型命名及新旧运行的安全定位。"""

import pytest

from evaluation.common.paths import create_run_dir, report_files, resolve_run_dir


def test_agent_runs_use_model_and_beijing_time_without_overwriting(tmp_path):
    options = {"model": "vendor/Qwen3.5:latest", "execute": True,
               "run_id": "comparison", "created_at": "2026-11-02T12:25:06+00:00"}
    first = create_run_dir(tmp_path, "dialogue", **options)
    (first / "traces.json").write_text("原始记录", encoding="utf-8")
    second = create_run_dir(tmp_path, "dialogue", **options)
    assert first == tmp_path / "evaluation/reports/agent/vendor-Qwen3.5-latest_2026-11-02_20-25-06"
    assert second.name == first.name + "_2"
    assert (first / "traces.json").read_text(encoding="utf-8") == "原始记录"


@pytest.mark.parametrize("model", ["../模型/名字", "x" * 300, "...", "vendor/model"])
def test_model_name_stays_in_category_and_supports_long_identifiers(tmp_path, model):
    output = create_run_dir(tmp_path, "dialogue", model=model, execute=True)
    assert output.parent == tmp_path / "evaluation/reports/agent"
    assert len(output.name) <= 150
    assert resolve_run_dir(tmp_path, f"agent/{output.name}") == output


def test_only_real_evaluations_write_reports(tmp_path):
    preview = create_run_dir(tmp_path, "dialogue", model="agent", execute=False)
    rag_preview = create_run_dir(tmp_path, "rag", model="embedding", execute=False, run_id="preview")
    check = create_run_dir(tmp_path, "contracts", model="", execute=False, run_id="contracts-check")
    assert preview.parent == tmp_path / ".run/evaluation-previews/agent"
    assert rag_preview == tmp_path / ".run/evaluation-previews/rag/preview"
    assert check == tmp_path / ".run/checks/contracts-check"
    assert not (tmp_path / "evaluation/reports").exists()


def test_rag_runs_keep_explicit_name_or_default_to_embedding_model(tmp_path):
    explicit = create_run_dir(tmp_path, "rag", model="BAAI/bge-small", execute=True, run_id="baseline")
    assert explicit == tmp_path / "evaluation/reports/rag/baseline"
    with pytest.raises(FileExistsError):
        create_run_dir(tmp_path, "rag", model="unused", execute=True, run_id="baseline")
    generated = create_run_dir(tmp_path, "rag", model="BAAI/bge-small", execute=True)
    assert generated.name.startswith("BAAI-bge-small_")


@pytest.mark.parametrize("prefix", ["evaluation/reports/agent", "evaluation/reports/rag",
                                    "evaluation/reports", "docs/reports"])
@pytest.mark.parametrize("name", ["model.v1_2026-11-02", "_old-run", "-old-run"])
def test_readers_accept_categorized_and_legacy_reports(tmp_path, prefix, name):
    folder = tmp_path / prefix / name
    folder.mkdir(parents=True)
    assert resolve_run_dir(tmp_path, folder.name) == folder
    if prefix.endswith(("agent", "rag")):
        assert resolve_run_dir(tmp_path, f"{folder.parent.name}/{folder.name}") == folder


def test_ambiguous_names_require_explicit_category(tmp_path):
    for prefix in ("agent", "rag"):
        (tmp_path / "evaluation/reports" / prefix / "same").mkdir(parents=True)
    with pytest.raises(ValueError, match="多个目录"):
        resolve_run_dir(tmp_path, "same")
    assert resolve_run_dir(tmp_path, "agent/same").parent.name == "agent"
    assert resolve_run_dir(tmp_path, "same", category="agent").parent.name == "agent"
    with pytest.raises(ValueError, match="分类不匹配"):
        resolve_run_dir(tmp_path, "rag/same", category="agent")


@pytest.mark.parametrize("reference", ["../outside", "/absolute", "agent/../outside", "agent/..",
                                      "agent//name", "other/name", "agent/name/extra", "", None])
def test_readers_reject_paths_outside_a_single_run(tmp_path, reference):
    with pytest.raises(ValueError):
        resolve_run_dir(tmp_path, reference)


def test_readers_and_writers_reject_symlinked_category(tmp_path):
    root = tmp_path / "evaluation/reports"
    root.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    (outside / "existing").mkdir(parents=True)
    (root / "agent").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="符号链接"):
        resolve_run_dir(tmp_path, "agent/existing")
    with pytest.raises(ValueError, match="符号链接"):
        create_run_dir(tmp_path, "dialogue", model="agent", execute=True)
    assert sorted(item.name for item in outside.iterdir()) == ["existing"]


def test_report_discovery_includes_categories_and_legacy_but_not_checks(tmp_path):
    root = tmp_path / "evaluation/reports"
    expected = [root / prefix / "summary.json" for prefix in ("agent/a", "rag/b", "old")]
    for path in expected + [root / "agent/a/checks/engineering/summary.json"]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    assert report_files(tmp_path, "summary.json") == sorted(expected, reverse=True)

