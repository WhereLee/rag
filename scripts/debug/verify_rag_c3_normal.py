# -*- coding: utf-8 -*-
"""C3 正常测试：corpus 文档入库 → 黄金集四组对比评估 → Recall@5 验收。
前置：网关(8082) + 新版 parse worker 已启动。用法: python scripts/debug/verify_rag_c3_normal.py
"""
import random
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "rag-python" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "rag-python" / "eval"))

import psycopg
import psycopg.rows
import requests

import config
from run_retrieval_eval import run as run_eval

BASE = "http://localhost:8082"
CORPUS = Path(__file__).resolve().parents[2] / "data" / "corpus"
DOCS = [
    "standard/文档智能解析技术规范 LT-S 001-2026.pdf",
    "whitepaper/企业智能文档管理白皮书（2026年）.pdf",
    "tech/fastapi_readme.md",
    "tech/mineru_readme.md",
    "whitepaper/rag_survey_arxiv.pdf",
]
fail = 0


def check(name, cond, detail=""):
    global fail
    if cond:
        print(f"[OK] {name}")
    else:
        print(f"[FAIL] {name} -> {detail}")
        fail += 1


def new_user():
    u = f"c3n_{uuid.uuid4().hex[:10]}"
    h = {"X-Forwarded-For": f"198.51.100.{random.randint(10, 240)}"}
    r0 = requests.post(f"{BASE}/api/auth/register", json={"username": u, "password": "Passw0rd1"},
                       headers=h, timeout=10)
    assert r0.status_code == 200, f"register failed: {r0.status_code} {r0.text}"
    r = requests.post(f"{BASE}/api/auth/login", json={"username": u, "password": "Passw0rd1"},
                      headers=h, timeout=10).json()
    return {"user": u, "headers": {"Authorization": f"Bearer {r['token']}"}}


def db():
    conn = psycopg.connect(config.PG_DSN, row_factory=psycopg.rows.dict_row)
    return conn


def upload(u, path, filename):
    with open(path, "rb") as f:
        r = requests.post(f"{BASE}/api/files/upload",
                          files={"file": (filename, f)}, headers=u["headers"], timeout=60)
    assert r.status_code == 200, f"upload failed: {r.status_code} {r.text}"
    return r.json()


def wait_tasks(file_ids, timeout=300):
    """等待多个文件全部解析终态，返回 (全部成功?, 任务行字典)。"""
    deadline = time.time() + timeout
    rows = {}
    with db() as conn:
        while time.time() < deadline:
            all_done = True
            for fid in file_ids:
                if fid not in rows:
                    row = conn.execute(
                        "SELECT status, chunk_count, error FROM parse_tasks WHERE file_id=%s",
                        (fid,)).fetchone()
                    if row and row["status"] not in ("pending", "parsing"):
                        rows[fid] = row
                    else:
                        all_done = False
            if all_done:
                return all(r["status"] in ("success", "partial") for r in rows.values()), rows
            time.sleep(3)
    return False, rows


u = new_user()
file_ids = []
for rel in DOCS:
    p = CORPUS / rel
    f = upload(u, p, p.name)
    file_ids.append(f["id"])
    print(f"uploaded {p.name} -> id={f['id']}")

ok, rows = wait_tasks(file_ids)
# 超时/失败任务：走 reparse 重试一次（VLM 图片描述缓存命中后二次解析快；R6 语义：仅 failed/partial 可重试）
retried = set()
for fid in list(rows):
    if rows[fid]["status"] not in ("success", "partial") and fid not in retried:
        print(f"reparse retry file_id={fid} ({rows[fid]['status']})")
        requests.post(f"{BASE}/api/files/{fid}/reparse", headers=u["headers"], timeout=10)
        retried.add(fid)
if retried:
    ok2, rows2 = wait_tasks([fid for fid in retried], timeout=420)
    rows.update(rows2)
    ok = ok and ok2
check("corpus 全部解析成功", ok, {k: v["status"] for k, v in rows.items()})
check("corpus 全部有块", all((r["chunk_count"] or 0) > 0 for r in rows.values()),
      {k: v["chunk_count"] for k, v in rows.items()})

# 用户 id：从 user_file 反查
with db() as conn:
    user_id = conn.execute("SELECT user_id FROM user_file WHERE id=%s", (file_ids[0],)).fetchone()["user_id"]

# 黄金集四组对比评估
out_path = str(Path(__file__).resolve().parents[2] / "logs" / "retrieval_eval_c3.json")
stats, per_query = run_eval(user_id, top_k=5, out_path=out_path)

n = len(per_query)
check("RRF+rerank Recall@5 ≥ 0.6", stats["rrf_rerank"] / n >= 0.6,
      f"{stats['rrf_rerank']}/{n}")
check("RRF ≥ max(纯向量, 纯BM25)", stats["rrf"] >= max(stats["vector"], stats["bm25"]),
      f"rrf={stats['rrf']} vector={stats['vector']} bm25={stats['bm25']}")
check("RRF+rerank ≥ RRF", stats["rrf_rerank"] >= stats["rrf"],
      f"full={stats['rrf_rerank']} rrf={stats['rrf']}")

print(f"\nC3 normal: {'PASS' if fail == 0 else f'{fail} FAILED'}")
sys.exit(1 if fail else 0)
