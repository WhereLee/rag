#!/bin/bash
# 部署辅助：检查 ingest.parser 模块内容
cd /opt/rag/rag-python/src
/opt/rag/rag-python/.venv/bin/python -c "
import sys
sys.path.insert(0, '/opt/rag/rag-python/src')
import ingest.parser as p
print('file:', p.__file__)
print('attrs:', [a for a in dir(p) if not a.startswith('_')])
import ingest
print('ingest file:', ingest.__file__)
print('ingest attrs:', [a for a in dir(ingest) if not a.startswith('_')])
" 2>&1 | tail -10
echo "== __init__ head =="
head -8 /opt/rag/rag-python/src/ingest/parser/__init__.py
echo "== DONE =="
