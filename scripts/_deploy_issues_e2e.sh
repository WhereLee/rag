#!/bin/bash
# 失败块代理链路端到端验证：注册→登录→GET/POST 全路径（归属校验/存在性/注入检测）
set -e
cd /opt/rag
G=http://127.0.0.1:8082
U="uitest$(date +%s)"
echo "== 注册 $U =="
curl -s "$G/api/auth/register" -X POST -H "Content-Type: application/json" \
  -d "{\"username\":\"$U\",\"password\":\"UiTest123\"}" | head -c 200
echo ""
echo "== 登录 =="
TOKEN=$(curl -s "$G/api/auth/login" -X POST -H "Content-Type: application/json" \
  -d "{\"username\":\"$U\",\"password\":\"UiTest123\"}" \
  | /opt/rag/rag-python/.venv/bin/python -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null || echo "")
echo "token_len=${#TOKEN}"
if [ -z "$TOKEN" ]; then echo "LOGIN_FAIL"; exit 1; fi

echo "== GET issues 列表（他人文件 → 预期 404 归属校验） =="
curl -s -o /dev/null -w "GET files/4/issues -> %{http_code}\n" \
  "$G/api/admin/proxy/api/ingest/files/4/issues" -H "Authorization: Bearer $TOKEN"

echo "== GET issues 列表（不存在文件 → 预期 404） =="
curl -s -o /dev/null -w "GET files/99999/issues -> %{http_code}\n" \
  "$G/api/admin/proxy/api/ingest/files/99999/issues" -H "Authorization: Bearer $TOKEN"

echo "== POST retry（不存在 issue → 预期 404） =="
curl -s -o /dev/null -w "POST issues/999999/retry -> %{http_code}\n" \
  -X POST "$G/api/admin/proxy/api/ingest/issues/999999/retry" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{}'

echo "== POST describe（注入检测先行 → 预期 404 issue 不存在，若到注入检测应 400） =="
curl -s -w "\nPOST issues/999999/describe -> %{http_code}\n" \
  -X POST "$G/api/admin/proxy/api/ingest/issues/999999/describe" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"text":"这是测试描述"}' | head -c 300

echo "== POST replace（multipart → 预期 404） =="
echo "x" > /tmp/fake.png
curl -s -o /dev/null -w "POST issues/999999/replace -> %{http_code}\n" \
  -X POST "$G/api/admin/proxy/api/ingest/issues/999999/replace" \
  -H "Authorization: Bearer $TOKEN" -F "file=@/tmp/fake.png"
rm -f /tmp/fake.png

echo "== 清理测试用户 =="
PW=$(sed -n 's|.*rag_app:\([^@]*\)@.*|\1|p' /opt/rag/.env)
export PGPASSWORD="$PW"
psql -h 127.0.0.1 -U rag_app -d rag_kb -q -c "DELETE FROM kb_user WHERE username='$U'"
echo "== DONE =="
