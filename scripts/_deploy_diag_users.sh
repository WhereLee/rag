#!/bin/bash
# 部署辅助：检查用户与文件归属
PW=$(sed -n 's|.*rag_app:\([^@]*\)@.*|\1|p' /opt/rag/.env)
export PGPASSWORD="$PW"
echo "== kb_user =="
psql -h 127.0.0.1 -U rag_app -d rag_kb -c "SELECT id, username, role FROM kb_user ORDER BY id;"
echo "== user_file =="
psql -h 127.0.0.1 -U rag_app -d rag_kb -c "SELECT id, filename, user_id, status FROM user_file;"
echo "== DONE =="
