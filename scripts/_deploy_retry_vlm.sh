#!/bin/bash
# VLM 修复部署：pull → 重启 worker → 重试 file4(扫描件)/file8(survey) → 轮询终态
set -e
cd /opt/rag
echo "== 1/4 git pull =="
git pull origin master 2>&1 | tail -1
echo "== 2/4 重启 worker =="
sudo systemctl restart rag-worker
sleep 3
systemctl is-active rag-worker
echo "== 3/4 重试失败任务 =="
PW=$(sed -n 's|.*rag_app:\([^@]*\)@.*|\1|p' /opt/rag/.env)
export PGPASSWORD="$PW"
psql -h 127.0.0.1 -U rag_app -d rag_kb -c "
UPDATE parse_tasks SET status='pending', stage=NULL, progress=0
WHERE file_id IN (4,8) AND status='failed'"
echo "== 4/4 轮询终态（上限 15 分钟） =="
/opt/rag/rag-python/.venv/bin/python -X utf8 - <<'PY'
import sys
import time
sys.path.insert(0, "/opt/rag/rag-python/src")
from db import pg_store

deadline = time.time() + 900
while time.time() < deadline:
    rows = pg_store.query(
        "SELECT file_id, status, COALESCE(error,'') AS err, chunk_count "
        "FROM parse_tasks WHERE file_id IN (4,8)")
    pending = [r for r in rows if r["status"] in ("pending", "parsing")]
    print({r["file_id"]: r["status"] for r in rows}, flush=True)
    if not pending:
        break
    time.sleep(15)
for r in rows:
    print(f"FINAL file_id={r['file_id']} status={r['status']} chunks={r['chunk_count']} err={r['err'][:80]}")
pg_store.close()
PY
echo "== DONE =="
