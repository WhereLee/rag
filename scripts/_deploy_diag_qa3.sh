#!/bin/bash
# 部署辅助：qa 服务 04:17:40-04:18:05 完整日志
journalctl -u rag-qa --since "2026-08-22 04:17:40" --until "2026-08-22 04:18:10" --no-pager
echo "== DONE =="
