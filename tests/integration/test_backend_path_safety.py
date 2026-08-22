"""验证场景快照和采集记录不绑定本机目录，迁移后仍可读取冻结资源。"""

import copy
import json
from dataclasses import replace
from pathlib import Path, PureWindowsPath

import pytest
import yaml

from backend.agent_harness import BackendService, load_scenario
from backend.agent_harness.rag import ingest
from backend.database import connect
from tests.paths import ROOT

SCENARIO_PATH = "backend/scenarios/didi_driver/scenario.yaml"


def test_snapshot_persists_relative_root_and_restores_legacy_location(database_config, tmp_path):
    scenario = load_scenario(ROOT / SCENARIO_PATH)
    service = BackendService(database_config, scenario)
    relative = scenario.root.relative_to(ROOT).as_posix()
    try:
        with connect(database_config) as db:
            row = db.execute(
                "SELECT root FROM snapshots WHERE hash=%s", (scenario.snapshot_hash,)
            ).fetchone()
            assert row["root"] == relative
            legacy_root = tmp_path / "previous-machine" / "growth-agent" / relative
            db.execute(
                "UPDATE snapshots SET root=%s WHERE hash=%s",
                (str(legacy_root), scenario.snapshot_hash),
            )
        frozen = BackendService.saved_scenario(database_config, scenario.snapshot_hash)
        assert frozen.root == scenario.root
        assert frozen.data == scenario.data
        assert frozen.resources == scenario.resources
        assert frozen.snapshot_hash == scenario.snapshot_hash
        with connect(database_config, read_only=True) as db:
            assert db.execute(
                "SELECT root FROM snapshots WHERE hash=%s", (scenario.snapshot_hash,)
            ).fetchone()["root"] == str(legacy_root)
    finally:
        service.close()


def test_external_scenario_snapshot_is_self_contained_without_its_original_directory(
    database_config, tmp_path
):
    original = load_scenario(ROOT / SCENARIO_PATH)
    external = replace(original, root=tmp_path / "external-scenario")
    service = BackendService(database_config, external)
    try:
        with connect(database_config, read_only=True) as db:
            assert db.execute(
                "SELECT root FROM snapshots WHERE hash=%s", (external.snapshot_hash,)
            ).fetchone()["root"] == "."
        frozen = BackendService.saved_scenario(database_config, external.snapshot_hash)
        assert frozen.root == ROOT
        assert frozen.resources == external.resources
        assert frozen.data == external.data
    finally:
        service.close()


@pytest.mark.parametrize("reference", [SCENARIO_PATH, "/growth-agent/" + SCENARIO_PATH])
def test_scenario_paths_are_resolved_from_project_in_another_working_directory(
    monkeypatch, tmp_path, reference
):
    monkeypatch.chdir(tmp_path)
    scenario = load_scenario(reference)
    assert scenario.id == "didi_driver"
    assert scenario.root == ROOT / Path(SCENARIO_PATH).parent


@pytest.mark.parametrize("windows", [False, True])
def test_scenario_resource_refs_reject_machine_absolute_paths(tmp_path, windows):
    scenario = load_scenario(ROOT / SCENARIO_PATH)
    config = copy.deepcopy(scenario.data)
    absolute = (
        str(PureWindowsPath("C:/") / "machine" / "SKILL.md")
        if windows else str(tmp_path / "SKILL.md")
    )
    config["skills"][0]["ref"] = absolute
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="ref 必须使用相对") as error:
        load_scenario(path)
    assert absolute not in str(error.value)


def test_collection_failure_keeps_error_category_without_local_directory(tmp_path, monkeypatch):
    manifest = tmp_path / "sources.yaml"
    manifest.write_text(yaml.safe_dump({
        "allowed_hosts": ["official.example"],
        "sources": [{
            "id": "rider-faq", "scenario_id": "meituan_rider",
            "title": "官方报名说明", "url": "https://official.example/faq",
        }],
    }), encoding="utf-8")
    local_file = tmp_path / "private-collection" / "response.html"

    def unavailable(*_args, **_kwargs):
        raise FileNotFoundError(2, "读取失败", str(local_file))

    monkeypatch.setattr(ingest, "_fetch", unavailable)
    output = tmp_path / "collection"
    summary = ingest.collect(manifest, output)
    catalog = (output / "catalog.json").read_text(encoding="utf-8")
    assert summary["failed"] == 1
    assert "FileNotFoundError" in summary["failures"][0]["error"]
    assert "response.html" in catalog
    assert str(tmp_path) not in json.dumps(summary)
    assert str(tmp_path) not in catalog
