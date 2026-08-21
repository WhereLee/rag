#!/bin/bash
# 部署辅助：拉取修复（__init__.py 入库）+ 重启服务
set -e
cd /opt/rag
echo "== git pull =="
git pull origin master 2>&1 | tail -3
echo "== restart =="
sudo systemctl restart rag-python rag-qa rag-worker rag-gateway
sleep 8
for s in rag-python rag-qa rag-worker rag-gateway; do
  printf "%-14s %s\n" "$s" "$(systemctl is-active $s)"
done
echo "== verify __init__ =="
ls -la /opt/rag/rag-python/src/ingest/parser/__init__.py
echo "== DONE =="
