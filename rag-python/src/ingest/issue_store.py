"""
Issue 存取层（失败块闭环 v2：挂 file_id 维度）。

v2 设计变更（SQL 审查重构）：
- 旧版依赖 ingest_job（job_id 外键）+ kb_document（document_id 外键），但解析链路实际走
  parse_tasks（无 ingest_job），且旧表已下线 → issue 直接挂 user_file（file_id），
  与解析任务解耦，worker 解析后按文件同步（create_batch / resolve_for_file）。
- 幂等：UNIQUE(file_id, page_no, block_order, block_type) + ON CONFLICT DO NOTHING
  （重复解析/重试不产生重复 issue）。

状态机：
  pending → retrying → resolved | failed
  pending → skipped（用户放弃）
  pending → cancelled（文件 replace 软删时批量取消）

并发控制：所有状态流转用乐观锁
  UPDATE ... WHERE id=%s AND status=<expected> RETURNING *
保证并发双点同一 issue 只有一次成功，避免重复 chunk。
"""
import json
import logging
from db import pg_store

logger = logging.getLogger("rag.issue_store")

PENDING = "pending"
RETRYING = "retrying"
RESOLVED = "resolved"
FAILED = "failed"
SKIPPED = "skipped"
CANCELLED = "cancelled"


def _row_to_dict(r):
    if r is None:
        return None
    d = dict(r)
    for k in ("created_at", "updated_at"):
        d[k] = str(d[k]) if d.get(k) else None
    bbox = d.get("bbox")
    if isinstance(bbox, str):
        try:
            d["bbox"] = json.loads(bbox)
        except (json.JSONDecodeError, TypeError):
            d["bbox"] = None
    return d


def create_batch(file_id: int, blocks_errors: list[dict],
                 bbox_by_key: dict = None) -> int:
    """批量落 issue（worker 解析后调用，从占位节点生成）。

    blocks_errors: [{page, block, type, reason, bbox}]；同键（file+page+block+type）幂等跳过。
    bbox_by_key: {(page, block): [x0,y0,x1,y1]} 供补 bbox。
    """
    added = 0
    for e in blocks_errors:
        page = e.get("page", 0)
        block = e.get("block", 0)
        btype = e.get("type", "text")
        reason = e.get("reason", "")[:2000]
        bbox = bbox_by_key.get(f"{page}:{block}") if bbox_by_key else None
        pg_store.execute(
            """INSERT INTO issue_items
               (file_id, page_no, block_order, block_type, reason, bbox)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (file_id, page_no, block_order, block_type) DO NOTHING""",
            (file_id, page, block, btype, reason,
             json.dumps(bbox) if bbox else None))
        added += 1
    if added:
        logger.info("issues batch created: file=%s n=%d", file_id, added)
    return added


def list_issues(file_id: int) -> list[dict]:
    rows = pg_store.query(
        """SELECT * FROM issue_items WHERE file_id=%s ORDER BY page_no, block_order""",
        (file_id,))
    return [_row_to_dict(r) for r in rows]


def get_issue(issue_id: int) -> dict | None:
    return _row_to_dict(pg_store.query_one(
        "SELECT * FROM issue_items WHERE id=%s", (issue_id,)))


def mark_retrying(issue_id: int) -> dict | None:
    """乐观锁流转 pending→retrying，并发双点仅一次成功。"""
    row = pg_store.query_one(
        """UPDATE issue_items SET status=%s, updated_at=NOW()
           WHERE id=%s AND status=%s RETURNING *""",
        (RETRYING, issue_id, PENDING))
    return _row_to_dict(row)


def mark_resolved(issue_id: int, resolution: str = "") -> dict | None:
    row = pg_store.query_one(
        """UPDATE issue_items SET status=%s, resolution=%s, updated_at=NOW()
           WHERE id=%s AND status IN (%s,%s) RETURNING *""",
        (RESOLVED, resolution, issue_id, RETRYING, PENDING))
    return _row_to_dict(row)


def mark_failed(issue_id: int, resolution: str = "") -> None:
    pg_store.execute(
        """UPDATE issue_items SET status=%s, resolution=%s, updated_at=NOW()
           WHERE id=%s""", (FAILED, resolution, issue_id))


def mark_skipped(issue_id: int) -> None:
    pg_store.execute(
        """UPDATE issue_items SET status=%s, updated_at=NOW()
           WHERE id=%s AND status IN (%s,%s)""",
        (SKIPPED, issue_id, PENDING, FAILED))


def resolve_for_file(file_id: int, resolution: str = "auto:parsed") -> int:
    """文件重新解析成功后，自动解决该文件全部待处理 issue（v2 闭环收尾）。"""
    row = pg_store.query_one(
        """UPDATE issue_items SET status=%s, resolution=%s, updated_at=NOW()
           WHERE file_id=%s AND status IN (%s,%s)
           RETURNING count(*) AS n""",
        (RESOLVED, resolution, file_id, PENDING, RETRYING))
    n = (row or {}).get("n", 0)
    if n:
        logger.info("issues auto-resolved: file=%s n=%d", file_id, n)
    return n


def reset_retrying(file_id: int) -> int:
    """解析完成仍有占位：retrying 的 issue 重置回 pending（允许用户再次操作）。"""
    row = pg_store.query_one(
        """UPDATE issue_items SET status=%s, updated_at=NOW()
           WHERE file_id=%s AND status=%s
           RETURNING count(*) AS n""",
        (PENDING, file_id, RETRYING))
    return (row or {}).get("n", 0)


def cancel_for_file(file_id: int) -> int:
    """文件软删时批量取消 pending issue。"""
    row = pg_store.query_one(
        """UPDATE issue_items SET status=%s, updated_at=NOW()
           WHERE file_id=%s AND status IN (%s,%s)
           RETURNING count(*) AS n""",
        (CANCELLED, file_id, PENDING, FAILED))
    return (row or {}).get("n", 0)
