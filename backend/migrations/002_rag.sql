-- 向量扩展安装在公共命名空间，测试与评测的隔离 schema 共用扩展类型和运算符。
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;

CREATE TABLE rag_index_meta (
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (namespace, key)
);

CREATE TABLE rag_corpora (
    namespace TEXT NOT NULL,
    scenario_id TEXT NOT NULL,
    version TEXT NOT NULL,
    indexed_at TEXT NOT NULL,
    chunk_count INTEGER NOT NULL CHECK (chunk_count >= 0),
    PRIMARY KEY (namespace, scenario_id)
);

-- 原文、向量与检索范围同一行发布；业务 ID 在每个索引命名空间内唯一。
CREATE TABLE rag_chunks (
    chunk_no BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    namespace TEXT NOT NULL,
    id TEXT NOT NULL,
    scenario_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    embedding public.vector(512) NOT NULL,
    is_privacy BOOLEAN NOT NULL,
    term_count INTEGER NOT NULL CHECK (term_count >= 0),
    UNIQUE (namespace, id),
    UNIQUE (namespace, chunk_no)
);
CREATE INDEX rag_chunks_scope ON rag_chunks (namespace, scenario_id, is_privacy);

-- 中文双字词与英文整词在应用中生成，保存词频用于精确计算原有 BM25 公式。
-- 不使用近似向量索引，避免迁移时同时改变候选召回率。
CREATE TABLE rag_chunk_terms (
    namespace TEXT NOT NULL,
    chunk_no BIGINT NOT NULL,
    term TEXT NOT NULL,
    frequency INTEGER NOT NULL CHECK (frequency > 0),
    PRIMARY KEY (chunk_no, term),
    FOREIGN KEY (namespace, chunk_no) REFERENCES rag_chunks(namespace, chunk_no) ON DELETE CASCADE
);
CREATE INDEX rag_terms_lookup ON rag_chunk_terms (namespace, term);
