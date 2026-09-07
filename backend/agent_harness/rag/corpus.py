"""校验并读取随项目交付的固定教学语料，不访问网络或修改快照。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path

CORPUS_RELATIVE_PATH = Path("backend/agent_harness/rag/knowledge/v1")
SCENARIOS = {"meituan_rider", "meituan_merchant", "didi_driver", "financial_consultation"}
_FILES = {"document.json", "clean.txt", "chunks.json"}
_LOCAL_PATH = re.compile(r"/(?:Users|home)/|[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/]")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(f"固定教学语料校验失败：{message}")


def _read(root: Path, relative: str, checksum: str | None = None) -> bytes:
    """仅接受包内相对路径，错误信息也只显示包内路径。"""
    path = Path(relative)
    _require(not path.is_absolute() and ".." not in path.parts
             and path.as_posix() == relative, "文件路径必须是包内相对路径")
    _require(not any((root / parent).is_symlink() for parent in (path, *path.parents)),
             "语料文件不能通过符号链接引用其他目录")
    try:
        content = (root / path).read_bytes()
    except OSError:
        raise ValueError(f"固定教学语料校验失败：无法读取 {relative}") from None
    if checksum is not None:
        _require(hashlib.sha256(content).hexdigest() == checksum,
                 f"{relative} 的 SHA-256 不一致，请恢复对应版本的教学语料")
    _require(not _LOCAL_PATH.search(content.decode("utf-8")),
             f"{relative} 包含本机用户目录")
    return content


def _load_snapshot(root: Path) -> tuple[dict, list[dict]]:
    """先验证整个版本的完整性，再让调用方选取单一业务场景。"""
    root = Path(root)
    checksum = _read(root, "catalog.sha256").decode().strip()
    _require(bool(re.fullmatch(r"[a-f0-9]{64}", checksum)), "catalog.sha256 格式不正确")
    catalog = json.loads(_read(root, "catalog.json", checksum))
    _require(catalog["schema_version"] == 1, "不支持的清单格式")
    _require(bool(re.fullmatch(r"teaching-v[1-9][0-9]*", catalog["version"])), "语料版本格式不正确")
    date.fromisoformat(catalog["as_of"])
    scenarios = catalog["scenarios"]
    _require(len(scenarios) == len(SCENARIOS)
             and {entry["scenario_id"] for entry in scenarios} == SCENARIOS,
             "清单必须完整包含四个业务场景")
    documents = catalog["documents"]
    _require(len(documents) == catalog["document_count"], "文档总数与清单不一致")
    expected_files = {"catalog.json", "catalog.sha256"}
    document_ids, chunk_ids, chunks = set(), set(), []
    for entry in documents:
        document_id, scenario_id = entry["id"], entry["scenario_id"]
        _require(bool(re.fullmatch(r"[a-z0-9_-]+", document_id))
                 and document_id not in document_ids, "来源标识无效或重复")
        document_ids.add(document_id)
        _require(scenario_id in SCENARIOS, "文档属于未知业务场景")
        _require(entry["source_status"] in {"fresh", "unchanged"}, "文档没有成功采集快照")
        _require(set(entry["files"]) == _FILES, "每份文档必须包含正文、分块和来源元数据")
        contents = {}
        for name, record in entry["files"].items():
            relative = f"documents/{document_id}/{name}"
            _require(record["path"] == relative, "文件路径必须与来源标识及文件名一致")
            expected_files.add(relative)
            contents[name] = _read(root, relative, record["sha256"])
        document = json.loads(contents["document.json"])
        _require(document["id"] == document_id and document["scenario_id"] == scenario_id
                 and document["content_hash"] == entry["content_hash"], "来源元数据与清单不一致")
        _require(bool(contents["clean.txt"].strip()), "教学正文不能为空")
        members = json.loads(contents["chunks.json"])
        _require(bool(members) and len(members) == entry["chunk_count"], "文档分块数与清单不一致")
        for chunk in members:
            _require(all(isinstance(chunk.get(field), str) and chunk[field].strip() for field in
                         ("id", "document_id", "scenario_id", "title", "text", "url",
                          "fetched_at", "content_hash")), "分块缺少必要的正文或来源字段")
            _require(chunk["id"] not in chunk_ids, "分块标识重复")
            chunk_ids.add(chunk["id"])
            _require(chunk["document_id"] == document_id and chunk["scenario_id"] == scenario_id
                     and chunk["content_hash"] == entry["content_hash"], "分块与来源元数据不一致")
            # 与采集器原来的加载口径一致，保留已有评测语料的完整 payload。
            chunks.append({**chunk, "fetched_at": entry["fetched_at"],
                           "source_status": entry["source_status"]})
    actual_files = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    _require(actual_files == expected_files, "目录文件与清单不一致，存在遗漏或未登记文件")
    _require(len(chunks) == catalog["chunk_count"], "分块总数与清单不一致")
    for scenario in scenarios:
        scenario_id = scenario["scenario_id"]
        members = sorted(json.dumps(chunk, ensure_ascii=False, sort_keys=True)
                         for chunk in chunks if chunk["scenario_id"] == scenario_id)
        # 保持与 PolicyIndex.build 相同的算法，供启动检查和评测比对版本。
        version = hashlib.sha256("\n".join(members).encode()).hexdigest()
        _require(bool(members) and version == scenario["version"]
                 and len(members) == scenario["chunk_count"]
                 and sum(entry["scenario_id"] == scenario_id for entry in documents)
                 == scenario["document_count"], f"{scenario_id} 的数量或版本与清单不一致")
    missing_ids = set()
    for missing in catalog["unavailable_sources"]:
        _require(missing["id"] not in document_ids | missing_ids
                 and missing["scenario_id"] in SCENARIOS and bool(missing["reason"]),
                 "缺失来源的说明不完整或与已有来源重复")
        missing_ids.add(missing["id"])
    return catalog, chunks


def _validated_snapshot(corpus_dir: Path) -> tuple[dict, list[dict]]:
    try:
        return _load_snapshot(corpus_dir)
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError):
        raise ValueError("固定教学语料校验失败：清单或文档格式不正确") from None


def load_corpus(corpus_dir: Path, scenario_id: str | None = None) -> list[dict]:
    """返回已校验的固定分块；有效日期仍由政策索引按实际查询日期判断。"""
    if scenario_id is not None:
        _require(scenario_id in SCENARIOS, "请求了未知业务场景")
    _, chunks = _validated_snapshot(corpus_dir)
    return [chunk for chunk in chunks if scenario_id is None or chunk["scenario_id"] == scenario_id]


def corpus_status(corpus_dir: Path) -> dict:
    """返回已验证的版本、覆盖数量及缺失来源，不连接数据库。"""
    catalog, _ = _validated_snapshot(corpus_dir)
    return {key: catalog[key] for key in (
        "version", "as_of", "document_count", "chunk_count", "scenarios", "unavailable_sources"
    )}
