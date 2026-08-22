#!/bin/bash
cd /opt/rag
PW=$(sed -n 's|.*rag_app:\([^@]*\)@.*|\1|p' /opt/rag/.env)
export PGPASSWORD="$PW"
echo "== counts =="
psql -h 127.0.0.1 -U rag_app -d rag_kb -t -A -c "SELECT 'qa_log='||count(*) FROM qa_log; SELECT 'qa_cache='||count(*) FROM qa_cache"
echo "== qa_log 最近 3 条 =="
psql -h 127.0.0.1 -U rag_app -d rag_kb -t -A -c "SELECT id, query FROM qa_log ORDER BY id DESC LIMIT 3"
echo "== qa_cache 最近 3 条 =="
psql -h 127.0.0.1 -U rag_app -d rag_kb -t -A -c "SELECT id, query FROM qa_cache ORDER BY id DESC LIMIT 3"
echo "== DONE =="
