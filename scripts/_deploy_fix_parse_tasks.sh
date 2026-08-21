#!/bin/bash
# 部署辅助：补建 parse_tasks 表（仓库缺失建表语句）并重跑 init_chunk.sql
set -e
cd /opt/rag
PW=$(sed -n 's|.*rag_app:\([^@]*\)@.*|\1|p' /opt/rag/.env)
export PGPASSWORD="$PW"
echo "== create parse_tasks =="
psql -h 127.0.0.1 -U rag_app -d rag_kb -v ON_ERROR_STOP=1 <<'SQL'
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
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
SQL
echo "== rerun init_chunk.sql =="
psql -h 127.0.0.1 -U rag_app -d rag_kb -v ON_ERROR_STOP=1 -f scripts/init_chunk.sql
echo "== parse_tasks columns =="
psql -h 127.0.0.1 -U rag_app -d rag_kb -c "\d parse_tasks" | head -20
echo "== all tables =="
psql -h 127.0.0.1 -U rag_app -d rag_kb -c "\dt"
echo "== DONE =="
