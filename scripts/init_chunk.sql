-- RAG 检索入库表：rag_chunk（C2 计划）+ 存量库 v1→v2 升级
-- 幂等可重复执行（CREATE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS / DO 条件迁移）
-- 使用方式：新库/存量库统一顺序执行 init_db.sql → init_chunk.sql

-- ========== 1. rag_chunk（新链路检索块；块随文件级联清理） ==========
CREATE TABLE IF NOT EXISTS rag_chunk (
  id BIGSERIAL PRIMARY KEY,
  file_id BIGINT NOT NULL REFERENCES user_file(id) ON DELETE CASCADE,
  chunk_type VARCHAR(10) NOT NULL,
  seq INT NOT NULL,
  content TEXT NOT NULL,
  chars INT NOT NULL,
  heading_path TEXT NOT NULL DEFAULT '',
  page_no INT,
  embedding vector(768),
  embed_model VARCHAR(60),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (file_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_rag_chunk_file ON rag_chunk(file_id);
CREATE INDEX IF NOT EXISTS idx_rag_chunk_embedding
  ON rag_chunk USING hnsw (embedding vector_cosine_ops) WHERE embedding IS NOT NULL;

-- 存量 rag_chunk 外键重建为 CASCADE（旧定义无级联，回收站过期清理会撞外键）
DO $$
DECLARE
    fk_name text;
BEGIN
    SELECT conname INTO fk_name FROM pg_constraint
    WHERE conrelid='rag_chunk'::regclass AND contype='f'
      AND NOT pg_get_constraintdef(oid) ILIKE '%CASCADE%'
    LIMIT 1;
    IF fk_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE rag_chunk DROP CONSTRAINT %I', fk_name);
        ALTER TABLE rag_chunk ADD CONSTRAINT rag_chunk_file_id_fkey
            FOREIGN KEY (file_id) REFERENCES user_file(id) ON DELETE CASCADE;
    END IF;
END $$;

-- ========== 2. 单层目录 + 会话/文件目录绑定 ==========
CREATE TABLE IF NOT EXISTS user_dir (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES kb_user(id),
  name VARCHAR(100) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, name)
);

ALTER TABLE user_file ADD COLUMN IF NOT EXISTS dir_id BIGINT REFERENCES user_dir(id);
ALTER TABLE qa_session ADD COLUMN IF NOT EXISTS dir_id BIGINT REFERENCES user_dir(id);
CREATE INDEX IF NOT EXISTS idx_user_file_user ON user_file(user_id, status);
CREATE INDEX IF NOT EXISTS idx_user_file_dir ON user_file(dir_id) WHERE dir_id IS NOT NULL;

-- ========== 3. 问答存档（两级复用） + 语义 HNSW ==========
CREATE TABLE IF NOT EXISTS qa_cache (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES kb_user(id),
  query_hash CHAR(32) NOT NULL,
  query TEXT NOT NULL,
  query_embedding vector(768),
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

-- ========== 4. 存量库补列（秒传/回收站/分片） ==========
ALTER TABLE user_file ADD COLUMN IF NOT EXISTS blob_id BIGINT;
ALTER TABLE user_file ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_user_file_deleted ON user_file (user_id, status, deleted_at);

-- 分片上传会话表（先建表再补列，保证新库/存量库顺序一致）
CREATE TABLE IF NOT EXISTS upload_session (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    file_hash VARCHAR(64) NOT NULL,
    filename VARCHAR(300) NOT NULL,
    file_size BIGINT NOT NULL,
    chunk_size BIGINT NOT NULL,
    chunk_count INT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'uploading',
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

-- 存量库补列（新库已有，IF NOT EXISTS 跳过）
ALTER TABLE upload_session ADD COLUMN IF NOT EXISTS dir_id BIGINT REFERENCES user_dir(id);

-- 物理文件表（秒传去重单元；owner=首个上传者）
CREATE TABLE IF NOT EXISTS file_blob (
    id BIGSERIAL PRIMARY KEY,
    file_hash VARCHAR(64) NOT NULL UNIQUE,
    stored_name VARCHAR(300) NOT NULL,
    file_size BIGINT NOT NULL,
    ref_count INT NOT NULL DEFAULT 0,
    owner_user_id BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 解析任务表（worker 消费；file_id 主键=幂等；stage/progress 进度回报）
CREATE TABLE IF NOT EXISTS parse_tasks (
    file_id BIGINT PRIMARY KEY REFERENCES user_file(id) ON DELETE CASCADE,
    status VARCHAR(20) DEFAULT 'pending',
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

-- ========== 5. user_file 同名唯一：全量约束 → 部分唯一索引（坑位 #53：软删行不占命名空间） ==========
-- 存量库：旧约束存在时直接 DROP（约束保证无重复，安全）；无约束的旧库先收敛重复（活跃行优先）
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'user_file_user_id_filename_key') THEN
        ALTER TABLE user_file DROP CONSTRAINT user_file_user_id_filename_key;
    ELSE
        -- 收敛重复：优先保留 status=1（活跃）行；同 status 保留 id 最小
        DELETE FROM parse_tasks WHERE file_id IN (
            SELECT u.id FROM user_file u
            WHERE EXISTS (SELECT 1 FROM user_file u2
                          WHERE u2.user_id = u.user_id AND u2.filename = u.filename
                            AND (u2.status > u.status OR (u2.status = u.status AND u2.id < u.id)))
        );
        DELETE FROM rag_chunk WHERE file_id IN (
            SELECT u.id FROM user_file u
            WHERE EXISTS (SELECT 1 FROM user_file u2
                          WHERE u2.user_id = u.user_id AND u2.filename = u.filename
                            AND (u2.status > u.status OR (u2.status = u.status AND u2.id < u.id)))
        );
        DELETE FROM user_file u
        WHERE EXISTS (
            SELECT 1 FROM user_file u2
            WHERE u2.user_id = u.user_id AND u2.filename = u.filename
              AND (u2.status > u.status OR (u2.status = u.status AND u2.id < u.id))
        );
    END IF;
END $$;
-- 部分唯一索引：仅 status=1（活跃）行唯一，软删行可同名重传（幂等：已存在则跳过）
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_file_active_name
    ON user_file(user_id, filename) WHERE status = 1;

-- ========== 6. 热点索引补齐 ==========
CREATE INDEX IF NOT EXISTS idx_qa_log_session ON qa_log(session_id);
CREATE INDEX IF NOT EXISTS idx_retrieval_trace ON retrieval_log(trace_id);
CREATE INDEX IF NOT EXISTS idx_eval_result_run ON eval_result(run_id);
CREATE INDEX IF NOT EXISTS idx_audit_created ON kb_audit_log(created_at);

-- ========== 7. 失败块闭环 v2：ingest_job/issue_items 迁移到 file_id 维度 ==========
-- 7.1 ingest_job：document_id（旧表 kb_document）→ file_id（新表 user_file）
--     存量任务全部是 queued 死任务（旧上传路径已删，无消费端），清空重来
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='ingest_job' AND column_name='document_id') THEN
        ALTER TABLE ingest_job DROP CONSTRAINT IF EXISTS ingest_job_document_id_fkey;
        DELETE FROM ingest_job;
        ALTER TABLE ingest_job RENAME COLUMN document_id TO file_id;
        ALTER TABLE ingest_job ADD CONSTRAINT ingest_job_file_id_fkey
            FOREIGN KEY (file_id) REFERENCES user_file(id);
    END IF;
END $$;

-- 7.2 issue_items：重建为 v2（挂 file_id；旧表空数据直接丢弃）
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='issue_items' AND column_name='document_id') THEN
        DROP TABLE issue_items;
        CREATE TABLE issue_items (
            id BIGSERIAL PRIMARY KEY,
            file_id BIGINT NOT NULL REFERENCES user_file(id) ON DELETE CASCADE,
            page_no INT NOT NULL,
            block_order INT DEFAULT 0,
            block_type VARCHAR(10) NOT NULL,
            reason TEXT NOT NULL,
            bbox JSONB,
            status VARCHAR(20) DEFAULT 'pending',
            resolution TEXT DEFAULT '',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (file_id, page_no, block_order, block_type)
        );
        CREATE INDEX IF NOT EXISTS idx_issue_file ON issue_items(file_id, status);
    END IF;
END $$;

-- ========== 8. 旧链路表下线（数据为历史测试数据，评估/反馈/MCP 已全部切新链路） ==========
-- 对 kb_document 的外部引用已在 7.1 解除（ingest_job 改指 user_file）；其余引用表随表自身删除
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_name='kb_user_document') THEN
        DROP TABLE IF EXISTS kb_user_document;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_name='kb_chunk') THEN
        DROP TABLE IF EXISTS kb_chunk;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_name='kb_document') THEN
        DROP TABLE IF EXISTS kb_document;
    END IF;
END $$;

-- ========== 9. 遗留列清理 ==========
ALTER TABLE kb_user DROP COLUMN IF EXISTS salt;              -- BCrypt 自带盐
ALTER TABLE memory_entry DROP COLUMN IF EXISTS user_id_old;  -- 迁移残留
