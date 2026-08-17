-- 智能文档问答系统 rag_kb 库初始化（多租户版）
-- 占位符 __EMBED_DIM__ 由 scripts/init_db.py 按所选 embedding 模型维度替换

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 用户表（必须最先创建，多张表引用）
CREATE TABLE IF NOT EXISTS kb_user (
    id BIGSERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    password_hash VARCHAR(100) NOT NULL,    -- BCrypt(60字符)；旧版 SHA-256 已废弃
    salt VARCHAR(32) DEFAULT '',            -- 遗留列：BCrypt 自带盐，不再写入
    role VARCHAR(20) DEFAULT 'user',        -- user / admin
    created_at TIMESTAMP DEFAULT NOW()
);

-- Java 网关：审计
CREATE TABLE IF NOT EXISTS kb_audit_log (
    id BIGSERIAL PRIMARY KEY,
    username VARCHAR(50),
    action VARCHAR(200),
    target VARCHAR(500),
    status_code INT,
    elapsed_ms INT,
    created_at TIMESTAMP DEFAULT NOW()
);

-- 文档与解析
CREATE TABLE IF NOT EXISTS kb_document (
    id BIGSERIAL PRIMARY KEY,
    filename VARCHAR(500) NOT NULL,
    doc_type VARCHAR(20) NOT NULL,          -- pdf/word/markdown/image
    file_hash CHAR(64) NOT NULL,            -- 内容 hash（非 UNIQUE，同文件共享）
    page_count INT, char_count INT,
    status SMALLINT DEFAULT 0,              -- 0解析中 1已入库 2失败 3已下线
    error TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_document_hash ON kb_document(file_hash);

-- 用户-文档映射（多租户隔离 + 同文件共享）
CREATE TABLE IF NOT EXISTS kb_user_document (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES kb_user(id),
    document_id BIGINT NOT NULL REFERENCES kb_document(id),
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(user_id, document_id)
);

CREATE TABLE IF NOT EXISTS kb_chunk (
    id BIGSERIAL PRIMARY KEY,
    document_id BIGINT REFERENCES kb_document(id) ON DELETE CASCADE,
    chunk_type VARCHAR(10) NOT NULL,        -- text/table/image
    page_no INT, seq INT,
    content TEXT NOT NULL, chars INT,
    embedding vector(__EMBED_DIM__),
    embed_model VARCHAR(60),                -- 向量化所用模型（E1 实验对比用）
    embedding2 vector(1792),                -- E1 实验列：ritrieve-zh-v1（1792 维）
    status SMALLINT DEFAULT 0,              -- 0待向量化 1已向量化
    meta JSONB
);
CREATE INDEX IF NOT EXISTS idx_chunk_embedding ON kb_chunk
    USING hnsw (embedding vector_cosine_ops) WHERE status = 1;
CREATE INDEX IF NOT EXISTS idx_chunk_embedding2 ON kb_chunk
    USING hnsw (embedding2 vector_cosine_ops) WHERE embedding2 IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_chunk_doc ON kb_chunk(document_id);

-- 问答与日志
CREATE TABLE IF NOT EXISTS qa_session (
    id VARCHAR(50) PRIMARY KEY,
    user_id BIGINT REFERENCES kb_user(id),
    created_at TIMESTAMP DEFAULT NOW(),
    summary TEXT
);

CREATE TABLE IF NOT EXISTS qa_log (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES kb_user(id),
    session_id VARCHAR(50), trace_id VARCHAR(50),
    query TEXT, answer TEXT,
    route VARCHAR(20),                      -- simple/standard/complex/out_of_scope
    chunk_ids BIGINT[],
    total_ms INT, token_in INT DEFAULT 0, token_out INT DEFAULT 0,
    thinking BOOLEAN DEFAULT FALSE,
    cache_hit BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS retrieval_log (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES kb_user(id),
    trace_id VARCHAR(50), query TEXT,
    hit_count INT, top_score FLOAT, low_confidence BOOLEAN DEFAULT FALSE,
    stage_ms JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);

-- 反馈闭环
CREATE TABLE IF NOT EXISTS feedback (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES kb_user(id),
    qa_log_id BIGINT REFERENCES qa_log(id),
    rating SMALLINT,                        -- 1 赞 / -1 踩
    correction TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bad_case (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES kb_user(id),
    qa_log_id BIGINT, query TEXT,
    snapshot JSONB,                         -- 完整链路快照（query/检索结果/答案）
    attribution VARCHAR(20) DEFAULT 'pending',   -- retrieval/generation/pending
    status VARCHAR(20) DEFAULT 'open',      -- open/confirmed/in_regression/rejected
    created_at TIMESTAMP DEFAULT NOW()
);

-- 评估体系
CREATE TABLE IF NOT EXISTS eval_question (
    id BIGSERIAL PRIMARY KEY,
    question TEXT NOT NULL, reference_answer TEXT,
    source_chunk_ids BIGINT[],
    dimension VARCHAR(20) NOT NULL,         -- factual/table/cross_page/refuse
    in_regression BOOLEAN DEFAULT FALSE,
    meta JSONB,                             -- evidence_keywords 等
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS eval_run (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(100), config JSONB,        -- 运行时的管线配置快照
    metrics JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS eval_result (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT REFERENCES eval_run(id) ON DELETE CASCADE,
    question_id BIGINT REFERENCES eval_question(id),
    scores JSONB, retrieved_chunk_ids BIGINT[],
    answer TEXT
);

-- Prompt 管理与审批
CREATE TABLE IF NOT EXISTS prompt_registry (
    id BIGSERIAL PRIMARY KEY,
    code VARCHAR(50) UNIQUE NOT NULL,       -- route/rewrite/grade/reflect/generate/diagnosis...
    content TEXT NOT NULL, version INT DEFAULT 1,
    status SMALLINT DEFAULT 1,
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS prompt_approval (
    id BIGSERIAL PRIMARY KEY,
    prompt_code VARCHAR(50), old_content TEXT, new_content TEXT,
    eval_compare JSONB,
    decision VARCHAR(20) DEFAULT 'pending', -- pending/approved/rejected
    decided_at TIMESTAMP, created_at TIMESTAMP DEFAULT NOW()
);

-- 记忆（多租户：user_id 关联 kb_user.id）
CREATE TABLE IF NOT EXISTS memory_entry (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES kb_user(id),
    mem_type VARCHAR(20),                   -- focus/open_question/preference
    content TEXT, embedding vector(__EMBED_DIM__),
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_memory_embedding ON memory_entry
    USING hnsw (embedding vector_cosine_ops) WHERE embedding IS NOT NULL;

CREATE TABLE IF NOT EXISTS diagnosis_report (
    id BIGSERIAL PRIMARY KEY,
    summary TEXT, metrics JSONB, anomalies JSONB, suggestions JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);

-- LangGraph Checkpointer 表由 langgraph-checkpoint-postgres 自动创建
