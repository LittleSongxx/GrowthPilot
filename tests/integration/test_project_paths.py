"""从任意工作目录启动时，项目资源仍按代码与配置所在位置解析。"""

from types import SimpleNamespace

import pytest

from backend.cli import scenario_path
from backend.paths import resolve_project_path


def test_relative_and_display_paths_resolve_after_relocation(tmp_path, monkeypatch):
    project = tmp_path / "student-copy"
    other = tmp_path / "unrelated"
    project.mkdir()
    other.mkdir()
    monkeypatch.chdir(other)

    expected = project / "backend/scenarios/meituan_rider/scenario.yaml"
    assert resolve_project_path("backend/scenarios/meituan_rider/scenario.yaml", project) == expected
    assert resolve_project_path("/growth-agent/backend/scenarios/meituan_rider/scenario.yaml", project) == expected
    assert resolve_project_path("/growth-agent", project) == project
    assert resolve_project_path(other / "explicit-custom.yaml", project) == other / "explicit-custom.yaml"


def test_display_path_cannot_escape_through_parent_or_symlink(tmp_path):
    project = tmp_path / "project"
    external = tmp_path / "external"
    project.mkdir()
    external.mkdir()
    (project / "linked").symlink_to(external, target_is_directory=True)
    for value in ("/growth-agent/../external/file.json", "/growth-agent/linked/file.json"):
        with pytest.raises(ValueError, match="不能超出项目目录"):
            resolve_project_path(value, project)


def test_scenario_selection_does_not_use_same_named_file_in_current_directory(tmp_path, monkeypatch):
    project = tmp_path / "project"
    other = tmp_path / "unrelated"
    relative = "backend/scenarios/meituan_rider/scenario.yaml"
    for folder in (project, other):
        entry = folder / relative
        entry.parent.mkdir(parents=True)
        entry.write_text("{}", encoding="utf-8")
    monkeypatch.chdir(other)
    settings = SimpleNamespace(project_root=project)
    assert scenario_path(settings, relative) == project / relative
    assert scenario_path(settings, "/growth-agent/" + relative) == project / relative
    assert scenario_path(settings, "meituan_rider") == project / relative


def test_relative_project_override_is_based_on_code_root(tmp_path, monkeypatch):
    from backend import settings as settings_module
    from tests.paths import ROOT

    code_root = tmp_path / "code"
    configured = code_root / "student-config"
    directory = configured / "backend/config"
    directory.mkdir(parents=True)
    for name in ("models.yaml", "app.yaml"):
        (directory / name).write_bytes((ROOT / "backend/config" / name).read_bytes())
    monkeypatch.setattr(settings_module, "PROJECT_ROOT", code_root)
    monkeypatch.setattr(settings_module, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("GROWTH_PROJECT_ROOT", "student-config")
    monkeypatch.setenv("GROWTH_RUNTIME_DIR", ".run")
    monkeypatch.chdir(tmp_path)

    settings = settings_module.Settings.from_env()
    assert settings.project_root == configured
    assert settings.runtime_dir == configured / ".run"
