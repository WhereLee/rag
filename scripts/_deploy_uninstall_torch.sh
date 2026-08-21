#!/bin/bash
# 部署辅助：卸载 torch 全家（torch/triton/nvidia CUDA 库），验证 tokenizer 不受影响
set -e
cd /opt/rag/rag-python
echo "== packages to remove =="
.venv/bin/pip list 2>/dev/null | grep -iE '^(torch|triton|nvidia)' | awk '{print $1}' | tee /tmp/torch_pkgs.txt
echo "== uninstall =="
.venv/bin/pip uninstall -y -r /tmp/torch_pkgs.txt 2>&1 | tail -1
echo "== verify AutoTokenizer =="
.venv/bin/python -c "from transformers import AutoTokenizer; print('AutoTokenizer OK')"
echo "== venv size after =="
du -sh .venv
echo "== DONE =="
