"""解析任务消费 worker（独立进程）：轮询 parse_tasks → 解析 → 产物落盘 → 状态更新。

- 通道：parse_tasks 表（Java 网关入队），SELECT ... FOR UPDATE SKIP LOCKED 并发安全
- 崩溃恢复：parsing 停留超过 STALE_MINUTES 回收为 pending
- 自动重试上限：失败 attempt+1，>= MAX_ATTEMPTS 不再自动消费（手动重试由网关重置 attempt）
- 单循环异常不退出（worker 长寿进程）
"""
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # rag-python/src

import config
from db.pg_store import connect
from ingest.indexer import ingest
from ingest.pipeline import parse_file

logger = logging.getLogger("rag.worker")

POLL_INTERVAL = 3      # 轮询间隔（秒）
STALE_MINUTES = 5      # parsing 停留超时回收（worker 崩溃恢复）
MAX_ATTEMPTS = 3       # 自动重试上限（手动重试不受限）
TIMEOUT = 300          # 单文件解析超时（多图文档 VLM 描述是预期成本，180s 对 6+ 图偏紧）


def recover_stale() -> None:
    """崩溃恢复：parsing 状态停留超过阈值 → 置回 pending。"""
    with connect() as conn:
        conn.execute(
            "UPDATE parse_tasks SET status='pending', updated_at=now() "
            "WHERE status='parsing' AND updated_at < now() - make_interval(secs => %s)",
            (STALE_MINUTES * 60,))


def claim_next() -> dict | None:
    """原子拉取一个 pending 任务（SKIP LOCKED 防多 worker 竞争）。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT pt.file_id, uf.blob_id, uf.user_id "
            "FROM parse_tasks pt JOIN user_file uf ON uf.id = pt.file_id "
            "WHERE pt.status='pending' AND pt.attempt < %s "
            "ORDER BY pt.updated_at LIMIT 1 FOR UPDATE OF pt SKIP LOCKED",
            (MAX_ATTEMPTS,)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE parse_tasks SET status='parsing', updated_at=now() WHERE file_id=%s",
                     (row["file_id"],))
        return dict(row)


def _finish(file_id: int, status: str, error: str, duration_ms: int, node_count: int,
            chunk_count: int = 0) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE parse_tasks SET status=%s, error=%s, duration_ms=%s, node_count=%s, "
            "chunk_count=%s, "
            "attempt = CASE WHEN %s='failed' THEN attempt + 1 ELSE attempt END, updated_at=now() "
            "WHERE file_id=%s",
            (status, error, duration_ms, node_count, chunk_count, status, file_id))


def process_task(task: dict) -> None:
    """解析单个任务：产物 JSON 落盘 + 状态更新（幂等，可安全重跑）。"""
    file_id, blob_id = task["file_id"], task["blob_id"]
    with connect() as conn:
        blob = conn.execute(
            "SELECT stored_name, owner_user_id FROM file_blob WHERE id=%s", (blob_id,)).fetchone()
    if blob is None:
        _finish(file_id, "failed", "物理文件记录缺失", 0, 0)
        return
    # 存储结构：data/files/{ownerUserId}/{storedName}——秒传复用 blob 时物理文件在首个上传者目录
    path = config.DATA_DIR / "files" / str(blob["owner_user_id"]) / blob["stored_name"]
    if not path.exists():
        _finish(file_id, "failed", "物理文件缺失", 0, 0)
        return

    result = parse_file(path, timeout=TIMEOUT)
    payload = {
        "file_id": file_id,
        "status": result.status,
        "parsed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "flags": result.flags,
        "duration_ms": int(result.duration * 1000),
        "error": result.error,
        "warnings": result.warnings,
        "nodes": [{"type": n.type, "text": n.text, "meta": n.meta} for n in result.nodes],
    }
    config.PARSED_DIR.mkdir(parents=True, exist_ok=True)
    (config.PARSED_DIR / f"{file_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    # 解析成功（含部分降级）→ 链式入库（切块+embedding+写 rag_chunk）；入库失败标 failed 可重试
    if result.status in ("success", "partial"):
        try:
            chunk_count = ingest(file_id, result.nodes)
        except Exception as e:
            logger.exception("ingest failed file_id=%s: %s", file_id, e)
            _finish(file_id, "failed", f"入库失败: {e}", payload["duration_ms"],
                    len(result.nodes))
            return
        _finish(file_id, result.status, result.error, payload["duration_ms"],
                len(result.nodes), chunk_count)
    else:
        _finish(file_id, result.status, result.error, payload["duration_ms"],
                len(result.nodes))
    logger.info("parsed file_id=%s status=%s nodes=%d err=%s",
                file_id, result.status, len(result.nodes), result.error or "-")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger.info("parse worker started: poll=%ss stale=%smin max_attempts=%s",
                POLL_INTERVAL, STALE_MINUTES, MAX_ATTEMPTS)
    while True:
        try:
            recover_stale()
            task = claim_next()
            if task is None:
                time.sleep(POLL_INTERVAL)
                continue
            t0 = time.time()
            logger.info("claim task file_id=%s", task["file_id"])
            process_task(task)
            logger.info("done file_id=%s in %.1fs", task["file_id"], time.time() - t0)
        except Exception as e:
            logger.exception("worker loop error: %s", e)
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
