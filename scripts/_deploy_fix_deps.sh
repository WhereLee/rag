#!/bin/bash
# 部署辅助：补装解析器依赖 + 重启服务
set -e
cd /opt/rag
echo "== git pull =="
git pull origin master 2>&1 | tail -2
echo "== pip install =="
/opt/rag/rag-python/.venv/bin/pip install -q openpyxl python-pptx -i https://pypi.tuna.tsinghua.edu.cn/simple 2>&1 | tail -2
echo "== verify import =="
cd /opt/rag/rag-python/src
/opt/rag/rag-python/.venv/bin/python -c "import ingest.parser as p; print('parser attrs:', [a for a in dir(p) if not a.startswith('_')])"
echo "== restart =="
sudo systemctl restart rag-worker rag-qa rag-python rag-gateway
sleep 8
for s in rag-worker rag-qa rag-python rag-gateway; do
  printf "%-14s %s\n" "$s" "$(systemctl is-active $s)"
done
echo "== DONE =="
