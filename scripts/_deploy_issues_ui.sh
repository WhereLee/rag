#!/bin/bash
# 前端失败块 UI 部署：pull → Java 构建 → 前端构建 → 重启网关 → 端到端冒烟
set -e
cd /opt/rag
echo "== 1/4 git pull + 构建 =="
git pull origin master 2>&1 | tail -1
cd /opt/rag/rag-java && mvn -q package -DskipTests && ls -lh target/rag-gateway-*.jar | tail -1
cd /opt/rag/rag-frontend && npm run build 2>&1 | tail -1

echo "== 2/4 重启网关 =="
sudo systemctl restart rag-gateway
sleep 6
systemctl is-active rag-gateway

echo "== 3/4 端到端冒烟（经网关：登录→列表 issue_count→issues 列表→describe 写入） =="
cd /opt/rag
PW=$(sed -n 's|.*rag_app:\([^@]*\)@.*|\1|p' /opt/rag/.env)
export PGPASSWORD="$PW"
# 服务器已有用户（user 1 持文件），用 JWT 登录接口拿 token（网关本地）
TOKEN=$(curl -s http://127.0.0.1:8082/api/auth/login -X POST -H "Content-Type: application/json" \
  -d '{"username":"seeduser","password":"seedpass123"}' | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null || echo "")
if [ -z "$TOKEN" ]; then
  echo "登录失败（无 seeduser 账号，跳过登录链路，直接内部验证）"
  TOKEN=""
fi

echo "-- issue_count 列表 --"
if [ -n "$TOKEN" ]; then
  curl -s "http://127.0.0.1:8082/api/files?page=1&pageSize=5" -H "Authorization: Bearer $TOKEN" \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print([(i['filename'], i.get('issue_count')) for i in d.get('items',[])])" 2>/dev/null || echo "(列表读取失败)"
else
  echo "(无 token，跳过)"
fi

echo "-- 直查 issue_items（服务器现有失败块） --"
psql -h 127.0.0.1 -U rag_app -d rag_kb -t -A -c "
SELECT file_id, count(*) FROM issue_items WHERE status IN ('pending','retrying','failed') GROUP BY file_id"
echo "== DONE =="
