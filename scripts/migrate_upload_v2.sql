-- ============ 阶段2 迁移：秒传（file_blob）+ 回收站（deleted_at） ============
-- 真正幂等：可重复执行；存量归并仅在 stored_name 列仍存在时执行一次。

-- 1. 物理文件表：内容级去重单元（sha256 唯一）。
--    owner_user_id = 第一个上传者（物理文件所在目录），秒传共享后删除/清理都按 owner 找路径。
CREATE TABLE IF NOT EXISTS file_blob (
    id BIGSERIAL PRIMARY KEY,
    file_hash VARCHAR(64) NOT NULL UNIQUE,
    stored_name VARCHAR(300) NOT NULL,
    file_size BIGINT NOT NULL,
    ref_count INT NOT NULL DEFAULT 0,
    owner_user_id BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- 兼容旧结构（第一次建表后补充的列）
ALTER TABLE file_blob ADD COLUMN IF NOT EXISTS owner_user_id BIGINT NOT NULL DEFAULT 0;

-- 2. user_file 增加 blob 引用与删除时间
ALTER TABLE user_file ADD COLUMN IF NOT EXISTS blob_id BIGINT;
ALTER TABLE user_file ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

-- 3. 存量归并（仅当 stored_name 列存在时执行一次；之后该列被删，条件为假自动跳过）
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = 'user_file' AND column_name = 'stored_name') THEN
        -- 按 stored_name 归并出 blob：ref_count = 引用数，owner = 首个引用用户。
        -- 占位 hash（legacy-前缀），旧文件不参与秒传去重；如需全量参与可后续写重算脚本。
        INSERT INTO file_blob (file_hash, stored_name, file_size, ref_count, owner_user_id)
        SELECT 'legacy-' || stored_name, stored_name, file_size, count(*), min(user_id)
        FROM user_file
        WHERE stored_name IS NOT NULL AND stored_name <> ''
        GROUP BY stored_name, file_size
        ON CONFLICT (file_hash) DO NOTHING;

        UPDATE user_file u SET blob_id = b.id
        FROM file_blob b
        WHERE u.stored_name = b.stored_name AND u.blob_id IS NULL;

        ALTER TABLE user_file DROP COLUMN stored_name;
    END IF;
END $$;

-- 4. 回填 owner_user_id（幂等：只补缺失的）
UPDATE file_blob b SET owner_user_id =
    (SELECT u.user_id FROM user_file u WHERE u.blob_id = b.id ORDER BY u.id LIMIT 1)
WHERE b.owner_user_id = 0;

-- 5. 回收站列表/清理索引
CREATE INDEX IF NOT EXISTS idx_user_file_deleted ON user_file (user_id, status, deleted_at);
