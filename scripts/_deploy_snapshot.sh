#!/bin/bash
# 部署辅助：服务状态快照 + 内存基线 + 端口监听
for s in rag-python rag-qa rag-worker rag-gateway nginx postgresql redis-server; do
  printf "%-14s %s\n" "$s" "$(systemctl is-active $s)"
done
echo "== memory =="
free -m
echo "== listening =="
sudo ss -tlnp | grep -E ':(80|8082|8090|8091|5432|6379) ' | awk '{print $4, $6}'
echo "== DONE =="
