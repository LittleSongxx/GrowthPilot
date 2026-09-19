"""验证教学包可独立复现、文件改动可检测，并阻止本机路径进入交付语料。"""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from backend.agent_harness.rag.corpus import CORPUS_RELATIVE_PATH, corpus_status, load_corpus

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / CORPUS_RELATIVE_PATH


@pytest.fixture
def copied_corpus(tmp_path):
    return Path(shutil.copytree(CORPUS, tmp_path / "knowledge" / "v1"))


def rewrite_catalog(root, change):
    """同步清单校验和，使测试能够覆盖清单内容的结构校验。"""
    path = root / "catalog.json"
    catalog = json.loads(path.read_text())
    change(catalog)
    content = json.dumps(catalog, ensure_ascii=False, indent=2).encode()
    path.write_bytes(content)
    (root / "catalog.sha256").write_text(hashlib.sha256(content).hexdigest() + "\n")


def test_teaching_corpus_preserves_all_four_evaluation_candidate_sets():
    chunks = load_corpus(CORPUS)
    expected = [json.loads(line) for path in (ROOT / "evaluation/datasets/corpus").glob("*.jsonl")
                for line in path.read_text().splitlines()]
    assert {chunk["id"]: chunk for chunk in chunks} == {chunk["id"]: chunk for chunk in expected}
    status = corpus_status(CORPUS)
    assert (status["version"], status["document_count"], status["chunk_count"]) == (
        "teaching-v1", 42, 1285
    )
    assert {item["scenario_id"]: item["chunk_count"] for item in status["scenarios"]} == {
        "meituan_rider": 95, "meituan_merchant": 252,
        "didi_driver": 623, "financial_consultation": 315,
    }
    assert [item["id"] for item in status["unavailable_sources"]] == [
        "didi-fuzhou-driver-licensing"
    ]
    for scenario in status["scenarios"]:
        selected = load_corpus(CORPUS, scenario["scenario_id"])
        assert len(selected) == scenario["chunk_count"]
        payloads = sorted(json.dumps(chunk, ensure_ascii=False, sort_keys=True) for chunk in selected)
        assert hashlib.sha256("\n".join(payloads).encode()).hexdigest() == scenario["version"]


def test_corpus_can_be_copied_to_another_machine_without_original_runtime(copied_corpus):
    assert corpus_status(copied_corpus) == corpus_status(CORPUS)
    assert load_corpus(copied_corpus) == load_corpus(CORPUS)
    assert not any(".run" in path.parts for path in copied_corpus.rglob("*"))


@pytest.mark.parametrize("action", ["change", "remove"])
def test_incomplete_or_changed_snapshot_fails_without_revealing_local_paths(copied_corpus, action):
    target = next(copied_corpus.rglob("clean.txt"))
    if action == "change":
        target.write_text(target.read_text() + "变更正文")
    else:
        target.unlink()
    with pytest.raises(ValueError, match="固定教学语料校验失败") as error:
        load_corpus(copied_corpus)
    assert str(copied_corpus) not in str(error.value)


def test_modified_catalog_is_rejected(copied_corpus):
    path = copied_corpus / "catalog.json"
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="catalog.json 的 SHA-256 不一致"):
        load_corpus(copied_corpus)


@pytest.mark.parametrize("relative", ["../../outside.json", "/growth-agent/outside.json"])
def test_manifest_cannot_reference_files_outside_its_package(copied_corpus, relative):
    rewrite_catalog(copied_corpus, lambda value: value["documents"][0]["files"]["chunks.json"].update(
        path=relative
    ))
    with pytest.raises(ValueError, match="文件路径必须"):
        load_corpus(copied_corpus)


def test_single_scenario_load_still_checks_complete_four_scenario_coverage(copied_corpus):
    rewrite_catalog(copied_corpus, lambda value: value["scenarios"].pop())
    with pytest.raises(ValueError, match="四个业务场景"):
        load_corpus(copied_corpus, "meituan_rider")


def test_unlisted_files_and_symbolic_links_are_rejected(copied_corpus, tmp_path):
    extra = copied_corpus / "unexpected.txt"
    extra.write_text("未登记的教学资料")
    with pytest.raises(ValueError, match="未登记文件"):
        load_corpus(copied_corpus)
    extra.unlink()
    target = next(copied_corpus.rglob("clean.txt"))
    outside = tmp_path / "outside.txt"
    outside.write_bytes(target.read_bytes())
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(ValueError, match="符号链接"):
        load_corpus(copied_corpus)


def test_private_home_path_is_rejected_even_with_matching_file_checksums(copied_corpus):
    catalog = json.loads((copied_corpus / "catalog.json").read_text())
    relative = catalog["documents"][0]["files"]["clean.txt"]["path"]
    content = b"/Users/example/private-project/growth-agent"
    (copied_corpus / relative).write_bytes(content)
    rewrite_catalog(copied_corpus, lambda value: value["documents"][0]["files"]["clean.txt"].update(
        sha256=hashlib.sha256(content).hexdigest()
    ))
    with pytest.raises(ValueError, match="本机用户目录"):
        load_corpus(copied_corpus)


def test_original_policy_validity_dates_are_preserved():
    fuzhou = [chunk for chunk in load_corpus(CORPUS)
              if chunk["document_id"] == "didi-fuzhou-rules-2023"]
    assert len(fuzhou) == 72
    assert all(chunk["valid_until"] == "2029-03-26" for chunk in fuzhou)
