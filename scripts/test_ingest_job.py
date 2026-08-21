# -*- coding: utf-8 -*-
"""入库任务系统单元测试（第一轮修复验证）。

覆盖：job 创建/幂等、状态机流转、租约回收、重试退避、死信、进度记录。
运行：pytest scripts/test_ingest_job.py  （需要 rag_kb 库与 PG_DSN 可达）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rag-python" / "src"))

import config  # noqa: E402
from db import pg_store  # noqa: E402
from ingest import job_store  # noqa: E402


def _cleanup(job_ids):
    for jid in job_ids:
        pg_store.execute("DELETE FROM ingest_job WHERE id=%s", (jid,))


def test_create_and_claim_flow():
    """创建 → 抢任务 → running 带租约 → 完成置 done。"""
    job = job_store.create_job(
        job_key="t1_k1", user_id=1, filename="a.md", doc_type="markdown",
        file_path="/tmp/a.md", document_id=1, file_hash="x" * 64, trace_id="t1")
    assert job["status"] == "queued"

    claimed = job_store.claim_next()
    assert claimed is not None and claimed["id"] == job["id"]
    assert claimed["status"] == "running"
    assert claimed["lease_until"] is not None

    # 已 running 且租约未过期：再次 claim 不应拿到同一个
    again = job_store.claim_next()
    assert again is None or again["id"] != job["id"]

    job_store.mark_done(job["id"], {"chunks": 3})
    done = job_store.get_job(job["id"])
    assert done["status"] == "done" and done["progress"] == 1.0
    _cleanup([job["id"]])


def test_idempotent_create():
    """同 job_key 并发双插只保留一条（唯一约束兜底）。"""
    job1 = job_store.create_job(
        job_key="t2_dup", user_id=2, filename="b.pdf", doc_type="pdf",
        file_path="/tmp/b.pdf", document_id=2, file_hash="y" * 64)
    job2 = job_store.create_job(
        job_key="t2_dup", user_id=2, filename="b.pdf", doc_type="pdf",
        file_path="/tmp/b.pdf", document_id=2, file_hash="y" * 64)
    assert job1["id"] == job2["id"]
    rows = pg_store.query("SELECT count(*) AS n FROM ingest_job WHERE job_key='t2_dup'")
    assert rows[0]["n"] == 1
    _cleanup([job1["id"]])


def test_failed_backoff_and_dead():
    """失败退避：attempt 递增、未超限回 queued 且带退避时间；超限进 dead。"""
    job = job_store.create_job(
        job_key="t3_k", user_id=1, filename="c.txt", doc_type="text",
        file_path="/tmp/c.txt", document_id=1, file_hash="z" * 64)
    jid = job["id"]

    r1 = job_store.mark_failed(jid, "boom1")
    assert r1["status"] == "queued" and r1["attempt"] == 1
    j = job_store.get_job(jid)
    assert j["lease_until"] is not None, "queued 重试任务应带退避到期时间"

    r2 = job_store.mark_failed(jid, "boom2")
    assert r2["status"] == "queued" and r2["attempt"] == 2

    # 退避中不可被 claim
    claimed = job_store.claim_next()
    assert claimed is None or claimed["id"] != jid

    r3 = job_store.mark_failed(jid, "boom3")
    assert r3["status"] == "dead", r3

    # 死信可人工重试
    rj = job_store.retry_job(jid)
    assert rj is not None and rj["status"] == "queued"
    j = job_store.get_job(jid)
    assert j["attempt"] == 0 and j["lease_until"] is None

    # 重试后立即可被 claim
    claimed = job_store.claim_next()
    assert claimed is not None and claimed["id"] == jid
    _cleanup([jid])


def test_lease_expiry_reclaim():
    """租约过期任务可被回收（worker 崩溃恢复语义）。"""
    job = job_store.create_job(
        job_key="t4_k", user_id=1, filename="d.txt", doc_type="text",
        file_path="/tmp/d.txt", document_id=1, file_hash="w" * 64)
    jid = job["id"]

    # 模拟占位后过期：直接置 running + 过去时间
    pg_store.execute(
        "UPDATE ingest_job SET status='running', lease_until=NOW() - INTERVAL '10 minutes' WHERE id=%s",
        (jid,))
    claimed = job_store.claim_next()
    assert claimed is not None and claimed["id"] == jid, "过期租约应被回收"
    _cleanup([jid])


def test_progress_detail_series():
    """进度明细 series 追加历史（job_key 唯一，cleanup 用 finally 保证执行）。"""
    import uuid
    key = "t5_" + uuid.uuid4().hex[:8]
    job = job_store.create_job(
        job_key=key, user_id=1, filename="e.md", doc_type="markdown",
        file_path="/tmp/e.md", document_id=1, file_hash="v" * 64)
    jid = job["id"]
    try:
        # 置于退避中（queued + 未来 lease）：worker 不会抢，本测试专注更新/状态写路径
        pg_store.execute(
            "UPDATE ingest_job SET status='queued', lease_until=NOW() + INTERVAL '10 minutes' WHERE id=%s",
            (jid,))
        job_store.update_progress(jid, stage="parsing", progress=0.3, detail={"page": 2})
        job_store.update_progress(jid, stage="chunking", progress=0.6, detail={"n_chunks": 5})
        job_store.mark_done(jid, {"chunks": 5})
        j = job_store.get_job(jid)
        series = j["step_detail"].get("series", [])
        # 三条：parsing / chunking / mark_done 各追加一条
        assert len(series) == 3, series
        assert [s["stage"] for s in series] == ["parsing", "chunking", "done"], series
        assert series[0]["page"] == 2
        assert series[1]["n_chunks"] == 5
        assert series[2]["result"] == {"chunks": 5}
    finally:
        _cleanup([jid])