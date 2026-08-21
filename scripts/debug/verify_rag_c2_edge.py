# -*- coding: utf-8 -*-
"""C2 边界测试：reparse 旧块清零 / embed 失败无残留 / 并发不串块 / 0 块文档。
前置：网关(8082) + 新版 parse worker 已启动。用法: python scripts/debug/verify_rag_c2_edge.py
"""
import random
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "rag-python" / "src"))

import psycopg
import psycopg.rows
import requests
from pgvector.psycopg import register_vector

import config

BASE = "http://localhost:8082"
fail = 0


def check(name, cond, detail=""):
    global fail
    if cond:
        print(f"[OK] {name}")
    else:
        print(f"[FAIL] {name} -> {detail}")
        fail += 1


def db():
    conn = psycopg.connect(config.PG_DSN, row_factory=psycopg.rows.dict_row)
    register_vector(conn)
    return conn


def new_user(prefix="c2e"):
    u = f"{prefix}_{uuid.uuid4().hex[:10]}"
    h = {"X-Forwarded-For": f"198.51.100.{random.randint(10, 240)}"}
    r0 = requests.post(f"{BASE}/api/auth/register", json={"username": u, "password": "Passw0rd1"},
                       headers=h, timeout=10)
    assert r0.status_code == 200, f"register failed: {r0.status_code} {r0.text}"
    r = requests.post(f"{BASE}/api/auth/login", json={"username": u, "password": "Passw0rd1"},
                      headers=h, timeout=10).json()
    assert "token" in r, f"login failed: {r}"
    return {"user": u, "headers": {"Authorization": f"Bearer {r['token']}"}}


def upload(u, path, filename):
    with open(path, "rb") as f:
        r = requests.post(f"{BASE}/api/files/upload",
                          files={"file": (filename, f)}, headers=u["headers"], timeout=30)
    assert r.status_code == 200, f"upload failed: {r.status_code} {r.text}"
    return r.json()


def wait_task(file_id, timeout=90):
    deadline = time.time() + timeout
    with db() as conn:
        while time.time() < deadline:
            row = conn.execute(
                "SELECT status, chunk_count, error FROM parse_tasks WHERE file_id=%s",
                (file_id,)).fetchone()
            if row and row["status"] not in ("pending", "parsing"):
                return row
            time.sleep(2)
    return None


def chunk_texts(file_id):
    with db() as conn:
        return [r["content"] for r in conn.execute(
            "SELECT content FROM rag_chunk WHERE file_id=%s ORDER BY seq", (file_id,)).fetchall()]


def blob_path(file_id):
    with db() as conn:
        b = conn.execute(
            "SELECT fb.stored_name, fb.owner_user_id FROM file_blob fb "
            "JOIN user_file uf ON uf.blob_id = fb.id WHERE uf.id=%s", (file_id,)).fetchone()
    return config.DATA_DIR / "files" / str(b["owner_user_id"]) / b["stored_name"]


tmp = Path(tempfile.mkdtemp(prefix="rag_c2_e_"))

# ---------- 1. 内容更新重入库：旧块清零、新块精确匹配（删旧写新一致性） ----------
# 注：Java reparse 接口语义为“仅 failed/partial 可重试”（R6 设计），内容更新场景
# 在入库层验证：同一 file_id 换新内容 parse → ingest → 旧块必须无残留
from ingest.indexer import ingest as do_ingest
from ingest.pipeline import parse_file

u = new_user()
p = tmp / "版本一.txt"
p.write_text("第一版内容，包含旧标记 OLDMARK_2026。第一版主体段落，介绍系统初始化流程。",
             encoding="utf-8")
f = upload(u, p, "版本一.txt")
t = wait_task(f["id"])
check("重入库前置 success", t and t["status"] == "success", t)
old_chunks = chunk_texts(f["id"])
check("重入库前置有块", len(old_chunks) > 0)

bpath = blob_path(f["id"])
bpath.write_text("第二版内容，包含新标记 NEWMARK_8888。第二版主体段落，介绍系统升级路径。",
                 encoding="utf-8")
res2 = parse_file(bpath)
check("版本二解析成功", res2.status == "success", res2.error)
cnt2 = do_ingest(f["id"], res2.nodes)
new_chunks = chunk_texts(f["id"])
check("重入库块数正确", len(new_chunks) == cnt2, (len(new_chunks), cnt2))
check("旧块无残留", all("OLDMARK_2026" not in c for c in new_chunks))
check("新块已入库", any("NEWMARK_8888" in c for c in new_chunks))

# ---------- 2. embed 失败：抛错且 DB 无残留 ----------
from ingest.parser.base import DocumentNode
import json as _json

p3 = tmp / "失败模拟.txt"
p3.write_text("这段内容用于模拟嵌入失败。嵌入模型不可用时任务应标记失败且无残留。", encoding="utf-8")
f3 = upload(u, p3, "失败模拟.txt")
t3 = wait_task(f3["id"])
check("embed 前置 success", t3 and t3["status"] == "success", t3)
before = len(chunk_texts(f3["id"]))

import ingest.indexer as idx_mod
real = idx_mod.embed_batch
idx_mod.embed_batch = lambda texts: (_ for _ in ()).throw(RuntimeError("simulated embed failure"))
try:
    payload = _json.loads((config.PARSED_DIR / f"{f3['id']}.json").read_text(encoding="utf-8"))
    nodes = [DocumentNode(n["type"], n["text"], n["meta"]) for n in payload["nodes"]]
    try:
        do_ingest(f3["id"], nodes)
        check("embed 失败抛异常", False, "未抛出")
    except RuntimeError as e:
        check("embed 失败抛异常", "simulated" in str(e), e)
finally:
    idx_mod.embed_batch = real
after = len(chunk_texts(f3["id"]))
check("embed 失败无残留", after == before, (before, after))

# ---------- 3. 并发：两文件同时上传，块数各自正确不串块 ----------
u2 = new_user("c2e2")
fa = tmp / "并发A.txt"
fb = tmp / "并发B.txt"
fa.write_text("并发测试 A 文件：包含 AAA 主题内容。" + "A" * 300, encoding="utf-8")
fb.write_text("并发测试 B 文件：包含 BBB 主题内容。" + "B" * 300, encoding="utf-8")
results = {}

def up_a():
    results["a"] = upload(u2, fa, "并发A.txt")

def up_b():
    results["b"] = upload(u2, fb, "并发B.txt")

ta, tb = threading.Thread(target=up_a), threading.Thread(target=up_b)
ta.start(); tb.start(); ta.join(); tb.join()
ta_row = wait_task(results["a"]["id"])
tb_row = wait_task(results["b"]["id"])
ca = len(chunk_texts(results["a"]["id"]))
cb = len(chunk_texts(results["b"]["id"]))
check("并发 A success", ta_row and ta_row["status"] == "success", ta_row)
check("并发 B success", tb_row and tb_row["status"] == "success", tb_row)
check("并发 A 块数正确", ca == (ta_row["chunk_count"] or 0), (ca, ta_row))
check("并发 B 块数正确", cb == (tb_row["chunk_count"] or 0), (cb, tb_row))
check("并发不串块", all("AAA" in c for c in chunk_texts(results["a"]["id"]))
      and all("BBB" in c for c in chunk_texts(results["b"]["id"])))

# ---------- 4. 0 块文档：纯标题 md → success + chunk_count=0 ----------
p4 = tmp / "空内容.md"
p4.write_text("# 只有标题\n\n## 没有正文\n", encoding="utf-8")
f4 = upload(u2, p4, "空内容.md")
t4 = wait_task(f4["id"])
check("0 块文档 success", t4 and t4["status"] == "success", t4)
check("0 块文档 chunk_count=0", t4 and (t4["chunk_count"] or 0) == 0, t4)
check("0 块文档无 rag_chunk 行", len(chunk_texts(f4["id"])) == 0)

print(f"\nC2 edge: {'PASS' if fail == 0 else f'{fail} FAILED'}")
sys.exit(1 if fail else 0)
