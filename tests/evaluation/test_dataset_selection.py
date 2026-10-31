"""验证按场景选择和隐藏评分数据读取，不执行模型请求。"""

import json
from pathlib import Path

import pytest

from evaluation.common.datasets import load_dialogue_datasets, load_rag_datasets, select_cases

ROOT = Path(__file__).resolve().parents[2]


def test_selection_preserves_versions_and_limits_each_scenario():
    datasets = [
        {
            "scenario_id": scenario,
            "corpus_version": scenario + "-v1",
            "cases": [{"id": scenario + "-1"}, {"id": scenario + "-2"}],
        }
        for scenario in ("one", "two")
    ]
    cases = select_cases(datasets, limit=1)
    assert [case["id"] for case in cases] == ["one-1", "two-1"]
    assert [case["corpus_version"] for case in cases] == ["one-v1", "two-v1"]
    assert "scenario_id" not in datasets[0]["cases"][0]
    with pytest.raises(ValueError):
        select_cases(datasets, case_ids=["missing"])
    with pytest.raises(ValueError):
        select_cases(datasets, case_ids=["one-1", "one-1"])
    with pytest.raises(ValueError):
        select_cases(datasets, limit=0)


def test_rag_requires_reference_for_every_relevant_chunk(tmp_path):
    folder = tmp_path / "evaluation/datasets/rag"
    folder.mkdir(parents=True)
    data = {
        "version": "rag-v1",
        "scenario_id": "meituan_rider",
        "corpus_version": "v1",
        "cases": [
            {
                "id": "rider-1",
                "topic": "报名",
                "question": "如何报名？",
                "question_family": "报名入口",
                "relevant_chunk_ids": ["one", "two"],
                "evidence": [
                    {
                        "chunk_id": "one",
                        "document_id": "source",
                        "content_hash": "hash",
                        "quote": "原文",
                    }
                ],
            }
        ],
    }
    (folder / "meituan_rider.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="逐项对应"):
        load_rag_datasets(tmp_path, "meituan_rider")


def test_dynamic_dataset_rejects_scripted_messages(tmp_path):
    folder = tmp_path / "evaluation/datasets/dialogue"
    folder.mkdir(parents=True)
    data = {
        "version": "dialogue-v3",
        "scenario_id": "meituan_rider",
        "cases": [{"id": "rider-1", "topic": "报名", "messages": ["固定下一句"]}],
    }
    (folder / "meituan_rider.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="固定客户台词"):
        load_dialogue_datasets(tmp_path, "meituan_rider")


def test_published_dataset_counts_and_unique_questions():
    for datasets in (load_rag_datasets(ROOT), load_dialogue_datasets(ROOT)):
        assert len(datasets) == 4
        assert all(len(dataset["cases"]) == 100 for dataset in datasets)
        assert len(select_cases(datasets)) == 400
    for dataset in load_rag_datasets(ROOT):
        assert len({case["question"].strip() for case in dataset["cases"]}) == 100
        assert len({case["question_family"] for case in dataset["cases"]}) >= 80
