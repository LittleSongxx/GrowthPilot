"""政策检索：PostgreSQL 保存原文与词频，pgvector 精确向量检索，通过 RRF 合并排名。

原文和出处随索引保存；检索只提供证据，不替模型判断政策是否适用于客户。
公开方法是同步的，Web/Agent 调用方应使用 asyncio.to_thread。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from backend.database import DatabaseConfig, connect, initialize_database, table_exists

MODEL_NAME = "BAAI/bge-small-zh-v1.5"
DIMENSIONS = 512
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
INDEX_SIGNATURE = f"policy-pg-v1:{MODEL_NAME}:{DIMENSIONS}:scope-context:bm25"
# 用四场景真实问答和域外问题检查过的宽松下限；它不是正确率或充分证据标准。
MIN_SIMILARITY = 0.40
_MODEL_LOCK = threading.Lock()

# 身份证、银行卡、征信本身也是申请条件，不能见到这些词就切到隐私资料。
_PRIVACY_INTENT = re.compile(
    r"隐私|敏感信息|信息安全|授权|撤回同意|撤销同意|"
    r"营销|推销|骚扰|退订|勿扰|免打扰|注销(?:账号|账户)|"
    r"(?:别|不要|不想|不再|停止).{0,8}(?:打电话|联系|短信)|"
    r"(?:信息|数据|资料).{0,12}(?:收集|采集|用途|使用|共享|分享|转让|出售|泄露|保护|政策|保存|删除|第三方|给谁)|"
    r"(?:收集|采集|共享|分享|转让|出售|泄露|保存|删除).{0,12}(?:信息|数据|资料)|"
    r"(?:读取|获取|访问|收集|采集).{0,8}(?:通讯录|联系人|定位|位置|相机|摄像头|麦克风|通话记录)|"
    r"(?:位置|定位|通讯录|相机|摄像头|麦克风).{0,4}权限|"
    r"(?:号码|手机号|电话|联系人|通讯录).{0,10}(?:给别人|给他人|给第三方|分享|共享|出售|卖给|泄露)|"
    r"(?:为什么|为何|用途|怎么用|做什么|干什么|干嘛|用来|用于|怎么知道|从哪里).{0,16}"
    r"(?:身份证|银行卡|征信|手机号|电话|定位|人脸|通讯录)|"
    r"(?:身份证|银行卡|征信|手机号|电话|定位|人脸|通讯录).{0,16}"
    r"(?:为什么|为何|用途|怎么用|做什么|干什么|干嘛|用来|用于|共享|泄露|卖给)|"
    r"\b(?:privacy|consent|unsubscribe)\b",
    re.IGNORECASE,
)
# 金融隐私政策也承载账号登录、本人信息访问和账单入口说明。
# 仅补充明确的账号与访问意图，借款申请材料和其他场景报名仍走业务资料。
_FINANCIAL_ACCOUNT_INTENT = re.compile(
    r"(?=.*(?:登录|注册(?:账号|账户)|(?:账号|账户|app|有钱花|度小满).{0,6}注册|"
    r"注册.{0,6}(?:有钱花|度小满|app)))(?=.*(?:信息|资料|手机号|手机号码|实名|绑卡))|"
    r"(?:查看|查阅|访问|修改|更正|管理).{0,8}(?:我的|本人|个人|账户|账号).{0,6}(?:信息|资料)|"
    r"(?:我的|本人|个人|账户|账号).{0,6}(?:信息|资料).{0,8}(?:在哪里|在哪|哪里|查看|查阅|修改|更正)|"
    r"(?:查看|查询|查阅|访问|找不到|看不到|哪里看|在哪看|怎么看).{0,10}(?:账单|还款计划|还款信息)|"
    r"(?:账单|还款计划|还款信息).{0,10}(?:入口|在哪|哪里|查看|查询|查阅|看不到|找不到)",
    re.IGNORECASE,
)


@lru_cache(maxsize=2)
def _load_model(cache_dir: str, local_files_only: bool):
    from fastembed import TextEmbedding

    return TextEmbedding(
        MODEL_NAME, cache_dir=cache_dir, threads=2, local_files_only=local_files_only
    )


def _terms(text: str) -> list[str]:
    """沿用原检索口径：中文生成双字词项，英文和数字保留整词。"""
    terms = []
    for part in re.findall(r"[\u4e00-\u9fff]+|[a-z0-9]+", text.lower()):
        if "\u4e00" <= part[0] <= "\u9fff" and len(part) > 1:
            terms.extend(part[i : i + 2] for i in range(len(part) - 1))
        else:
            terms.append(part)
    return terms


def _retrieval_text(chunk: dict) -> str:
    """给条款补上适用范围；限制上下文长度，正文保持原样。"""
    title = " ".join(chunk["title"].split())
    context = []
    for field, limit in (("title", 40), ("region", 24), ("business_type", 16), ("section", 40)):
        value = " ".join(str(chunk.get(field) or "").split())
        if field == "section":
            value = value.removeprefix(title).strip(" /")
        value = value[:limit]
        if value and value not in context:
            context.append(value)
    return "\n".join([*context, chunk["text"]])


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _scenario(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", value):
        raise ValueError("scenario_id 必须是 1–80 位字母、数字、下划线或连字符。")


def _is_current(chunk: dict) -> bool:
    """仅使用来源明确声明的日期；没有日期不代表已核实其现行有效性。"""
    if chunk.get("source_status") in {"stale", "failed", "superseded"}:
        return False
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    for field, future in (("valid_from", True), ("valid_until", False)):
        if chunk.get(field):
            declared = date.fromisoformat(chunk[field][:10])
            if (future and declared > today) or (not future and declared < today):
                return False
    return True


def corpus_versions(chunks: list[dict]) -> dict[str, dict]:
    """按索引的序列化与有效期口径计算版本，避免把不同教学语料误当作已初始化。"""
    members = {}
    for chunk in chunks:
        if _is_current(chunk):
            members.setdefault(chunk["scenario_id"], []).append(_json(chunk))
    return {
        scenario: {
            "version": hashlib.sha256("\n".join(sorted(payloads)).encode()).hexdigest(),
            "chunk_count": len(payloads),
        }
        for scenario, payloads in sorted(members.items())
    }


class PolicyIndex:
    def __init__(
        self,
        runtime_dir: Path,
        *,
        database: DatabaseConfig | None = None,
        namespace: str = "production",
        embedder=None,
    ):
        self.runtime_dir = Path(runtime_dir)
        self.database = database or DatabaseConfig.from_env()
        _scenario(namespace)
        self.namespace = namespace
        # 构造不连接数据库，预览和读取索引状态不会隐式建表。
        # 测试可注入确定性模型；正式调用不提供该参数。
        self._embedder = embedder

    @property
    def identity(self) -> str:
        """供日志和评测使用的安全标识，不包含数据库密码。"""
        return f"{self.database.identity}/rag/{self.namespace}"

    @contextmanager
    def _connect(self, *, read_only: bool = False):
        with connect(self.database, read_only=read_only) as connection:
            yield connection

    @contextmanager
    def _read_snapshot(self):
        # 默认 READ COMMITTED 会在每条 SQL 间切换快照；显式固定语料和两路候选。
        with self._connect(read_only=True) as connection, connection.transaction():
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            yield connection

    def _embed(self, texts: list[str], *, allow_download: bool = False) -> list[str]:
        try:
            if self._embedder is None:
                with _MODEL_LOCK:
                    self._embedder = _load_model(
                        str(self.runtime_dir / "models"), not allow_download
                    )
            vectors = list(self._embedder.embed(texts, batch_size=32))
        except (OSError, ValueError, RuntimeError) as exc:
            raise RuntimeError(
                f"政策向量模型加载或计算失败（{MODEL_NAME}）；请运行 growth knowledge build "
                "准备本地模型缓存。通话检索不会下载模型。"
            ) from exc
        if len(vectors) != len(texts):
            raise RuntimeError("政策向量模型返回的向量数量与文本数量不一致。")
        output = []
        for vector in vectors:
            if len(vector) != DIMENSIONS or not all(math.isfinite(v) for v in vector):
                raise RuntimeError(f"政策向量模型必须返回 {DIMENSIONS} 维有限数值。")
            if not any(vector):
                raise RuntimeError("政策向量模型不能返回全零向量。")
            # pgvector 的 vector 类型以 float32 保存；绑定文本避免引入 NumPy 适配依赖。
            output.append("[" + ",".join(str(float(value)) for value in vector) + "]")
        return output

    def prepare_model(self) -> None:
        """启动时先验证本机模型，缓存缺失时恢复；通话检索仍不联网下载。"""
        try:
            self._embed(["教学知识检索"])
        except RuntimeError:
            self._embedder = None
            self._embed(["教学知识检索"], allow_download=True)

    def _signature(self, connection) -> str | None:
        if not table_exists(connection, "rag_index_meta"):
            return None
        row = connection.execute(
            "SELECT value FROM rag_index_meta WHERE namespace = %s AND key = 'signature'",
            (self.namespace,),
        ).fetchone()
        return row["value"] if row else None

    def _lock_namespace(self, connection) -> None:
        # 同一命名空间的整库/单场景重建串行发布，其他命名空间互不阻塞。
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"rag:{self.database.schema}:{self.namespace}",),
        )

    def build(self, chunks: list[dict], scenario_id: str | None = None) -> dict:
        """先生成全部向量，再用一个事务替换目标场景，失败时旧索引仍可用。"""
        if scenario_id is not None:
            _scenario(scenario_id)
        ids = set()
        required = (
            "id", "document_id", "scenario_id", "title", "text", "url",
            "fetched_at", "content_hash",
        )
        for chunk in chunks:
            if any(
                not isinstance(chunk.get(key), str) or not chunk[key].strip() for key in required
            ):
                raise ValueError(f"政策切块缺少非空字段：{', '.join(required)}")
            _scenario(chunk["scenario_id"])
            if scenario_id and chunk["scenario_id"] != scenario_id:
                raise ValueError("重建场景与切块的 scenario_id 不一致。")
            if chunk["id"] in ids:
                raise ValueError(f"重复的政策切块 ID：{chunk['id']}")
            ids.add(chunk["id"])
        # 过期或暂未生效的原始文档仍留在采集快照；生效后重建即可纳入索引。
        chunks = [chunk for chunk in chunks if _is_current(chunk)]
        payloads = [_json(chunk) for chunk in chunks]
        texts = [_retrieval_text(chunk) for chunk in chunks]
        embeddings = self._embed(texts, allow_download=True) if texts else []
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC).isoformat()
        initialize_database(self.database)
        with self._connect() as connection, connection.transaction():
            self._lock_namespace(connection)
            signature = self._signature(connection)
            if signature not in (None, INDEX_SIGNATURE) and scenario_id is not None:
                raise RuntimeError("向量模型或索引格式已改变，请不指定场景重建全部知识库。")
            where = "namespace = %s"
            args = [self.namespace]
            if scenario_id is not None:
                where += " AND scenario_id = %s"
                args.append(scenario_id)
            # 词频随切块级联删除；向量与原文同一行，发布时不会出现半套索引。
            connection.execute(f"DELETE FROM rag_chunks WHERE {where}", args)
            connection.execute(f"DELETE FROM rag_corpora WHERE {where}", args)
            for chunk, embedding, text in zip(chunks, embeddings, texts):
                frequencies = Counter(_terms(text))
                row = connection.execute(
                    "INSERT INTO rag_chunks "
                    "(namespace, id, scenario_id, payload, embedding, is_privacy, term_count) "
                    "VALUES (%s, %s, %s, %s, %s::public.vector, %s, %s) RETURNING chunk_no",
                    (
                        self.namespace, chunk["id"], chunk["scenario_id"], Jsonb(chunk), embedding,
                        chunk.get("document_type") == "privacy_policy", sum(frequencies.values()),
                    ),
                ).fetchone()
                if frequencies:
                    with connection.cursor() as cursor:
                        cursor.executemany(
                            "INSERT INTO rag_chunk_terms (namespace, chunk_no, term, frequency) "
                            "VALUES (%s, %s, %s, %s)",
                            [(self.namespace, row["chunk_no"], term, count)
                             for term, count in frequencies.items()],
                        )
            for scenario in sorted({c["scenario_id"] for c in chunks}):
                members = sorted(
                    p for c, p in zip(chunks, payloads) if c["scenario_id"] == scenario
                )
                version = hashlib.sha256("\n".join(members).encode()).hexdigest()
                connection.execute(
                    "INSERT INTO rag_corpora "
                    "(namespace, scenario_id, version, indexed_at, chunk_count) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (self.namespace, scenario, version, now, len(members)),
                )
            connection.execute(
                "INSERT INTO rag_index_meta (namespace, key, value) VALUES (%s, 'signature', %s) "
                "ON CONFLICT (namespace, key) DO UPDATE SET value = EXCLUDED.value",
                (self.namespace, INDEX_SIGNATURE),
            )
        return self.status()

    def _status(self, connection, scenarios: list[str] | None = None) -> dict:
        signature = self._signature(connection)
        state = {"status": "no_data", "model": MODEL_NAME, "dimensions": DIMENSIONS, "scenarios": []}
        if signature is None:
            return state
        if signature != INDEX_SIGNATURE:
            return {**state, "status": "rebuild_required"}
        where = "namespace = %s"
        args = [self.namespace]
        if scenarios is not None:
            where += " AND scenario_id = ANY(%s)"
            args.append(scenarios)
        corpora = connection.execute(
            "SELECT scenario_id, version, indexed_at, chunk_count "
            f"FROM rag_corpora WHERE {where} ORDER BY scenario_id", args,
        ).fetchall()
        return {**state, "status": "ready" if corpora else "no_data", "scenarios": corpora}

    def status(self) -> dict:
        with self._read_snapshot() as connection:
            return self._status(connection)

    def snapshot(self, scenarios: list[str] | None = None) -> dict:
        """在同一只读快照中导出语料版本与原文，供冻结评测验证。"""
        if scenarios is not None:
            scenarios = list(scenarios)
            for scenario in scenarios:
                _scenario(scenario)
        with self._read_snapshot() as connection:
            state = self._status(connection, scenarios)
            state.update(
                identity=self.identity, namespace=self.namespace,
                signature=self._signature(connection), chunks=[],
            )
            if state["status"] != "ready":
                return state
            where = "namespace = %s"
            args = [self.namespace]
            if scenarios is not None:
                where += " AND scenario_id = ANY(%s)"
                args.append(scenarios)
            for row in connection.execute(
                f"SELECT id, scenario_id, payload FROM rag_chunks WHERE {where} ORDER BY id", args,
            ).fetchall():
                payload = row["payload"]
                if (
                    not isinstance(payload, dict)
                    or payload.get("id") != row["id"]
                    or payload.get("scenario_id") != row["scenario_id"]
                ):
                    raise ValueError("索引行标识与政策原文不一致，请重新构建知识库。")
                state["chunks"].append(payload)
            return state

    def corpus(self, scenario_id: str) -> dict | None:
        """读取单个场景的语料版本；未索引或格式不匹配时返回空。"""
        _scenario(scenario_id)
        with self._read_snapshot() as connection:
            scenarios = self._status(connection, [scenario_id])["scenarios"]
            return scenarios[0] if scenarios else None

    def clear(self) -> None:
        """清理当前命名空间，供临时评测结束时释放索引。"""
        with self._connect() as connection, connection.transaction():
            if self._signature(connection) is None:
                return
            self._lock_namespace(connection)
            for table in ("rag_chunks", "rag_corpora", "rag_index_meta"):
                connection.execute(f"DELETE FROM {table} WHERE namespace = %s", (self.namespace,))

    def _scope(self, scenario_id: str, is_privacy: bool) -> tuple[str, tuple]:
        # 在 Top-K 之前再次检查有效期，避免过期原文占满候选池。
        current = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        return (
            ("c.namespace = %s AND c.scenario_id = %s AND c.is_privacy = %s "
            "AND COALESCE(c.payload->>'source_status', '') NOT IN ('stale', 'failed', 'superseded') "
            "AND (NULLIF(left(c.payload->>'valid_from', 10), '')::date IS NULL "
            "OR NULLIF(left(c.payload->>'valid_from', 10), '')::date <= %s) "
            "AND (NULLIF(left(c.payload->>'valid_until', 10), '')::date IS NULL "
            "OR NULLIF(left(c.payload->>'valid_until', 10), '')::date >= %s)"),
            (self.namespace, scenario_id, is_privacy, current, current),
        )

    def _vector_candidates(self, connection, vector, scenario_id, is_privacy, candidates):
        scope, args = self._scope(scenario_id, is_privacy)
        return connection.execute(
            "SELECT c.chunk_no, c.embedding OPERATOR(public.<=>) %s::public.vector AS distance "
            f"FROM rag_chunks c WHERE {scope} ORDER BY distance, c.chunk_no LIMIT %s",
            (vector, *args, candidates),
        ).fetchall()

    def _lexical_candidates(self, connection, terms, scenario_id, is_privacy, candidates):
        """按 BM25 公式计算词项相关性，统计范围为当前命名空间的完整语料。"""
        scope, args = self._scope(scenario_id, is_privacy)
        return connection.execute(
            "WITH corpus_stats AS ("
            " SELECT count(*)::double precision AS documents, avg(term_count) AS average_length"
            " FROM rag_chunks WHERE namespace = %s"
            "), document_frequencies AS ("
            " SELECT term, count(*)::double precision AS documents"
            " FROM rag_chunk_terms WHERE namespace = %s AND term = ANY(%s) GROUP BY term"
            ") SELECT c.chunk_no, -sum("
            " greatest(ln((s.documents - d.documents + 0.5) / (d.documents + 0.5)), 0.000001)"
            " * (t.frequency * 2.2)"
            " / (t.frequency + 1.2 * (0.25 + 0.75 * c.term_count / nullif(s.average_length, 0)))"
            ") AS score FROM rag_chunks c"
            " JOIN rag_chunk_terms t ON t.chunk_no = c.chunk_no AND t.namespace = c.namespace"
            " JOIN document_frequencies d ON d.term = t.term CROSS JOIN corpus_stats s"
            f" WHERE {scope} GROUP BY c.chunk_no ORDER BY score, c.chunk_no LIMIT %s",
            (self.namespace, self.namespace, terms, *args, candidates),
        ).fetchall()

    def search(self, query: str, scenario_id: str, limit: int = 8) -> dict:
        """返回相关原文和来源；一次自然语言检索内部包含向量与关键词两路。"""
        _scenario(scenario_id)
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 1000:
            raise ValueError("政策查询必须是 1–1000 字的非空文本。")
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("limit 必须是 1–10 的整数。")
        query = query.strip()
        data = {"query": query, "scenario_id": scenario_id, "clauses": []}
        result = {"status": "no_data", "data": data, "evidence": [], "retryable": False}
        with self._read_snapshot() as connection:
            signature = self._signature(connection)
            if signature is None:
                data["reason"] = "knowledge_not_indexed"
                return result
            if signature != INDEX_SIGNATURE:
                data["reason"] = "index_rebuild_required"
                return result
            corpus = connection.execute(
                "SELECT version, indexed_at FROM rag_corpora "
                "WHERE namespace = %s AND scenario_id = %s", (self.namespace, scenario_id),
            ).fetchone()
            if not corpus:
                data["reason"] = "scenario_not_indexed"
                return result
            data.update(
                corpus_version=corpus["version"], indexed_at=corpus["indexed_at"], model=MODEL_NAME,
                retrieval="pgvector cosine + BM25, reciprocal rank fusion",
            )
            vector = self._embed([QUERY_PREFIX + query])[0]
            # 返回条数只控制证据长度，不改变两路候选池和融合排名。
            candidates = 20
            terms = list(dict.fromkeys(_terms(query)))[:64]
            include_privacy = bool(
                _PRIVACY_INTENT.search(query)
                or (
                    scenario_id == "financial_consultation"
                    and _FINANCIAL_ACCOUNT_INTENT.search(query)
                )
            )
            data["retrieval_scopes"] = ["business", "privacy"] if include_privacy else ["business"]
            vector_rows, lexical_rows = [], []
            # 在两路 Top-K 之前限定范围；隐私问题分组召回，长协议不会吃掉业务候选。
            for is_privacy in [False, True] if include_privacy else [False]:
                vector_rows.extend(self._vector_candidates(
                    connection, vector, scenario_id, is_privacy, candidates,
                ))
                if terms:
                    lexical_rows.extend(self._lexical_candidates(
                        connection, terms, scenario_id, is_privacy, candidates,
                    ))
            # 对关键词补入的候选也计算向量排名，避免“两路都出现”胜过明显更相关的原文。
            vector_ids = {row["chunk_no"] for row in vector_rows}
            for row in lexical_rows:
                if row["chunk_no"] not in vector_ids:
                    vector_rows.append(connection.execute(
                        "SELECT chunk_no, embedding OPERATOR(public.<=>) %s::public.vector AS distance "
                        "FROM rag_chunks WHERE namespace = %s AND chunk_no = %s",
                        (vector, self.namespace, row["chunk_no"]),
                    ).fetchone())
                    vector_ids.add(row["chunk_no"])
            vector_rows.sort(key=lambda row: (row["distance"], row["chunk_no"]))
            lexical_rows.sort(key=lambda row: (row["score"], row["chunk_no"]))
            # RRF 按名次相加，避免直接混合量纲不同的 BM25 与余弦分数。
            ranks: dict[int, dict] = {}
            for route, rows in (("vector", vector_rows), ("lexical", lexical_rows)):
                for rank, row in enumerate(rows, 1):
                    item = ranks.setdefault(row["chunk_no"], {"rrf_score": 0.0})
                    item[f"{route}_rank"] = rank
                    item["rrf_score"] += 1 / (60 + rank)
            for chunk_no, rank in sorted(
                ranks.items(), key=lambda pair: (-pair[1]["rrf_score"], pair[0])
            ):
                row = connection.execute(
                    "SELECT payload, embedding OPERATOR(public.<=>) %s::public.vector AS distance "
                    "FROM rag_chunks WHERE namespace = %s AND chunk_no = %s AND scenario_id = %s",
                    (vector, self.namespace, chunk_no, scenario_id),
                ).fetchone()
                similarity = 1 - row["distance"]
                if similarity < MIN_SIMILARITY:
                    continue
                clause = row["payload"]
                if not _is_current(clause):
                    continue
                clause["retrieval"] = {**rank, "cosine_similarity": round(similarity, 4)}
                data["clauses"].append(clause)
            ranked = data["clauses"]
            selected = ranked[:limit]
            # 混合问题的两类证据都相关时各保留一条；不让弱相关业务条款挤掉隐私原文。
            if include_privacy and limit > 1 and ranked:
                best_similarity = max(c["retrieval"]["cosine_similarity"] for c in ranked)
                for privacy in (False, True):
                    if any(
                        (c.get("document_type") == "privacy_policy") == privacy for c in selected
                    ):
                        continue
                    candidate = next(
                        (
                            c
                            for c in ranked
                            if (c.get("document_type") == "privacy_policy") == privacy
                            and c["retrieval"]["cosine_similarity"] >= best_similarity - 0.10
                        ),
                        None,
                    )
                    if candidate:
                        selected[-1:] = [candidate]
            data["clauses"] = [c for c in ranked if c in selected]
            result["evidence"] = [c["id"] for c in data["clauses"]]
        if data["clauses"]:
            result["status"] = "success"
            data["usage"] = (
                "相关性分数不是正确率。核对原文的地区、对象和时间；证据不足时继续检索或明确未知。"
            )
            if any(c.get("partial_sentence") for c in data["clauses"]):
                data["usage"] += (
                    "标记 partial_sentence 的片段是超长句的一部分，不能独立作为完整依据；"
                    "需结合相关片段核对完整条件。"
                )
        else:
            data["reason"] = "no_relevant_clauses"
        return result
