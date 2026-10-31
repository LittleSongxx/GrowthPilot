"""报告中的完整轨迹链接应指向实际保存的普通或压缩 JSON 文件。"""

import gzip

import pytest

from tests.evaluation.test_report import dialogue_summary, render_dialogue


@pytest.mark.parametrize("compressed", [False, True])
def test_report_links_to_existing_trace_format(tmp_path, compressed):
    filename = "traces.json.gz" if compressed else "traces.json"
    content = b"[]"
    (tmp_path / filename).write_bytes(gzip.compress(content) if compressed else content)

    report = render_dialogue(tmp_path, dialogue_summary())

    assert f"[完整对话与调用事件]({filename})" in report
    if compressed:
        assert "[完整对话与调用事件](traces.json)" not in report
