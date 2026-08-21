#!/bin/bash
# 部署辅助：修复问答 500（模型权限 + upload_session 表 + 运行目录）
set -e
cd /opt/rag
PW=$(sed -n 's|.*rag_app:\([^@]*\)@.*|\1|p' /opt/rag/.env)
export PGPASSWORD="$PW"

echo "== 1/3 migrate_upload_v3.sql =="
psql -h 127.0.0.1 -U rag_app -d rag_kb -v ON_ERROR_STOP=1 -f scripts/migrate_upload_v3.sql

echo "== 2/3 models permission =="
sudo chmod -R a+rX /opt/rag/rag-python/models

echo "== 3/3 runtime dirs =="
sudo mkdir -p /opt/rag/data/files /opt/rag/data/jobs /opt/rag/data/parsed /opt/rag/data/browser_upload
sudo chown -R rag:ubuntu /opt/rag/data
sudo chmod -R g+rwX /opt/rag/data

echo "== restart services =="
sudo systemctl restart rag-qa rag-gateway
sleep 6
for s in rag-qa rag-gateway; do
  printf "%-14s %s\n" "$s" "$(systemctl is-active $s)"
done
echo "== verify upload_session =="
psql -h 127.0.0.1 -U rag_app -d rag_kb -c "\dt upload_session" | head -4
echo "== DONE =="
