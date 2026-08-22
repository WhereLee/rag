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
from ingest.parser.base import DocumentNode
from ingest.quality import PLACEHOLDER_MARKERS

logger = logging.getLogger("rag.worker")

POLL_INTERVAL = 3      # 轮询间隔（秒）
STALE_MINUTES = 5      # parsing 停留超时回收（worker 崩溃恢复）
MAX_ATTEMPTS = 3       # 自动重试上限（手动重试不受限）
TIMEOUT = 300          # 单文件解析超时（多图文档 VLM 描述是预期成本，180s 对 6+ 图偏紧）

_last_report_ts = 0.0  # 页级进度写库节流（高频回调不刷库）


def _progress(file_id: int, stage: str, progress: float, force: bool = False) -> None:
    """阶段/进度回报写 parse_tasks（前端轮询展示）。

    force=True：阶段切换（chunking/embedding/indexing）必须落库；
    force=False：页级高频回调按 1s 节流（50 页 PDF 只写 ~几十次而非每页）。
    """
    global _last_report_ts
    now = time.time()
    if not force and now - _last_report_ts < 1.0:
        return
    _last_report_ts = now
    with connect() as conn:
        conn.execute(
            "UPDATE parse_tasks SET stage=%s, progress=%s, updated_at=now() "
            "WHERE file_id=%s", (stage, progress, file_id))


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
            "chunk_count=%s, stage='done', progress=1.0, "
            "attempt = CASE WHEN %s='failed' THEN attempt + 1 ELSE attempt END, updated_at=now() "
            "WHERE file_id=%s",
            (status, error, duration_ms, node_count, chunk_count, status, file_id))


def process_task(task: dict) -> None:
    """解析单个任务：产物 JSON 落盘 + 状态更新（幂等，可安全重跑）。"""
    file_id, blob_id = task["file_id"], task["blob_id"]
    _progress(file_id, "parsing", 0.05, force=True)  # 起始进度（前端立即可见）
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

    result = parse_file(path, timeout=TIMEOUT,
                        progress_cb=lambda s, p, d: _progress(file_id, s, p))
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
            chunk_count = ingest(file_id, result.nodes,
                                 progress_cb=lambda s, p: _progress(file_id, s, p, force=True))
        except Exception as e:
            logger.exception("ingest failed file_id=%s: %s", file_id, e)
            _finish(file_id, "failed", f"入库失败: {e}", payload["duration_ms"],
                    len(result.nodes))
            return
        # 失败块闭环：入库成功后同步 issue（无占位自动解决；有占位新增/重置）
        try:
            _sync_issues(file_id, result)
        except Exception as e:
            logger.warning("issues sync failed file_id=%s: %s", file_id, e)
        _finish(file_id, result.status, result.error, payload["duration_ms"],
                len(result.nodes), chunk_count)
    else:
        _finish(file_id, result.status, result.error, payload["duration_ms"],
                len(result.nodes))
    logger.info("parsed file_id=%s status=%s nodes=%d err=%s",
                file_id, result.status, len(result.nodes), result.error or "-")


def _extract_placeholders(result) -> tuple[list[dict], dict]:
    """从解析结果提取占位节点（失败块）→ issue 输入（page/block/type/reason/bbox）。

    block 用节点在解析产物中的序号（同文件同解析器顺序稳定，重试可定位）。
    """
    blocks_errors, bbox_by_key = [], {}
    for i, n in enumerate(result.nodes):
        if any(n.text.startswith(m) for m in PLACEHOLDER_MARKERS):
            page = n.meta.get("page") or 0
            blocks_errors.append({
                "page": page,
                "block": i,
                "type": n.type,
                "reason": n.text[:2000],
            })
            if n.meta.get("bbox"):
                bbox_by_key[f"{page}:{i}"] = n.meta["bbox"]
    return blocks_errors, bbox_by_key


def _sync_issues(file_id: int, result) -> None:
    """解析完成后同步失败块 issue（v2 闭环）：
    - 无占位节点 → 该文件全部 pending/retrying issue 自动 resolved（重试成功收尾）
    - 有占位节点 → 新增/刷新 issue（幂等），retrying 重置回 pending（允许再次操作）
    """
    from ingest import issue_store
    blocks_errors, bbox_by_key = _extract_placeholders(result)
    if not blocks_errors:
        issue_store.resolve_for_file(file_id)
    else:
        issue_store.create_batch(file_id, blocks_errors, bbox_by_key)
        issue_store.reset_retrying(file_id)


def process_retry_job(job: dict) -> None:
    """消费 ingest_job（job_type=block_retry，替代图替换场景）：
    解析替代图 → 替换原解析产物中目标页节点 → 重新入库 → issue resolved。
    失败走 mark_failed（指数退避重试），终态 mark_done/mark_dead。
    """
    from ingest import issue_store, job_store
    issue = issue_store.get_issue(job["issue_id"]) if job.get("issue_id") else None
    if not issue:
        job_store.mark_dead(job["id"], "issue 不存在")
        return
    resolution = issue.get("resolution") or ""
    if not resolution.startswith("replace:"):
        job_store.mark_dead(job["id"], f"resolution 非法: {resolution}")
        issue_store.mark_failed(issue["id"], "任务死信（resolution 非法）")
        return
    alt_path = Path(resolution[len("replace:"):])
    if not alt_path.exists():
        job_store.mark_dead(job["id"], f"替代图缺失: {alt_path}")
        issue_store.mark_failed(issue["id"], f"替代图缺失: {alt_path}")
        return

    alt_result = parse_file(alt_path, timeout=TIMEOUT)
    if alt_result.status == "failed" or not alt_result.nodes:
        job_store.mark_failed(job["id"], f"替代图解析失败: {alt_result.error}")
        issue_store.mark_failed(issue["id"], f"替代图解析失败: {alt_result.error}")
        return

    # 读原解析产物（data/parsed/{file_id}.json），替换目标页节点（保持顺序）
    payload_path = config.PARSED_DIR / f"{issue['file_id']}.json"
    if not payload_path.exists():
        job_store.mark_failed(job["id"], "原解析产物缺失")
        issue_store.mark_failed(issue["id"], "原解析产物缺失")
        return
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        nodes = [DocumentNode(t, text, meta) for t, text, meta in payload.get("nodes", [])]
    except Exception as e:
        job_store.mark_failed(job["id"], f"原解析产物读取失败: {e}")
        issue_store.mark_failed(issue["id"], f"原解析产物读取失败: {e}")
        return

    page = issue.get("page_no") or 0
    for n in alt_result.nodes:      # 替代图解析结果修正页号/来源
        n.meta["page"] = page
        n.meta["source"] = job.get("filename") or payload.get("file_id")
    new_nodes, inserted = [], False
    for n in nodes:
        if (n.meta.get("page") or 0) == page and not inserted:
            new_nodes.extend(alt_result.nodes)
            inserted = True
        if (n.meta.get("page") or 0) != page:
            new_nodes.append(n)
    if not inserted:
        new_nodes.extend(alt_result.nodes)
    if not new_nodes:
        job_store.mark_failed(job["id"], "替换后无节点")
        issue_store.mark_failed(issue["id"], "替换后无节点")
        return
    try:
        ingest(issue["file_id"], new_nodes)
    except Exception as e:
        logger.exception("replace ingest failed issue=%s: %s", issue["id"], e)
        job_store.mark_failed(job["id"], f"入库失败: {e}")
        issue_store.mark_failed(issue["id"], f"入库失败: {e}")
        return
    issue_store.mark_resolved(issue["id"], "replaced")
    job_store.mark_done(job["id"], {"action": "replace", "nodes": len(new_nodes)})
    logger.info("issue %s replaced (file=%s)", issue["id"], issue["file_id"])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger.info("parse worker started: poll=%ss stale=%smin max_attempts=%s",
                POLL_INTERVAL, STALE_MINUTES, MAX_ATTEMPTS)
    while True:
        try:
            recover_stale()
            task = claim_next()
            if task is None:
                # 主解析队列空闲 → 消费失败块替换队列（ingest_job，block_retry）
                from ingest import job_store
                job = job_store.claim_next()
                if job is not None:
                    t0 = time.time()
                    logger.info("claim job id=%s type=%s", job["id"], job.get("job_type"))
                    process_retry_job(job)
                    logger.info("done job id=%s in %.1fs", job["id"], time.time() - t0)
                    continue
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
