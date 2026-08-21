#!/bin/bash
# 部署辅助：执行秒传迁移（migrate_upload_v2.sql）+ 重启 worker 验证
set -e
cd /opt/rag
PW=$(sed -n 's|.*rag_app:\([^@]*\)@.*|\1|p' /opt/rag/.env)
export PGPASSWORD="$PW"
echo "== migrate_upload_v2.sql =="
psql -h 127.0.0.1 -U rag_app -d rag_kb -v ON_ERROR_STOP=1 -f scripts/migrate_upload_v2.sql
echo "== user_file key columns =="
psql -h 127.0.0.1 -U rag_app -d rag_kb -c "\d user_file" | grep -E 'blob_id|dir_id|deleted_at|stored_name|user_id'
echo "== restart worker =="
sudo systemctl restart rag-worker
sleep 5
echo "== worker status =="
systemctl is-active rag-worker
echo "== worker recent log =="
journalctl -u rag-worker --no-pager -n 8 | tail -6
echo "== DONE =="
