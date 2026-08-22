#!/bin/bash
# 部署前状态检查（只读）
cd /opt/rag
echo "== git =="
git log -1 --oneline
echo "== services =="
for s in rag-python rag-qa rag-worker rag-gateway nginx; do
  printf "%-12s %s\n" "$s" "$(systemctl is-active $s)"
done
echo "== db data =="
PW=$(sed -n 's|.*rag_app:\([^@]*\)@.*|\1|p' /opt/rag/.env)
export PGPASSWORD="$PW"
psql -h 127.0.0.1 -U rag_app -d rag_kb -t -A -c "
SELECT 'kb_document='||count(*) FROM kb_document
UNION ALL SELECT 'kb_chunk='||count(*) FROM kb_chunk
UNION ALL SELECT 'kb_user_document='||count(*) FROM kb_user_document
UNION ALL SELECT 'user_file='||count(*) FROM user_file
UNION ALL SELECT 'rag_chunk='||count(*) FROM rag_chunk
UNION ALL SELECT 'ingest_job='||count(*) FROM ingest_job
UNION ALL SELECT 'issue_items='||count(*) FROM issue_items
UNION ALL SELECT 'parse_tasks='||count(*) FROM parse_tasks
UNION ALL SELECT 'qa_cache='||count(*) FROM qa_cache"
echo "== disk =="
df -h / | tail -1
echo "== DONE =="
