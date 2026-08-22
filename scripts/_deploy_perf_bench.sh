#!/bin/bash
# 检索层压测（配置已生效，纯压测；cascade off/on × 并发 4/8，每轮 30s）
set -e
cd /opt/rag/rag-python/src
PY=/opt/rag/rag-python/.venv/bin/python

for cfg in off on; do
  for c in 4 8; do
    echo "==== cascade=$cfg concurrency=$c ===="
    $PY -X utf8 /opt/rag/scripts/bench_retrieval.py \
      --concurrency $c --duration 30 --cascade $cfg 2>&1 \
      | grep -E "bench start|requests=|latency ms|downgraded" || true
  done
done
echo "== DONE =="
