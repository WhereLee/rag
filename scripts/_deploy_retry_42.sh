#!/bin/bash
# 重置 42.pdf 解析任务
cd /opt/rag
PW=$(sed -n 's|.*rag_app:\([^@]*\)@.*|\1|p' /opt/rag/.env)
export PGPASSWORD="$PW"
psql -h 127.0.0.1 -U rag_app -d rag_kb -q -c "UPDATE parse_tasks SET status='pending', stage=NULL, progress=0 WHERE file_id=19"
echo "RESET_OK file_id=19"
