#!/bin/bash
# 部署辅助：qa 服务检索日志
journalctl -u rag-qa --no-pager -n 60 | tail -30
echo "== DONE =="
