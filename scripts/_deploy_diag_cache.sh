#!/bin/bash
# 核实 qa_cache / qa_log 内容
cd /opt/rag
PW=$(sed -n 's|.*rag_app:\([^@]*\)@.*|\1|p' /opt/rag/.env)
export PGPASSWORD="$PW"
echo "== qa_cache (user 1) =="
psql -h 127.0.0.1 -U rag_app -d rag_kb -c "
SELECT id, query_hash, LEFT(query, 30) AS q, LEFT(answer, 50) AS ans, invalidated, created_at
FROM qa_cache WHERE user_id=1 ORDER BY id DESC LIMIT 5"
echo "== qa_log (user 1) =="
psql -h 127.0.0.1 -U rag_app -d rag_kb -c "
SELECT id, LEFT(query, 30) AS q, LEFT(answer, 50) AS ans, route, cache_hit, created_at
FROM qa_log WHERE user_id=1 ORDER BY id DESC LIMIT 5"
echo "== DONE =="
