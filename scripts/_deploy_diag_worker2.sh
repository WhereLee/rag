#!/bin/bash
# 部署辅助：worker 最新解析失败日志
journalctl -u rag-worker --no-pager -n 40 | tail -30
echo "== DONE =="
