"""原子保存评测产物，使用内容哈希标识本次输入。"""

import gzip
import hashlib
import json
from pathlib import Path

from backend.paths import portable_data

JSON_COMPRESSION_THRESHOLD = 50 * 1024 * 1024


def stored_json_path(path):
    """找到实际保存的 JSON；压缩与未压缩版本不能同时作为有效证据。"""
    path = Path(path)
    plain = path.with_suffix("") if path.suffix == ".gz" else path
    compressed = plain.with_suffix(plain.suffix + ".gz")
    for candidate in (plain, compressed):
        if candidate.is_symlink():
            raise ValueError("评测 JSON 不能使用符号链接")
    if plain.exists() and compressed.exists():
        raise ValueError("同一评测 JSON 存在两个格式，请先核对并保留唯一有效版本")
    return compressed if compressed.is_file() else plain


def load(path):
    """透明读取原始 JSON 或无损压缩的 JSON，不改变记录内容和顺序。"""
    path = stored_json_path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as source:
        return json.load(source)


def digest(value):
    """对可序列化内容生成稳定哈希，不把凭据放入待计算内容。"""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()


def dump(path, value):
    """原子保存 JSON；聚合轨迹达到 50 MiB 时无损压缩，避免超大 Git 文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(portable_data(value), ensure_ascii=False, indent=2, default=str) + "\n").encode()
    compressed = path.with_suffix(path.suffix + ".gz")
    use_compression = path.name == "traces.json" and len(raw) >= JSON_COMPRESSION_THRESHOLD
    destination = compressed if use_compression else path
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        if use_compression:
            # 固定压缩头，确保相同原文得到相同压缩字节，便于校验冻结证据。
            with (
                temporary.open("wb") as target,
                gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0) as archive,
            ):
                archive.write(raw)
        else:
            temporary.write_bytes(raw)
        temporary.replace(destination)
        if path.name == "traces.json":
            (path if use_compression else compressed).unlink(missing_ok=True)
    finally:
        temporary.unlink(missing_ok=True)
