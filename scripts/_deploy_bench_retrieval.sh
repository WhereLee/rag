#!/bin/bash
# 检索层压测：cascade off/on × 并发 4/8，每档 30s，完整输出落文件
cd /opt/rag/rag-python/src
PY=/opt/rag/rag-python/.venv/bin/python
OUT=/tmp/bench_retrieval.txt
> "$OUT"
for cfg in off on; do
  for c in 4 8; do
    echo "==== cascade=$cfg concurrency=$c ====" >> "$OUT"
    $PY -X utf8 /opt/rag/scripts/bench_retrieval.py \
      --concurrency $c --duration 30 --cascade $cfg >> "$OUT" 2>&1
  done
done
echo "== DONE ==" >> "$OUT"
