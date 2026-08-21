-- RAG 检索入库表：rag_chunk（C2 计划）
-- 幂等可重复执行（CREATE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS）

CREATE TABLE IF NOT EXISTS rag_chunk (
  id BIGSERIAL PRIMARY KEY,
  file_id BIGINT NOT NULL REFERENCES user_file(id),
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

ALTER TABLE parse_tasks ADD COLUMN IF NOT EXISTS chunk_count INT;
ALTER TABLE parse_tasks ADD COLUMN IF NOT EXISTS stage TEXT;
ALTER TABLE parse_tasks ADD COLUMN IF NOT EXISTS progress REAL;

-- ========== 目录体系 + 问答存档（目录-对话-存档计划 P1/P3） ==========
-- 单层目录：目录名同用户唯一；非空目录禁删（应用层约束）

CREATE TABLE IF NOT EXISTS user_dir (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES kb_user(id),
  name VARCHAR(100) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, name)
);

ALTER TABLE user_file ADD COLUMN IF NOT EXISTS dir_id BIGINT REFERENCES user_dir(id);
ALTER TABLE qa_session ADD COLUMN IF NOT EXISTS dir_id BIGINT REFERENCES user_dir(id);
CREATE INDEX IF NOT EXISTS idx_user_file_dir ON user_file(dir_id) WHERE dir_id IS NOT NULL;

-- 问答存档：精确命中（query_hash）+ 语义命中（query_embedding）两级复用
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
  cache_shared_from BIGINT,   -- 跨用户复用时记录来源缓存 id（诊断/审计；空=本人问答产生）
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, query_hash)
);
