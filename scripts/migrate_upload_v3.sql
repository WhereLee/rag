-- ============ 阶段3 迁移：分片上传（upload_session / upload_chunk） ============
-- 幂等：可重复执行。

CREATE TABLE IF NOT EXISTS upload_session (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    file_hash VARCHAR(64) NOT NULL,
    filename VARCHAR(300) NOT NULL,
    file_size BIGINT NOT NULL,
    chunk_size BIGINT NOT NULL,
    chunk_count INT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'uploading',   -- uploading / completed
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
    UNIQUE (session_id, chunk_index)   -- 同片重传（断点续传重试）用 ON CONFLICT 覆盖
);
