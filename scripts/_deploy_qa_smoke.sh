#!/bin/bash
# 全链路问答冒烟：检索 → LLM 生成 → SSE 流式 → qa_log/qa_cache 落库
set -e
cd /opt/rag
KEY=$(sed -n 's|^INTERNAL_API_KEY=\(.*\)|\1|p' /opt/rag/.env)
echo "== 1/3 提问（SSE 流式，含思考+正文） =="
curl -sN http://127.0.0.1:8091/qa/ask \
  -H "Content-Type: application/json" \
  -H "X-User-Id: 1" \
  -H "X-Internal-Key: $KEY" \
  -d '{"query":"量子计算在金融风控领域目前落地较快的是哪三大领域？","session_id":""}' \
  --max-time 120 > /tmp/qa_smoke.txt || true
echo "-- 事件类型统计 --"
grep -o '"type": "[a-z_]*"' /tmp/qa_smoke.txt | sort | uniq -c
echo "-- 正文片段（delta） --"
grep '"delta"' /tmp/qa_smoke.txt | head -3 | cut -c1-150

echo "== 2/3 引用与元信息 =="
grep -o '"citations": \[[^]]*\]' /tmp/qa_smoke.txt | head -1 | cut -c1-200
grep -o '"rejected": [a-z]*' /tmp/qa_smoke.txt | head -1
grep -o '"cache_ref": [a-z]*' /tmp/qa_smoke.txt | head -1

echo "== 3/3 落库验证 =="
PW=$(sed -n 's|.*rag_app:\([^@]*\)@.*|\1|p' /opt/rag/.env)
export PGPASSWORD="$PW"
psql -h 127.0.0.1 -U rag_app -d rag_kb -t -A -c "
SELECT id, route, cache_hit, LEFT(answer, 60) FROM qa_log
WHERE user_id=1 ORDER BY id DESC LIMIT 1"
psql -h 127.0.0.1 -U rag_app -d rag_kb -t -A -c "
SELECT count(*) FROM qa_cache WHERE user_id=1"
echo "== DONE =="
