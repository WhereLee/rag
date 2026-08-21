#!/bin/bash
# 部署辅助：问答 500 排查（qa 服务 + 网关日志）
echo "== rag-qa status =="
systemctl is-active rag-qa
echo "== rag-qa recent log =="
journalctl -u rag-qa --no-pager -n 25 | tail -20
echo "== rag-gateway recent error =="
journalctl -u rag-gateway --no-pager -n 40 | grep -iE 'error|exception|fail' | tail -8 || true
echo "== DONE =="
