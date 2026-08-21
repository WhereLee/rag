#!/bin/bash
# 部署辅助：拉取 q_hash 修复 + 重启 rag-qa
set -e
cd /opt/rag
git pull origin master 2>&1 | tail -2
sudo systemctl restart rag-qa
sleep 6
systemctl is-active rag-qa
echo "== DONE =="
