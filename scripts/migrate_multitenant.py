"""
多租户隔离迁移脚本：
1. 创建 kb_user_document 映射表
2. 移除 kb_document.file_hash UNIQUE 约束
3. 加 user_id 列到 6 张表
4. 迁移 memory_entry.user_id 类型
5. 创建 admin 用户并映射现有文档

用法：python scripts/migrate_multitenant.py
"""
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rag-python" / "src"))
import config  # noqa: E402


def main():
    with psycopg.connect(config.PG_DSN) as conn:
        cur = conn.cursor()

        # 1. 创建 kb_user_document 映射表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS kb_user_document (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES kb_user(id),
                document_id BIGINT NOT NULL REFERENCES kb_document(id),
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(user_id, document_id)
            )
        """)
        print("[migrate] kb_user_document created")

        # 2. 移除 file_hash UNIQUE 约束（如果存在）
        cur.execute("""
            DO $$ BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'kb_document_file_hash_key'
                ) THEN
                    ALTER TABLE kb_document DROP CONSTRAINT kb_document_file_hash_key;
                    RAISE NOTICE 'dropped kb_document_file_hash_key';
                END IF;
            END $$
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_document_hash ON kb_document(file_hash)
        """)
        print("[migrate] file_hash UNIQUE -> INDEX")

        # 3. 加 user_id 列到需要的表
        for table in ("qa_log", "qa_session", "retrieval_log", "feedback", "bad_case"):
            cur.execute(f"""
                DO $$ BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='{table}' AND column_name='user_id'
                    ) THEN
                        ALTER TABLE {table} ADD COLUMN user_id BIGINT REFERENCES kb_user(id);
                        RAISE NOTICE 'added user_id to {table}';
                    END IF;
                END $$
            """)
        print("[migrate] user_id columns added to 5 tables")

        # 4. memory_entry: VARCHAR user_id -> BIGINT（迁移旧数据）
        # 先把旧的 'default' 映射到 admin 用户
        cur.execute("""
            SELECT count(*) FROM information_schema.columns
            WHERE table_name='memory_entry' AND column_name='user_id'
            AND data_type='character varying'
        """)
        if cur.fetchone()[0] > 0:
            # 旧 VARCHAR 列存在，需要迁移
            # 先备份旧值（不删除），加新 BIGINT 列
            cur.execute("ALTER TABLE memory_entry RENAME COLUMN user_id TO user_id_old")
            cur.execute("ALTER TABLE memory_entry ADD COLUMN user_id BIGINT REFERENCES kb_user(id)")
            print("[migrate] memory_entry user_id migrated VARCHAR -> BIGINT")

        # 5. 确保 admin 用户存在
        cur.execute("""
            INSERT INTO kb_user (username, password_hash, salt, role)
            VALUES ('admin', '$2a$10$placeholder_bcrt', '', 'admin')
            ON CONFLICT (username) DO NOTHING
            RETURNING id
        """)
        row = cur.fetchone()
        if row:
            admin_id = row[0]
            print(f"[migrate] admin user created: id={admin_id}")
        else:
            cur.execute("SELECT id FROM kb_user WHERE username='admin'")
            admin_id = cur.fetchone()[0]
            print(f"[migrate] admin user already exists: id={admin_id}")

        # 6. 将所有现有文档映射给 admin
        cur.execute("""
            INSERT INTO kb_user_document (user_id, document_id)
            SELECT %s, d.id FROM kb_document d
            WHERE d.status = 1
            AND NOT EXISTS (
                SELECT 1 FROM kb_user_document ud
                WHERE ud.user_id = %s AND ud.document_id = d.id
            )
        """, (admin_id, admin_id))
        mapped = cur.rowcount
        print(f"[migrate] {mapped} documents mapped to admin")

        # 7. 将旧 memory_entry 归入 admin
        cur.execute("""
            UPDATE memory_entry SET user_id = %s WHERE user_id IS NULL
        """, (admin_id,))

        conn.commit()
        print("[migrate] migration complete")


if __name__ == "__main__":
    main()
