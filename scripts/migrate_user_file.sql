-- 用户文件表（逐步推进第 1 步：登录/上传/管理文件）
-- 幂等：重复执行安全
CREATE TABLE IF NOT EXISTS user_file (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES kb_user(id),
    filename VARCHAR(255) NOT NULL,           -- 原始文件名（展示用）
    stored_name VARCHAR(100) NOT NULL,        -- uuid.ext（磁盘存储名）
    file_size BIGINT NOT NULL DEFAULT 0,
    content_type VARCHAR(100) DEFAULT '',
    status SMALLINT DEFAULT 1,                -- 1正常 0已删除
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_user_file_user ON user_file(user_id, status);
