#!/bin/bash
# 恢复演练：从 COS 下载最新备份 → 建临时库还原 → 关键表行数对比 → 清理
set -euo pipefail
cd /opt/rag
COSCMD=/opt/rag/coscmd-venv/bin/coscmd
PW=$(sed -n 's|.*rag_app:\([^@]*\)@.*|\1|p' /opt/rag/.env)
export PGPASSWORD="$PW"
PG="sudo -u postgres psql"
# 行数对比用 rag_app（只读 SELECT，同权限验证）
PGAPP="psql -h 127.0.0.1 -U rag_app"

echo "== 1/4 下载最新备份 =="
LATEST=$("$COSCMD" list rag-backup/ 2>/dev/null | grep '\.dump\.gz' | awk '{print $1}' | sort | tail -1)
echo "最新备份: $LATEST"
"$COSCMD" download -f "$LATEST" "/tmp/$LATEST" 2>&1 | tail -1
ls -lh "/tmp/$LATEST"

echo "== 2/4 建临时库并还原 =="
$PG -q -c "DROP DATABASE IF EXISTS rag_restore_test"
$PG -q -c "CREATE DATABASE rag_restore_test OWNER rag_app"
# 先装扩展（dump 不含扩展二进制，vector/pg_trgm 是二进制依赖，真实恢复同样需要这步）
$PG -d rag_restore_test -q -c "CREATE EXTENSION IF NOT EXISTS vector"
$PG -d rag_restore_test -q -c "CREATE EXTENSION IF NOT EXISTS pg_trgm"
gunzip -f "/tmp/$LATEST"
RESTORE_DUMP="/tmp/${LATEST%.gz}"
sudo -u postgres pg_restore -d rag_restore_test --no-owner --no-privileges --role=rag_app "$RESTORE_DUMP" > /tmp/restore_log.txt 2>&1 || true
grep -c "ERROR" /tmp/restore_log.txt || echo "0 errors"

echo "== 3/4 关键表行数对比（生产 vs 恢复） =="
for t in user_file parse_tasks rag_chunk issue_items qa_log qa_cache; do
  prod=$($PGAPP -d rag_kb -t -A -c "SELECT count(*) FROM $t" 2>/dev/null || echo "N/A")
  rest=$($PGAPP -d rag_restore_test -t -A -c "SELECT count(*) FROM $t" 2>/dev/null || echo "N/A")
  if [ "$prod" = "$rest" ]; then
    echo "  $t: $prod == $rest ✓"
  else
    echo "  $t: $prod != $rest ✗ !!!"
  fi
done

echo "== 4/4 清理临时库与文件 =="
$PG -q -c "DROP DATABASE rag_restore_test"
rm -f "/tmp/$LATEST" "$RESTORE_DUMP"
echo "== DONE =="
