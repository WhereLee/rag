#!/bin/bash
# 部署辅助：启动 4 个服务并探活
echo "== start services =="
sudo systemctl start rag-python rag-qa rag-worker rag-gateway
sleep 8
echo "== status =="
for s in rag-python rag-qa rag-worker rag-gateway; do
  printf "%-14s %s\n" "$s" "$(systemctl is-active $s)"
done
echo "== health checks =="
echo "-- 8090:"; curl -s -m 5 http://127.0.0.1:8090/health; echo
echo "-- 8091:"; curl -s -m 5 http://127.0.0.1:8091/health; echo
echo "-- 8082:"; curl -s -m 5 http://127.0.0.1:8082/health; echo
echo "== recent errors =="
journalctl -u rag-python -u rag-qa -u rag-worker -u rag-gateway --no-pager -n 30 2>/dev/null | grep -iE 'error|exception|traceback|failed' | tail -10 || true
echo "== DONE =="
