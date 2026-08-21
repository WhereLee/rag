#!/bin/bash
# 部署辅助：实测 base reranker 对查询-块的打分（验证 -5 阈值是否合理）
cd /opt/rag/rag-python/src
/opt/rag/rag-python/.venv/bin/python -X utf8 -c "
import sys
sys.path.insert(0, '/opt/rag/rag-python/src')
from retrieval.retriever import retrieve
chunks = retrieve(1, '量子计算目前主要应用在哪些领域？', top_k=5)
print('retrieved chunks:', len(chunks))
for c in chunks:
    print('id=%s score=%.3f reranked=%s file=%s content=%s' % (c.chunk_id, c.score, c.reranked, c.filename, c.content[:60]))
import config
print('RERANK_REJECT =', config.RERANK_REJECT)
" 2>&1 | tail -10
echo "== DONE =="
