#!/bin/bash
# v2 部署冒烟：检索链路 + 网关 + 日志
set -e
cd /opt/rag/rag-python/src

echo "== retrieve smoke =="
/opt/rag/rag-python/.venv/bin/python -X utf8 - <<'PY'
import sys
sys.path.insert(0, ".")
from retrieval.retriever import retrieve
chunks = retrieve(None, "测试", top_k=3)
print("RETRIEVE_CHUNKS:", len(chunks))
for c in chunks[:2]:
    print("  -", c.chunk_id, c.filename, round(c.score, 2), "reranked" if c.reranked else "rrf")
from db import pg_store
t = pg_store.query_one("SELECT status, stage, progress, chunk_count FROM parse_tasks")
print("PARSE_TASK:", dict(t) if t else None)
pg_store.close()
PY

echo "== gateway health =="
curl -s http://127.0.0.1:8082/health
echo ""

echo "== worker log =="
journalctl -u rag-worker --no-pager -n 3 | tail -3

echo "== qa log =="
journalctl -u rag-qa --no-pager -n 2 | tail -2

echo "== python log =="
journalctl -u rag-python --no-pager -n 2 | tail -2

echo "== DONE =="
