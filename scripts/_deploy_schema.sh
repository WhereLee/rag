#!/bin/bash
# 部署辅助：导入表结构（init_db.py 替换 __EMBED_DIM__ + init_chunk.sql 向量表）
set -e
cd /opt/rag
echo "== init_db.py (embed_dim auto) =="
rag-python/.venv/bin/python scripts/init_db.py
echo "== init_chunk.sql =="
PW=$(sed -n 's|.*rag_app:\([^@]*\)@.*|\1|p' /opt/rag/.env)
PGPASSWORD="$PW" psql -h 127.0.0.1 -U rag_app -d rag_kb -v ON_ERROR_STOP=1 -f scripts/init_chunk.sql
echo "== tables =="
PGPASSWORD="$PW" psql -h 127.0.0.1 -U rag_app -d rag_kb -c "\dt"
echo "== DONE =="
