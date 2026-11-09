"""评测命令离线解析并发参数，确认默认预览不会请求模型。"""

import pytest

from backend import cli
from evaluation import runner


@pytest.mark.parametrize("options, expected", [([], 16), (["--concurrency", "4"], 4)])
def test_cli_forwards_dialogue_concurrency_with_preview_default(monkeypatch, options, expected):
    settings = object()
    captured = {}

    def run_suite(suite, actual_settings, **kwargs):
        assert suite == "dialogue" and actual_settings is settings
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(runner, "run_suite", run_suite)
    monkeypatch.setattr(
        "sys.argv", ["growthpilot", "evaluate", "--suite", "dialogue", "--run", "preview", *options]
    )
    with pytest.raises(SystemExit) as raised:
        cli.main()
    assert raised.value.code == 0
    assert captured["concurrency"] == expected
    assert captured["execute"] is False and captured["repeats"] == 2


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "sixteen"])
def test_cli_rejects_invalid_concurrency_before_loading_settings(monkeypatch, value, capsys):
    def forbidden():
        raise AssertionError("参数校验前不应加载运行资源")

    monkeypatch.setattr(cli.Settings, "from_env", forbidden)
    monkeypatch.setattr(
        "sys.argv",
        ["growthpilot", "evaluate", "--suite", "dialogue", "--run", "preview", "--concurrency", value],
    )
    with pytest.raises(SystemExit) as raised:
        cli.main()
    assert raised.value.code == 2
    assert "必须是正整数" in capsys.readouterr().err


def test_cli_forwards_judge_retry_and_defaults_to_preview(monkeypatch):
    from evaluation.judges import retry

    settings, captured = object(), {}

    def run_retry(actual_settings, source_run, **kwargs):
        assert actual_settings is settings and source_run == "original"
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(retry, "run_retry", run_retry)
    monkeypatch.setattr("sys.argv", [
        "growth", "evaluate", "--suite", "dialogue", "--run", "new",
        "--retry-judge-from", "original", "--concurrency", "4",
    ])
    with pytest.raises(SystemExit) as raised:
        cli.main()
    assert raised.value.code == 0
    assert captured == {"run_id": "new", "execute": False, "concurrency": 4}


@pytest.mark.parametrize("options", [
    ["--suite", "rag"], ["--suite", "dialogue", "--repeats", "2"],
    ["--suite", "dialogue", "--case", "case-1"], ["--suite", "dialogue", "--limit", "0"],
    ["--suite", "dialogue", "--scenario", "meituan_rider"],
])
def test_cli_rejects_judge_retry_plan_changes(monkeypatch, capsys, options):
    monkeypatch.setattr(cli.Settings, "from_env", object)
    monkeypatch.setattr("sys.argv", [
        "growth", "evaluate", "--run", "new", "--retry-judge-from", "original", *options,
    ])
    with pytest.raises(SystemExit) as raised:
        cli.main()
    assert raised.value.code == 1
    assert "沿用源计划" in capsys.readouterr().err
