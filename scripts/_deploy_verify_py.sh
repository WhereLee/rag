#!/bin/bash
# 部署辅助：验证 Python 依赖安装结果
cd /opt/rag/rag-python
echo "== largest packages =="
du -sh .venv/lib/python3.12/site-packages/* 2>/dev/null | sort -rh | head -8
echo "== verify config/onnxruntime =="
.venv/bin/python -c "import config; print('PG_DSN loaded:', bool(config.PG_DSN)); print('EMBED_DIM:', config.EMBED_DIM); import onnxruntime; print('onnxruntime:', onnxruntime.__version__)"
echo "== DONE =="
