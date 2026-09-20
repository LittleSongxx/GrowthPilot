"""启动时验证并恢复本地向量模型，测试使用确定性向量且不下载模型。"""

import pytest

from backend.agent_harness.rag import index
from backend.database import DatabaseConfig


class WorkingModel:
    def embed(self, texts, *, batch_size):
        return [[1.0] + [0.0] * (index.DIMENSIONS - 1) for _ in texts]


class BrokenModel:
    def embed(self, texts, *, batch_size):
        raise RuntimeError("模拟模型文件损坏")


@pytest.mark.parametrize("local_state", ["ready", "missing", "broken"])
def test_model_preparation_uses_local_cache_then_recovers_if_needed(tmp_path, monkeypatch, local_state):
    calls = []

    def load_model(cache_dir, local_files_only):
        assert cache_dir == str(tmp_path / "models")
        calls.append(local_files_only)
        if local_files_only and local_state == "missing":
            raise OSError("模拟本地缓存缺失")
        if local_files_only and local_state == "broken":
            return BrokenModel()
        return WorkingModel()

    monkeypatch.setattr(index, "_load_model", load_model)
    policy = index.PolicyIndex(tmp_path, database=DatabaseConfig.from_env())
    policy.prepare_model()
    assert calls == ([True] if local_state == "ready" else [True, False])
    assert len(policy._embed(["报名条件"])[0].strip("[]").split(",")) == index.DIMENSIONS
    assert calls == ([True] if local_state == "ready" else [True, False])


def test_model_preparation_does_not_hide_a_failed_recovery(tmp_path, monkeypatch):
    calls = []

    def unavailable(cache_dir, local_files_only):
        calls.append(local_files_only)
        raise OSError("模拟恢复失败")

    monkeypatch.setattr(index, "_load_model", unavailable)
    policy = index.PolicyIndex(tmp_path, database=DatabaseConfig.from_env())
    with pytest.raises(RuntimeError, match="政策向量模型加载或计算失败"):
        policy.prepare_model()
    assert calls == [True, False]


def test_regular_embedding_does_not_implicitly_download_a_missing_model(tmp_path, monkeypatch):
    calls = []

    def unavailable(cache_dir, local_files_only):
        calls.append(local_files_only)
        raise OSError("模拟本地缓存缺失")

    monkeypatch.setattr(index, "_load_model", unavailable)
    policy = index.PolicyIndex(tmp_path, database=DatabaseConfig.from_env())
    with pytest.raises(RuntimeError, match="通话检索不会下载模型"):
        policy._embed(["报名条件"])
    assert calls == [True]
