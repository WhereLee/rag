#!/bin/bash
# 部署辅助：创建 venv 并安装 Python 依赖（清华镜像加速）
# 用法：bash scripts/_deploy_pydeps.sh
set -e
cd /opt/rag/rag-python
echo "== create venv =="
python3.12 -m venv .venv
echo "== pip install (tsinghua mirror) =="
.venv/bin/pip install -q --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
.venv/bin/pip install -q -e . -i https://pypi.tuna.tsinghua.edu.cn/simple
echo "== verify =="
.venv/bin/python -c "import config; print('PG_DSN loaded:', bool(config.PG_DSN)); print('EMBED_DIM:', config.EMBED_DIM)"
echo "== DONE =="
