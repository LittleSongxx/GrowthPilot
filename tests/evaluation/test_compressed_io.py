"""大规模聚合轨迹无损压缩，仍可按原顺序读取和验证。"""

import gzip
import json

import pytest

from evaluation.common import io


def test_large_trace_compression_preserves_exact_json_and_is_deterministic(tmp_path, monkeypatch):
    monkeypatch.setattr(io, "JSON_COMPRESSION_THRESHOLD", 1)
    value = [{"case_id": "案例一", "repeat": 2}, {"case_id": "案例二", "repeat": 1}]
    path = tmp_path / "traces.json"
    io.dump(path, value)
    compressed = io.stored_json_path(path)
    assert compressed.name == "traces.json.gz" and not path.exists()
    raw = compressed.read_bytes()
    expected = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    assert gzip.decompress(raw) == expected
    assert io.load(path) == io.load(compressed) == value
    io.dump(path, value)
    assert compressed.read_bytes() == raw


def test_format_switch_keeps_only_latest_complete_trace(tmp_path, monkeypatch):
    path = tmp_path / "traces.json"
    io.dump(path, ["原记录"])
    assert path.is_file()
    monkeypatch.setattr(io, "JSON_COMPRESSION_THRESHOLD", 1)
    io.dump(path, ["新记录"])
    assert not path.exists()
    assert io.load(path) == ["新记录"]
    monkeypatch.setattr(io, "JSON_COMPRESSION_THRESHOLD", 1024)
    io.dump(path, ["最后记录"])
    assert path.is_file() and not path.with_suffix(".json.gz").exists()
    assert io.load(path) == ["最后记录"]
    assert not list(tmp_path.glob("*.tmp"))


def test_other_json_artifacts_keep_readable_format(tmp_path, monkeypatch):
    monkeypatch.setattr(io, "JSON_COMPRESSION_THRESHOLD", 1)
    io.dump(tmp_path / "manifest.json", {"status": "completed"})
    assert io.load(tmp_path / "manifest.json") == {"status": "completed"}
    assert not (tmp_path / "manifest.json.gz").exists()


def test_failed_compression_preserves_previous_artifact(tmp_path, monkeypatch):
    path = tmp_path / "traces.json"
    io.dump(path, ["已有证据"])
    monkeypatch.setattr(io, "JSON_COMPRESSION_THRESHOLD", 1)

    def failed(*_args, **_kwargs):
        raise OSError("模拟磁盘写入失败")

    monkeypatch.setattr(gzip.GzipFile, "write", failed)
    with pytest.raises(OSError, match="磁盘写入失败"):
        io.dump(path, ["待保存"])
    assert io.load(path) == ["已有证据"]
    assert not (tmp_path / "traces.json.gz").exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_read_rejects_ambiguous_formats_and_symlinks(tmp_path):
    plain = tmp_path / "traces.json"
    compressed = tmp_path / "traces.json.gz"
    plain.write_text("[]", encoding="utf-8")
    compressed.write_bytes(gzip.compress(b"[]", mtime=0))
    with pytest.raises(ValueError, match="两个格式"):
        io.load(plain)
    compressed.unlink()
    compressed.symlink_to(tmp_path / "outside.gz")
    with pytest.raises(ValueError, match="符号链接"):
        io.load(plain)
