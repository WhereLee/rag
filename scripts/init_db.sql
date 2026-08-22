-- 智能文档问答系统 rag_kb 库初始化（v2 统一 schema：单文件可构建完整库）
-- 幂等可重复执行；占位符 __EMBED_DIM__ 由 scripts/init_db.py 按所选 embedding 模型维度替换
-- 设计说明：
--   - 旧链路表（kb_document/kb_chunk/kb_user_document）已下线，新链路（user_file/rag_chunk/parse_tasks）为唯一数据源
--   - 所有表统一 TIMESTAMPTZ（避免跨表时区语义混乱）
--   - 失败块闭环（issue_items/ingest_job）直接挂 file_id（user_file 维度），不再依赖旧表

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 用户表（BCrypt 自带盐；role 仅 admin/user）
CREATE TABLE IF NOT EXISTS kb_user (
    id BIGSERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    password_hash VARCHAR(100) NOT NULL,    -- BCrypt(60字符)
    role VARCHAR(20) DEFAULT 'user',        -- user / admin
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Java 网关：审计
CREATE TABLE IF NOT EXISTS kb_audit_log (
    id BIGSERIAL PRIMARY KEY,
    username VARCHAR(50),
    action VARCHAR(200),
    target VARCHAR(500),
    status_code INT,
    elapsed_ms INT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON kb_audit_log(created_at);

-- 单层目录（目录名同用户唯一；非空目录禁删由应用层约束）
CREATE TABLE IF NOT EXISTS user_dir (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES kb_user(id),
    name VARCHAR(100) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, name)
);

-- 物理文件表：内容级去重单元（sha256 唯一）
-- owner_user_id = 第一个上传者（物理文件所在目录），秒传共享后删除/清理都按 owner 找路径
CREATE TABLE IF NOT EXISTS file_blob (
    id BIGSERIAL PRIMARY KEY,
    file_hash VARCHAR(64) NOT NULL UNIQUE,
    stored_name VARCHAR(300) NOT NULL,
    file_size BIGINT NOT NULL,
    ref_count INT NOT NULL DEFAULT 0,
    owner_user_id BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 用户文件（私人文件管理：上传/列表/删除/回收站/下载；blob 秒传去重）
CREATE TABLE IF NOT EXISTS user_file (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES kb_user(id),
    blob_id BIGINT REFERENCES file_blob(id),
    filename VARCHAR(255) NOT NULL,           -- 原始文件名（展示用）
    file_size BIGINT NOT NULL DEFAULT 0,
    content_type VARCHAR(100) DEFAULT '',
    status SMALLINT DEFAULT 1,                -- 1正常 0已删除
    dir_id BIGINT REFERENCES user_dir(id),
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    -- 同名并发双插兜底（应用层 SELECT 预检 + 唯一约束双保险）
    UNIQUE (user_id, filename)
);
-- 注：idx_user_file_user / dir / deleted 索引在 init_chunk.sql 统一创建（存量库需先补列再建索引）

-- 解析任务（worker 消费；file_id 主键=幂等；parsing 停留超时回收=崩溃恢复）
CREATE TABLE IF NOT EXISTS parse_tasks (
    file_id BIGINT PRIMARY KEY REFERENCES user_file(id) ON DELETE CASCADE,
    status VARCHAR(20) DEFAULT 'pending',     -- pending/parsing/success/partial/failed
    attempt INT DEFAULT 0,
    error TEXT,
    duration_ms INT,
    node_count INT,
    chunk_count INT,
    stage TEXT,
    progress REAL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 检索块（新链路）：块随文件级联清理（回收站过期物理删 user_file 不再撞外键）；
-- 软删文件查询期过滤（JOIN user_file WHERE status=1），块表不做物理删
CREATE TABLE IF NOT EXISTS rag_chunk (
    id BIGSERIAL PRIMARY KEY,
    file_id BIGINT NOT NULL REFERENCES user_file(id) ON DELETE CASCADE,
    chunk_type VARCHAR(10) NOT NULL,
    seq INT NOT NULL,
    content TEXT NOT NULL,
    chars INT NOT NULL,
    heading_path TEXT NOT NULL DEFAULT '',
    page_no INT,
    embedding vector(__EMBED_DIM__),
    embed_model VARCHAR(60),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (file_id, seq)                     -- 幂等重解析（ON CONFLICT DO UPDATE）
);
CREATE INDEX IF NOT EXISTS idx_rag_chunk_file ON rag_chunk(file_id);
CREATE INDEX IF NOT EXISTS idx_rag_chunk_embedding
  ON rag_chunk USING hnsw (embedding vector_cosine_ops) WHERE embedding IS NOT NULL;

-- 分片上传会话（断点续传；同片重传用 ON CONFLICT 覆盖）
CREATE TABLE IF NOT EXISTS upload_session (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    file_hash VARCHAR(64) NOT NULL,
    filename VARCHAR(300) NOT NULL,
    file_size BIGINT NOT NULL,
    chunk_size BIGINT NOT NULL,
    chunk_count INT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'uploading',   -- uploading/completed
    dir_id BIGINT REFERENCES user_dir(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_upload_session_user ON upload_session (user_id, status, updated_at);

CREATE TABLE IF NOT EXISTS upload_chunk (
    id BIGSERIAL PRIMARY KEY,
    session_id BIGINT NOT NULL REFERENCES upload_session(id) ON DELETE CASCADE,
    chunk_index INT NOT NULL,
    chunk_size BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, chunk_index)
);

-- 会话与问答日志
CREATE TABLE IF NOT EXISTS qa_session (
    id VARCHAR(50) PRIMARY KEY,
    user_id BIGINT REFERENCES kb_user(id),
    dir_id BIGINT REFERENCES user_dir(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    summary TEXT
);

CREATE TABLE IF NOT EXISTS qa_log (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES kb_user(id),
    session_id VARCHAR(50), trace_id VARCHAR(50),
    query TEXT, answer TEXT,
    route VARCHAR(20),                      -- qa/agent/simple/standard/complex/out_of_scope...
    chunk_ids BIGINT[],
    total_ms INT, token_in INT DEFAULT 0, token_out INT DEFAULT 0,
    thinking BOOLEAN DEFAULT FALSE,
    cache_hit BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_qa_log_session ON qa_log(session_id);

CREATE TABLE IF NOT EXISTS retrieval_log (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES kb_user(id),
    trace_id VARCHAR(50), query TEXT,
    hit_count INT, top_score FLOAT, low_confidence BOOLEAN DEFAULT FALSE,
    stage_ms JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_retrieval_trace ON retrieval_log(trace_id);

-- 问答存档：精确命中（query_hash）+ 语义命中（query_embedding）两级复用
-- 跨用户复用受 file_ids 同 blob 边界约束（防经缓存泄露他人文件）
CREATE TABLE IF NOT EXISTS qa_cache (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES kb_user(id),
    query_hash CHAR(32) NOT NULL,
    query TEXT NOT NULL,
    query_embedding vector(__EMBED_DIM__),
    answer TEXT NOT NULL,
    chunk_ids BIGINT[] NOT NULL DEFAULT '{}',
    file_ids BIGINT[] NOT NULL DEFAULT '{}',
    hit_count INT NOT NULL DEFAULT 0,
    invalidated BOOLEAN NOT NULL DEFAULT FALSE,
    cache_shared_from BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, query_hash)
);
CREATE INDEX IF NOT EXISTS idx_qa_cache_embedding
  ON qa_cache USING hnsw (query_embedding vector_cosine_ops) WHERE query_embedding IS NOT NULL;

-- 反馈闭环
CREATE TABLE IF NOT EXISTS feedback (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES kb_user(id),
    qa_log_id BIGINT REFERENCES qa_log(id),
    rating SMALLINT,                        -- 1 赞 / -1 踩
    correction TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bad_case (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES kb_user(id),
    qa_log_id BIGINT, query TEXT,
    snapshot JSONB,                         -- 完整链路快照
    attribution VARCHAR(20) DEFAULT 'pending',   -- retrieval/generation/pending
    status VARCHAR(20) DEFAULT 'open',      -- open/confirmed/in_regression/rejected
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 评估体系
CREATE TABLE IF NOT EXISTS eval_question (
    id BIGSERIAL PRIMARY KEY,
    question TEXT NOT NULL, reference_answer TEXT,
    source_chunk_ids BIGINT[],
    dimension VARCHAR(20) NOT NULL,         -- factual/table/cross_page/refuse
    in_regression BOOLEAN DEFAULT FALSE,
    meta JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS eval_run (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(100), config JSONB,        -- 运行时的管线配置快照
    metrics JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS eval_result (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT REFERENCES eval_run(id) ON DELETE CASCADE,
    question_id BIGINT REFERENCES eval_question(id),
    scores JSONB, retrieved_chunk_ids BIGINT[],
    answer TEXT
);
CREATE INDEX IF NOT EXISTS idx_eval_result_run ON eval_result(run_id);

-- Prompt 管理与审批
CREATE TABLE IF NOT EXISTS prompt_registry (
    id BIGSERIAL PRIMARY KEY,
    code VARCHAR(50) UNIQUE NOT NULL,       -- route/rewrite/grade/reflect/generate/diagnosis...
    content TEXT NOT NULL, version INT DEFAULT 1,
    status SMALLINT DEFAULT 1,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS prompt_approval (
    id BIGSERIAL PRIMARY KEY,
    prompt_code VARCHAR(50), old_content TEXT, new_content TEXT,
    eval_compare JSONB,
    decision VARCHAR(20) DEFAULT 'pending', -- pending/approved/rejected
    decided_at TIMESTAMPTZ, created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 长期记忆（跨会话关注点，语义召回）
CREATE TABLE IF NOT EXISTS memory_entry (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES kb_user(id),
    mem_type VARCHAR(20),                   -- focus/open_question/preference
    content TEXT, embedding vector(__EMBED_DIM__),
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_memory_embedding ON memory_entry
    USING hnsw (embedding vector_cosine_ops) WHERE embedding IS NOT NULL;

-- 失败块闭环（v2：挂 file_id 维度，与解析任务 parse_tasks 解耦）
-- 占位节点 = 解析失败的块（如 VLM 图片识别失败），用户可选重试/替代图/文字描述恢复
CREATE TABLE IF NOT EXISTS issue_items (
    id BIGSERIAL PRIMARY KEY,
    file_id BIGINT NOT NULL REFERENCES user_file(id) ON DELETE CASCADE,
    page_no INT NOT NULL,
    block_order INT DEFAULT 0,
    block_type VARCHAR(10) NOT NULL,
    reason TEXT NOT NULL,
    bbox JSONB,
    status VARCHAR(20) DEFAULT 'pending',   -- pending/retrying/resolved/failed/skipped/cancelled
    resolution TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (file_id, page_no, block_order, block_type)   -- 重复解析幂等
);
-- 注：idx_issue_file 索引在 init_chunk.sql 统一创建（存量库 issue_items 需先重建为 v2 再建索引）

-- 块重试任务（替代图替换场景专用：job_type 恒为 block_retry）
-- job_key 幂等键唯一约束防并发双插；lease_until 双语义（running=租约/queued=退避）
CREATE TABLE IF NOT EXISTS ingest_job (
    id BIGSERIAL PRIMARY KEY,
    job_key VARCHAR(100) UNIQUE NOT NULL,          -- sha256(user_id:file_id:issue_id)
    user_id BIGINT NOT NULL REFERENCES kb_user(id),
    file_id BIGINT NOT NULL REFERENCES user_file(id),
    file_hash CHAR(64) NOT NULL,
    filename VARCHAR(500) NOT NULL,
    doc_type VARCHAR(20) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'queued',  -- queued/running/done/failed/dead
    stage VARCHAR(30) NOT NULL DEFAULT 'pending',  -- pending/parsing/chunking/embedding/indexing
    progress FLOAT DEFAULT 0,
    step_detail JSONB,
    attempt INT DEFAULT 0,
    max_attempts INT DEFAULT 3,
    lease_until TIMESTAMPTZ,
    error TEXT,
    file_path VARCHAR(500),
    trace_id VARCHAR(50),
    job_type VARCHAR(20) DEFAULT 'block_retry',
    issue_id BIGINT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ingest_job_status ON ingest_job(status, lease_until);
CREATE INDEX IF NOT EXISTS idx_ingest_job_user ON ingest_job(user_id, created_at);

-- 诊断报告（系统健康巡检落库）
CREATE TABLE IF NOT EXISTS diagnosis_report (
    id BIGSERIAL PRIMARY KEY,
    summary TEXT, metrics JSONB, anomalies JSONB, suggestions JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- LangGraph Checkpointer 表由 langgraph-checkpoint-postgres 自动创建
