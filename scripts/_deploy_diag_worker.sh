#!/bin/bash
# 部署辅助：worker 解析失败详情
journalctl -u rag-worker --no-pager -n 60 | grep -vE 'INFO' | tail -40
echo "== DONE =="
