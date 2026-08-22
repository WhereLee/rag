#!/bin/bash
# 服务器灌语料（压测前置）：corpus 文件 → file_blob/user_file/parse_tasks → worker 消费
set -e
cd /opt/rag

echo "== 1/3 scp corpus =="
mkdir -p /opt/rag/data/corpus
# 从本地同步（调用方 scp 已处理；这里只确保目录）

echo "== 2/3 插库 =="
/opt/rag/rag-python/.venv/bin/python -X utf8 - <<'PY'
import hashlib
import sys
import uuid
from pathlib import Path

sys.path.insert(0, "/opt/rag/rag-python/src")
from db import pg_store

DATA = Path("/opt/rag/data/corpus")
FILES_DIR = Path("/opt/rag/data/files/1")
FILES_DIR.mkdir(parents=True, exist_ok=True)

files = sorted(p for p in DATA.rglob("*") if p.is_file())
print(f"corpus files: {len(files)}")
for p in files:
    data = p.read_bytes()
    h = hashlib.sha256(data).hexdigest()
    existing = pg_store.query_one("SELECT id, stored_name FROM file_blob WHERE file_hash=%s", (h,))
    if existing:
        blob_id = existing["id"]
    else:
        stored = uuid.uuid4().hex + p.suffix.lower()
        (FILES_DIR / stored).write_bytes(data)
        row = pg_store.query_one(
            "INSERT INTO file_blob (file_hash, stored_name, file_size, ref_count, owner_user_id) "
            "VALUES (%s,%s,%s,1,1) ON CONFLICT (file_hash) DO NOTHING RETURNING id", (h, stored, len(data)))
        if row:
            blob_id = row["id"]
        else:
            blob_id = pg_store.query_one("SELECT id FROM file_blob WHERE file_hash=%s", (h,))["id"]
    # user_file（同名唯一约束：文件名带相对路径前缀防冲突）
    rel = str(p.relative_to(DATA)).replace("\\", "/")
    uf = pg_store.query_one("SELECT id FROM user_file WHERE user_id=1 AND filename=%s", (rel,))
    if uf:
        print(f"skip(exists): {rel}")
        continue
    uf_row = pg_store.query_one(
        "INSERT INTO user_file (user_id, blob_id, filename, file_size, content_type, status) "
        "VALUES (1,%s,%s,%s,%s,1) RETURNING id",
        (blob_id, rel, len(data), p.suffix.lower() or "txt"))
    pg_store.execute(
        "INSERT INTO parse_tasks (file_id, status) VALUES (%s,'pending') ON CONFLICT (file_id) DO NOTHING",
        (uf_row["id"],))
    print(f"queued: {rel} -> file_id={uf_row['id']} blob={blob_id}")
pg_store.close()
PY

echo "== 3/3 等待 worker 消费（轮询 parse_tasks，上限 8 分钟） =="
/opt/rag/rag-python/.venv/bin/python -X utf8 - <<'PY'
import sys
import time

sys.path.insert(0, "/opt/rag/rag-python/src")
from db import pg_store

deadline = time.time() + 480
while time.time() < deadline:
    rows = pg_store.query(
        "SELECT status, count(*) AS n FROM parse_tasks GROUP BY status ORDER BY status")
    done = {r["status"]: r["n"] for r in rows}
    pending = done.get("pending", 0) + done.get("parsing", 0)
    print("status:", done, flush=True)
    if pending == 0:
        break
    time.sleep(10)
total = pg_store.query_one("SELECT count(*) AS n FROM parse_tasks")["n"]
chunks = pg_store.query_one("SELECT count(*) AS n FROM rag_chunk WHERE file_id IN (SELECT file_id FROM parse_tasks)")["n"]
print(f"FINAL: tasks={total} chunks={chunks}")
pg_store.close()
PY
echo "== DONE =="
