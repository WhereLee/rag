#!/bin/bash
# 重启服务（.env 参数生效）+ 健康检查 + 清理
set -e
sudo systemctl restart rag-python rag-qa rag-worker
sleep 10
for s in rag-python rag-qa rag-worker rag-gateway; do
  printf "%-12s %s\n" "$s" "$(systemctl is-active $s)"
done
curl -s http://127.0.0.1:8090/health
echo ""
rm -f /tmp/_deploy_perf_batch1.sh /tmp/_deploy_perf_bench2.sh \
      /tmp/_deploy_diag_parse.sh /tmp/_deploy_seed_corpus.sh \
      /tmp/_deploy_perf_bench.sh /tmp/_deploy_perf_bench2.sh
echo "CLEANED"
