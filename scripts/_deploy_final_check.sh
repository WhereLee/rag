#!/bin/bash
# 部署辅助：最终确认（qa_cache 写入 + 内存基线 + 全服务状态）
PW=$(sed -n 's|.*rag_app:\([^@]*\)@.*|\1|p' /opt/rag/.env)
export PGPASSWORD="$PW"
echo "== qa_cache（修复后应能写入）=="
psql -h 127.0.0.1 -U rag_app -d rag_kb -c "SELECT id, user_id, query, answer, invalidated FROM qa_cache ORDER BY id DESC LIMIT 3;"
echo "== qa_log =="
psql -h 127.0.0.1 -U rag_app -d rag_kb -c "SELECT id, user_id, left(query,20) AS query, total_ms, left(answer,40) AS answer FROM qa_log ORDER BY id DESC LIMIT 3;"
echo "== memory =="
free -m | head -2
echo "== services =="
for s in rag-python rag-qa rag-worker rag-gateway nginx postgresql redis-server; do
  printf "%-14s %s\n" "$s" "$(systemctl is-active $s)"
done
echo "== DONE =="
