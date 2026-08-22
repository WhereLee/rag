#!/bin/bash
# v2 部署续跑：schema 升级 → 启动 → 验证（第 6-8 步）
set -e
cd /opt/rag
PW=$(sed -n 's|.*rag_app:\([^@]*\)@.*|\1|p' /opt/rag/.env)
export PGPASSWORD="$PW"
PY=/opt/rag/rag-python/.venv/bin/python

echo "== 6/8 schema upgrade =="
$PY scripts/init_db.py
PGCLIENTENCODING=UTF8 psql -h 127.0.0.1 -U rag_app -d rag_kb -v ON_ERROR_STOP=1 -f scripts/init_chunk.sql
echo "schema upgrade done"

echo "== 7/8 start services =="
sudo systemctl start rag-gateway rag-python rag-qa rag-worker
sleep 10
for s in rag-gateway rag-python rag-qa rag-worker; do
  printf "%-12s %s\n" "$s" "$(systemctl is-active $s)"
done

echo "== 8/8 verify =="
echo "-- schema checks --"
psql -h 127.0.0.1 -U rag_app -d rag_kb -t -A -c "
SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='kb_document') THEN 'PASS old tables dropped' ELSE 'FAIL' END
UNION ALL SELECT CASE WHEN EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='issue_items' AND column_name='file_id') THEN 'PASS issue_items v2' ELSE 'FAIL' END
UNION ALL SELECT CASE WHEN EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='ingest_job' AND column_name='file_id') THEN 'PASS ingest_job v2' ELSE 'FAIL' END
UNION ALL SELECT CASE WHEN EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='upload_session' AND column_name='dir_id') THEN 'PASS upload_session dir_id' ELSE 'FAIL' END
UNION ALL SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_constraint WHERE conname='user_file_user_id_filename_key') THEN 'PASS user_file unique' ELSE 'FAIL' END
UNION ALL SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_indexes WHERE indexname='idx_qa_cache_embedding') THEN 'PASS qa_cache hnsw' ELSE 'FAIL' END"
echo "-- data preserved --"
psql -h 127.0.0.1 -U rag_app -d rag_kb -t -A -c "
SELECT 'user_file='||count(*) FROM user_file
UNION ALL SELECT 'rag_chunk='||count(*) FROM rag_chunk
UNION ALL SELECT 'parse_tasks='||count(*) FROM parse_tasks
UNION ALL SELECT 'qa_cache='||count(*) FROM qa_cache"
echo "-- health --"
curl -s http://127.0.0.1:8090/health
echo ""
echo "== DONE =="
