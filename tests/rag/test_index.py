"""索引测试使用确定性向量，验证真实 PostgreSQL/pgvector 与 BM25，不下载模型。"""

import math
from contextlib import contextmanager

import pytest
from psycopg import IntegrityError
from psycopg.types.json import Jsonb

from backend.agent_harness.rag.index import DIMENSIONS, QUERY_PREFIX, PolicyIndex, _retrieval_text
from tests.db import database_for


def vector(*values):
    return [*values, *([0.0] * (DIMENSIONS - len(values)))]


class TestEmbedder:
    __test__ = False

    def embed(self, texts, **kwargs):
        for text in texts:
            if any(word in text for word in ("费用", "交钱", "押金")):
                yield vector(1, 0, 0)
            elif any(word in text for word in ("车辆", "电动车")):
                yield vector(0, 1, 0)
            else:
                yield vector(0, 0, 1)


def chunk(id="fee", scenario="rider", text="注册费用：请以当地服务说明为准。", **extra):
    return {
        "id": id,
        "document_id": f"doc-{id}",
        "scenario_id": scenario,
        "title": "官方入驻说明",
        "section": "入驻",
        "text": text,
        "url": f"https://example.org/{id}",
        "fetched_at": "2026-10-26T00:00:00+00:00",
        "published_at": None,
        "content_hash": f"sha256-{id}",
        **extra,
    }


def index(tmp_path):
    return PolicyIndex(tmp_path, database=database_for(tmp_path), embedder=TestEmbedder())


def test_missing_index_does_not_load_model_or_create_files(tmp_path, monkeypatch):
    policy = PolicyIndex(tmp_path / "missing", database=database_for(tmp_path))
    monkeypatch.setattr(policy, "_embed", lambda _: pytest.fail("must not load model"))
    result = policy.search("注册要交钱吗", "rider")
    assert result["status"] == "no_data"
    assert result["data"]["reason"] == "knowledge_not_indexed"
    assert policy.status()["status"] == "no_data"
    assert not (policy.runtime_dir / "models").exists()


def test_isolation_is_applied_before_knn_limit_and_fts(tmp_path):
    policy = index(tmp_path)
    policy.build([chunk(id=f"driver-{i}", scenario="driver") for i in range(40)] + [chunk()])
    result = policy.search("注册要交钱吗", "rider", limit=1)
    assert result["evidence"] == ["fee"]
    assert result["data"]["clauses"][0]["scenario_id"] == "rider"
    assert policy.search("注册要交钱吗", "merchant")["status"] == "no_data"


def test_return_limit_keeps_candidate_pool_and_ranking_stable(tmp_path, monkeypatch):
    policy = index(tmp_path)
    policy.build(
        [chunk(id=f"rider-{i}") for i in range(40)]
        + [chunk(id=f"driver-{i}", scenario="driver") for i in range(40)]
        + [chunk(id=f"privacy-{i}", document_type="privacy_policy") for i in range(40)]
    )
    statements = []
    connect = policy._connect

    @contextmanager
    def traced_connection(*, read_only=False):
        with connect(read_only=read_only) as connection:
            class TracedConnection:
                def __getattr__(self, name):
                    return getattr(connection, name)

                def execute(self, sql, params=None):
                    statements.append((sql, params))
                    return connection.execute(sql, params)

            yield TracedConnection()

    monkeypatch.setattr(policy, "_connect", traced_connection)
    default = policy.search("费用", "rider")["data"]["clauses"]
    assert len(default) == 8
    for limit in (1, 5, 8, 10):
        clauses = policy.search("费用", "rider", limit=limit)["data"]["clauses"]
        assert len(clauses) == limit
        assert clauses[:8] == default[:limit]
        assert all(c["scenario_id"] == "rider" for c in clauses)
        assert all(c.get("document_type") != "privacy_policy" for c in clauses)
    # 直接核对两路实际 SQL，避免恰好相同的排序掩盖候选池随 limit 扩大的回归。
    vector_queries = [(sql, args) for sql, args in statements if "ORDER BY distance" in sql]
    lexical_queries = [(sql, args) for sql, args in statements if "AS score FROM" in sql]
    assert len(vector_queries) == len(lexical_queries) == 5
    assert all("LIMIT %s" in sql and args[-1] == 20 for sql, args in vector_queries)
    assert all("LIMIT %s" in sql and args[-1] == 20 for sql, args in lexical_queries)


def test_real_payload_provenance_and_index_survive_reopening(tmp_path):
    original = chunk(region="杭州试点", valid_until="2027-01-01", business_type="众包")
    policy = index(tmp_path)
    status = policy.build([original])
    reopened = index(tmp_path)
    result = reopened.search("交钱吗", "rider")
    clause = result["data"]["clauses"][0]
    assert {key: clause[key] for key in original} == original
    assert clause["published_at"] is None  # 抓取时间不能冒充发布时间。
    assert result["data"]["corpus_version"] == status["scenarios"][0]["version"]
    assert result["data"]["indexed_at"]
    assert "正确率" in result["data"]["usage"]


def test_rebuild_replaces_one_scenario_and_empty_build_removes_it(tmp_path, monkeypatch):
    policy = index(tmp_path)
    policy.build([chunk(), chunk(id="driver", scenario="driver")])
    old_version = policy.status()["scenarios"][1]["version"]
    policy.build([chunk(id="new")], scenario_id="rider")
    assert policy.search("费用", "rider")["evidence"] == ["new"]
    assert policy.search("费用", "driver")["evidence"] == ["driver"]
    assert policy.status()["scenarios"][1]["version"] != old_version
    monkeypatch.setattr(policy, "_embed", lambda _: pytest.fail("empty build must not load model"))
    policy.build([], scenario_id="rider")
    assert policy.search("费用", "rider")["status"] == "no_data"
    assert policy.status()["scenarios"][0]["scenario_id"] == "driver"
    with policy._connect() as connection:
        assert connection.execute("SELECT count(*) AS count FROM rag_chunks").fetchone()["count"] == 1
        assert connection.execute(
            "SELECT count(DISTINCT chunk_no) AS count FROM rag_chunk_terms"
        ).fetchone()["count"] == 1


def test_failed_embedding_and_database_write_keep_old_corpus(tmp_path):
    policy = index(tmp_path)
    policy.build([chunk(), chunk(id="other", scenario="driver")])
    original = policy.status()

    class InvalidEmbedder:
        def embed(self, texts, **kwargs):
            return [[float("nan")] * DIMENSIONS for _ in texts]

    policy._embedder = InvalidEmbedder()
    with pytest.raises(RuntimeError, match="向量模型"):
        policy.build([chunk(id="bad")], scenario_id="rider")
    assert policy.status() == original
    policy._embedder = TestEmbedder()
    # 重复占用其他场景的 ID，使删除之后的 INSERT 失败，检查事务回滚。
    with pytest.raises(IntegrityError):
        policy.build([chunk(id="other")], scenario_id="rider")
    assert policy.status() == original
    assert policy.search("费用", "rider")["evidence"] == ["fee"]


def test_rrf_promotes_agreement_between_vector_and_lexical_routes(tmp_path):
    class RankedEmbedder:
        def embed(self, texts, **kwargs):
            for text in texts:
                similarity = (
                    1
                    if text.startswith(QUERY_PREFIX)
                    else 0.9
                    if "平台费用" in text
                    else 0.8
                    if "押金 押金" in text
                    else 0.7
                )
                yield vector(similarity, math.sqrt(1 - similarity**2))

    policy = PolicyIndex(tmp_path, database=database_for(tmp_path), embedder=RankedEmbedder())
    policy.build(
        [
            chunk(id="vector-first", text="平台费用规则"),
            chunk(id="both", text="押金 押金 押金"),
            chunk(id="lexical", text="押金及其相关管理细节，所有办理程序和说明。"),
        ]
    )
    clauses = policy.search("押金", "rider")["data"]["clauses"]
    assert clauses[0]["id"] == "both"
    assert clauses[0]["retrieval"]["vector_rank"] == 2
    assert clauses[0]["retrieval"]["lexical_rank"] == 1
    assert clauses[0]["retrieval"]["rrf_score"] > clauses[-1]["retrieval"]["rrf_score"]


def test_unrelated_query_returns_no_evidence(tmp_path):
    policy = index(tmp_path)
    policy.build([chunk()])
    result = policy.search("法国巴黎卢浮宫门票怎么买", "rider")
    assert result["status"] == "no_data"
    assert result["data"]["reason"] == "no_relevant_clauses"
    assert not result["evidence"]


def test_policy_dates_follow_china_midnight(monkeypatch):
    from datetime import UTC, datetime

    from backend.agent_harness.rag import index as module

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            # 上海已进入 27 日，UTC 尚为 26 日；政策日期按本地日历判断。
            return datetime(2026, 10, 26, 16, 30, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(module, "datetime", Clock)
    assert module._is_current(chunk(valid_from="2026-10-27"))
    assert not module._is_current(chunk(valid_until="2026-10-26"))


def test_signature_mismatch_requires_full_rebuild(tmp_path):
    policy = index(tmp_path)
    policy.build([chunk()])
    with policy._connect() as connection, connection.transaction():
        connection.execute("UPDATE rag_index_meta SET value = 'old-model'")
    assert policy.status()["status"] == "rebuild_required"
    assert policy.search("费用", "rider")["data"]["reason"] == "index_rebuild_required"
    with pytest.raises(RuntimeError, match="重建全部"):
        policy.build([chunk()], scenario_id="rider")
    policy.build([chunk(id="new")])
    assert policy.search("费用", "rider")["evidence"] == ["new"]


@pytest.mark.parametrize(
    "query,scenario,limit",
    [
        (" ", "rider", 5),
        ("费用" * 501, "rider", 5),
        ("费用", "", 5),
        ("费用", "rider", 0),
        ("费用", "rider", 11),
        ("费用", "rider", 1.5),
        ("费用", "rider", True),
    ],
)
def test_input_bounds(tmp_path, query, scenario, limit):
    with pytest.raises(ValueError):
        index(tmp_path).search(query, scenario, limit)


def test_rebuild_rejects_duplicates_and_scenario_mismatch_before_writing(tmp_path):
    policy = index(tmp_path)
    with pytest.raises(ValueError, match="重复"):
        policy.build([chunk(), chunk()])
    with pytest.raises(ValueError, match="不一致"):
        policy.build([chunk()], scenario_id="driver")
    assert policy.status()["status"] == "no_data"
    assert not (policy.runtime_dir / "models").exists()


def test_only_build_may_download_the_model(tmp_path, monkeypatch):
    local_only_flags = []

    def load_model(cache_dir, local_files_only):
        local_only_flags.append(local_files_only)
        return TestEmbedder()

    monkeypatch.setattr("backend.agent_harness.rag.index._load_model", load_model)
    PolicyIndex(tmp_path, database=database_for(tmp_path)).build([chunk()])
    PolicyIndex(tmp_path, database=database_for(tmp_path)).search("费用", "rider")
    assert local_only_flags == [False, True]


def test_excludes_expired_future_and_failed_sources(tmp_path):
    policy = index(tmp_path)
    policy.build(
        [
            chunk(),
            chunk(id="expired", valid_until="1900-01-01"),
            chunk(id="future", valid_from="2999-01-01"),
            chunk(id="stale", source_status="stale"),
        ]
    )
    assert policy.status()["scenarios"][0]["chunk_count"] == 1
    assert policy.search("费用", "rider")["evidence"] == ["fee"]
    # 即使索引建立时有效，查询时也要再次核对，不能继续返回已过期原文。
    expired = chunk(valid_until="1900-01-01")
    with policy._connect() as connection, connection.transaction():
        connection.execute("UPDATE rag_chunks SET payload = %s", (Jsonb(expired),))
    assert policy.search("费用", "rider")["status"] == "no_data"


def test_business_scope_filters_privacy_before_both_candidate_limits(tmp_path):
    policy = index(tmp_path)
    # 隐私文档有大量同词、同向量切块；必须在两路 top-20 之前排除。
    policy.build(
        [
            chunk(id=f"privacy-{i}", text="费用 费用 费用", document_type="privacy_policy")
            for i in range(45)
        ]
        + [chunk(id="business", text="费用标准")]
    )
    result = policy.search("费用", "rider")
    assert result["evidence"] == ["business"]
    assert result["data"]["retrieval_scopes"] == ["business"]


@pytest.mark.parametrize(
    "query",
    [
        "个人信息会提供给谁",
        "征信授权如何撤回",
        "不想再收到营销短信",
        "为什么要身份证",
        "银行卡信息用途是什么",
        "为什么要查询征信",
        "注销账号怎样删除资料",
        "请不要再联系我",
        "我的资料会给第三方吗",
        "你怎么知道我的电话",
        "隐私政策是什么",
        "会读取我的通讯录吗",
        "你们会获取定位吗",
        "关掉位置权限还能用吗",
        "会不会把我的号码给别人",
        "个人信息保护政策",
    ],
)
def test_privacy_intent_can_retrieve_original_policy(tmp_path, query):
    policy = index(tmp_path)
    original = chunk(
        id="privacy",
        text="个人信息的收集和使用，营销退订与撤回授权方法。",
        document_type="privacy_policy",
    )
    policy.build([chunk(), original])
    result = policy.search(query, "rider")
    assert "privacy" in result["evidence"]
    assert result["data"]["retrieval_scopes"] == ["business", "privacy"]
    clause = next(c for c in result["data"]["clauses"] if c["id"] == "privacy")
    assert clause["text"] == original["text"]
    assert clause["url"] == original["url"]


@pytest.mark.parametrize(
    "query",
    [
        "身份证照片有什么要求",
        "银行卡有哪些申请要求",
        "征信不好能申请吗",
        "商家入驻要提交什么资料",
        "注册费用怎么收",
        "需要本人身份证和银行卡吗",
        "申请度小满需要准备哪些个人资料？",
        "注册司机要填写哪些个人信息？",
    ],
)
def test_application_documents_are_not_automatically_privacy_queries(tmp_path, query):
    policy = index(tmp_path)
    policy.build([chunk(id="privacy", text=query, document_type="privacy_policy")])
    result = policy.search(query, "rider")
    assert result["status"] == "no_data"  # 不能在缺少业务资料时偷偷用隐私协议补位。
    assert result["data"]["retrieval_scopes"] == ["business"]


@pytest.mark.parametrize(
    "query,scenario,include_privacy",
    [
        ("注册登录有钱花需要什么个人信息", "financial_consultation", True),
        ("查看账单还款信息入口", "financial_consultation", True),
        ("有钱花注册需要哪些信息", "financial_consultation", True),
        ("注册账户不提供手机号还能登录吗", "financial_consultation", True),
        ("不申请额度，只想查看账单", "financial_consultation", True),
        ("看不到我的还款计划", "financial_consultation", True),
        ("在哪里修改本人信息", "financial_consultation", True),
        ("我的个人资料在哪里查看", "financial_consultation", True),
        ("司机需要准备什么个人资料", "didi_driver", False),
        ("骑手注册需要哪些个人信息", "meituan_rider", False),
        ("登录骑手平台需要哪些材料", "meituan_rider", False),
        ("申请度小满需要准备哪些个人资料", "financial_consultation", False),
        ("不申请额度，只想了解申请需要什么材料", "financial_consultation", False),
        ("注册费用怎么收", "financial_consultation", False),
        ("借款要提交账单流水吗", "financial_consultation", False),
        ("账单要在几号还款", "financial_consultation", False),
    ],
)
def test_financial_account_access_scopes_do_not_expand_application_material_queries(
    tmp_path, query, scenario, include_privacy
):
    policy = index(tmp_path)
    original = chunk(
        id="account-access",
        scenario=scenario,
        text=query,
        document_type="privacy_policy",
    )
    policy.build([original])
    result = policy.search(query, scenario)
    assert result["data"]["retrieval_scopes"] == (
        ["business", "privacy"] if include_privacy else ["business"]
    )
    assert result["evidence"] == (["account-access"] if include_privacy else [])
    if include_privacy:
        assert result["data"]["clauses"][0]["text"] == original["text"]


@pytest.mark.parametrize("business_similarity,expect_business", [(0.93, True), (0.50, False)])
@pytest.mark.parametrize(
    "query,scenario",
    [
        ("注册费用怎么收，为什么要身份证", "rider"),
        ("注册费用怎么收，在哪里查看我的账单", "financial_consultation"),
    ],
)
def test_mixed_query_keeps_relevant_business_without_forcing_weak_evidence(
    tmp_path, business_similarity, expect_business, query, scenario
):
    class ScopeEmbedder:
        def embed(self, texts, **kwargs):
            for text in texts:
                similarity = (
                    1
                    if text.startswith(QUERY_PREFIX)
                    else 0.99
                    if "个人信息" in text
                    else business_similarity
                )
                yield vector(similarity, math.sqrt(1 - similarity**2))

    policy = PolicyIndex(tmp_path, database=database_for(tmp_path), embedder=ScopeEmbedder())
    policy.build(
        [
            chunk(
                id=f"privacy-{i}",
                scenario=scenario,
                text="注册费用 个人信息 为什么要身份证",
                document_type="privacy_policy",
            )
            for i in range(45)
        ]
        + [chunk(id="business", scenario=scenario, text="注册费用标准")]
    )
    result = policy.search(query, scenario, limit=5)
    assert len(result["evidence"]) == 5
    assert result["evidence"][0].startswith("privacy-")
    assert ("business" in result["evidence"]) is expect_business


def test_privacy_scope_preserves_scenario_and_validity_constraints(tmp_path):
    policy = index(tmp_path)
    policy.build(
        [
            chunk(
                id=f"other-{i}",
                scenario="driver",
                text="个人信息收集规则",
                document_type="privacy_policy",
            )
            for i in range(40)
        ]
        + [
            chunk(
                id="expired",
                text="个人信息收集规则",
                document_type="privacy_policy",
                valid_until="1900-01-01",
            ),
            chunk(id="rider-privacy", text="个人信息收集规则", document_type="privacy_policy"),
        ]
    )
    result = policy.search("个人信息收集规则", "rider")
    assert result["evidence"] == ["rider-privacy"]


def test_partial_sentence_remains_marked_and_warns_about_incomplete_evidence(tmp_path):
    policy = index(tmp_path)
    policy.build([chunk(partial_sentence=True)])
    result = policy.search("费用", "rider")
    assert result["data"]["clauses"][0]["partial_sentence"] is True
    assert "不能独立作为完整依据" in result["data"]["usage"]


def test_region_enters_embeddings_and_lexical_retrieval_for_identical_clauses(tmp_path):
    class RecordingEmbedder(TestEmbedder):
        def __init__(self):
            self.inputs = []

        def embed(self, texts, **kwargs):
            self.inputs.extend(texts)
            for text in texts:
                yield vector(1, 1 if "杭州" in text else -1)

    embedder = RecordingEmbedder()
    policy = PolicyIndex(tmp_path, database=database_for(tmp_path), embedder=embedder)
    body = "费用标准以当地公布的办理规则为准。"
    policy.build(
        [
            chunk(id="beijing", text=body, region="北京市", business_type="网约车"),
            chunk(id="hangzhou", text=body, region="杭州市", business_type="网约车"),
        ]
    )
    assert "北京市" in embedder.inputs[0]
    assert "杭州市" in embedder.inputs[1]
    assert "官方入驻说明" in embedder.inputs[1]
    assert "网约车" in embedder.inputs[1]
    assert "入驻" in embedder.inputs[1]
    result = policy.search("杭州费用", "rider", limit=1)
    assert result["evidence"] == ["hangzhou"]
    assert result["data"]["clauses"][0]["text"] == body


def test_long_context_is_bounded_and_deduplicated_without_cutting_the_clause():
    title = "很长的官方文件标题" * 20
    body = "原始条款" * 87 + "结束"
    source = chunk(
        title=title,
        section=title + " / 第二条车辆要求",
        text=body,
        region="杭州市",
        business_type="网约车",
    )
    text = _retrieval_text(source)
    assert text.endswith(body)
    context = text[: -len(body)]
    assert len(context) <= 124
    assert context.count(title[:40]) == 1
    assert "第二条车辆要求" in context
    assert "杭州市" in context
    assert "网约车" in context
    assert source["title"] == title
    assert source["section"] == title + " / 第二条车辆要求"


def test_namespaces_isolate_same_chunk_ids_and_clear_only_selected_index(tmp_path):
    database = database_for(tmp_path)
    production = PolicyIndex(tmp_path, database=database, embedder=TestEmbedder())
    evaluation = PolicyIndex(
        tmp_path, database=database, namespace="eval_frozen", embedder=TestEmbedder(),
    )
    production.build([chunk(text="费用依生产政策说明。")])
    evaluation.build([chunk(text="费用依冻结评测政策说明。")])
    assert production.snapshot()["chunks"][0]["text"] == "费用依生产政策说明。"
    assert evaluation.snapshot()["chunks"][0]["text"] == "费用依冻结评测政策说明。"
    assert production.corpus("rider")["version"] != evaluation.corpus("rider")["version"]
    assert production.identity != evaluation.identity
    evaluation.clear()
    assert evaluation.status()["status"] == "no_data"
    assert production.search("费用", "rider")["evidence"] == ["fee"]


def test_snapshot_preserves_original_payload_hash_and_filters_scenarios(tmp_path):
    import hashlib
    import json

    policy = index(tmp_path)
    originals = [chunk(id="fee"), chunk(id="vehicle", text="车辆要求"), chunk(id="other", scenario="driver")]
    policy.build(originals)
    snapshot = policy.snapshot(["rider"])
    assert snapshot["chunks"] == originals[:2]
    expected = hashlib.sha256("\n".join(sorted(
        json.dumps(c, ensure_ascii=False, sort_keys=True) for c in originals[:2]
    )).encode()).hexdigest()
    assert snapshot["scenarios"][0]["version"] == expected
    assert policy.corpus("rider")["version"] == expected
    assert policy.corpus("missing") is None
    assert policy.snapshot([])["chunks"] == []


def test_bm25_matches_formula_using_all_namespace_documents(tmp_path):
    from collections import Counter

    from backend.agent_harness.rag.index import _terms

    policy = index(tmp_path)
    originals = [
        chunk(id="one", text="押金 押金 车辆"),
        chunk(id="two", text="押金 费用 材料 材料 材料"),
        chunk(id="other", scenario="driver", text="车辆 车辆"),
        chunk(id="privacy", document_type="privacy_policy", text="信息 信息 信息 信息"),
    ]
    policy.build(originals)
    # 其他命名空间的词频不得影响本次索引的 BM25 统计。
    PolicyIndex(tmp_path, database=policy.database, namespace="elsewhere", embedder=TestEmbedder()).build(
        [chunk(id=str(i), text="押金") for i in range(8)],
    )
    terms = [Counter(_terms(_retrieval_text(c))) for c in originals]
    average = sum(sum(t.values()) for t in terms) / len(terms)
    expected = []
    for counts in terms[:2]:
        frequency = counts["押金"]
        inverse_frequency = max(math.log((4 - 2 + 0.5) / (2 + 0.5)), 0.000001)
        expected.append(-inverse_frequency * frequency * 2.2 / (
            frequency + 1.2 * (0.25 + 0.75 * sum(counts.values()) / average)
        ))
    with policy._read_snapshot() as connection:
        rows = policy._lexical_candidates(connection, ["押金"], "rider", False, 20)
    assert [float(row["score"]) for row in rows] == pytest.approx(sorted(expected))


def test_rebuild_between_queries_keeps_one_consistent_search_snapshot(tmp_path, monkeypatch):
    policy = index(tmp_path)
    original = policy.build([chunk(id="old")])["scenarios"][0]["version"]
    vector_candidates = policy._vector_candidates
    replaced = False

    def replace_after_snapshot(*args):
        nonlocal replaced
        if not replaced:
            replaced = True
            # 第二条连接先提交新版，当前查询仍必须使用已取得的旧版快照。
            index(tmp_path).build([chunk(id="new")])
        return vector_candidates(*args)

    monkeypatch.setattr(policy, "_vector_candidates", replace_after_snapshot)
    result = policy.search("费用", "rider")
    assert result["evidence"] == ["old"]
    assert result["data"]["corpus_version"] == original
    assert policy.search("费用", "rider")["evidence"] == ["new"]


def test_current_sources_are_filtered_before_candidate_limit(tmp_path):
    policy = index(tmp_path)
    policy.build([chunk(id=f"old-{i}") for i in range(30)] + [chunk(id="current")])
    with policy._connect() as connection:
        connection.execute(
            "UPDATE rag_chunks SET payload = jsonb_set(payload, '{valid_until}', %s::jsonb) "
            "WHERE id LIKE 'old-%%'", ('"1900-01-01"',),
        )
    assert policy.search("费用", "rider")["evidence"] == ["current"]


def test_reading_absent_schema_does_not_initialize_database(tmp_path, database_config):
    from backend.database import DatabaseConfig, close_database, connect

    missing = DatabaseConfig(database_config.dsn, database_config.schema + "_missing")
    policy = PolicyIndex(tmp_path, database=missing)
    try:
        assert policy.status()["status"] == "no_data"
        assert policy.snapshot()["chunks"] == []
        assert policy.search("费用", "rider")["data"]["reason"] == "knowledge_not_indexed"
        with connect(database_config, read_only=True) as connection:
            assert connection.execute(
                "SELECT to_regnamespace(%s) AS name", (missing.schema,),
            ).fetchone()["name"] is None
    finally:
        close_database(missing)


def test_snapshot_rejects_payload_identity_inconsistent_with_index_row(tmp_path):
    policy = index(tmp_path)
    policy.build([chunk()])
    with policy._connect() as connection:
        connection.execute("UPDATE rag_chunks SET payload = %s", (Jsonb(chunk(id="wrong")),))
    with pytest.raises(ValueError, match="原文不一致"):
        policy.snapshot()
