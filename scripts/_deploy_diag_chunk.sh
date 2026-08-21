#!/bin/bash
# 部署辅助：检查 rag_chunk 入库情况
PW=$(sed -n 's|.*rag_app:\([^@]*\)@.*|\1|p' /opt/rag/.env)
export PGPASSWORD="$PW"
echo "== parse_tasks =="
psql -h 127.0.0.1 -U rag_app -d rag_kb -c "SELECT file_id, status, stage, progress, chunk_count, node_count, error FROM parse_tasks;"
echo "== rag_chunk by file =="
psql -h 127.0.0.1 -U rag_app -d rag_kb -c "SELECT file_id, chunk_type, count(*), min(embed_model) AS embed_model FROM rag_chunk GROUP BY file_id, chunk_type ORDER BY file_id;"
echo "== user_file =="
psql -h 127.0.0.1 -U rag_app -d rag_kb -c "SELECT id, filename, user_id, blob_id, status FROM user_file;"
echo "== DONE =="
