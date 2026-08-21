"""
Issue 存取层（第三轮：失败块闭环）。

职责边界：只读写 issue_items 表，不含识别/入库逻辑。

状态机：
  pending → retrying → resolved | failed
  pending → skipped（用户放弃）
  pending → cancelled（文档 replace 软删时批量取消）

并发控制（审查修订 R3）：所有状态流转用乐观锁
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


def create_batch(job_id: int, document_id: int, blocks_errors: list[dict],
                 bbox_by_key: dict = None) -> int:
    """批量落 issue（worker 完成解析后调用，从 stats.page_errors + 块 bbox 生成）。

    blocks_errors: [{page, block, type, reason, bbox}]；已有则按 key 幂等跳过。
    bbox_by_key: {(page, block): [x0,y0,x1,y1]} 供补 bbox。
    """
    added = 0
    for e in blocks_errors:
        page = e.get("page", 0)
        block = e.get("block", 0)
        btype = e.get("type", "text")
        reason = e.get("reason", "")[:2000]
        bbox = bbox_by_key.get(f"{page}:{block}") if bbox_by_key else None
        exists = pg_store.query_one(
            """SELECT id FROM issue_items
               WHERE job_id=%s AND page_no=%s AND block_order=%s AND block_type=%s""",
            (job_id, page, block, btype))
        if exists:
            continue
        pg_store.execute(
            """INSERT INTO issue_items
               (job_id, document_id, page_no, block_order, block_type, reason, bbox)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (job_id, document_id, page, block, btype, reason,
             json.dumps(bbox) if bbox else None))
        added += 1
    return added


def list_issues(job_id: int) -> list[dict]:
    rows = pg_store.query(
        """SELECT * FROM issue_items WHERE job_id=%s ORDER BY page_no, block_order""",
        (job_id,))
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


def cancel_for_document(document_id: int) -> int:
    """文档 replace 软删时批量取消 pending issue（审查修订 R5）。"""
    row = pg_store.query_one(
        """UPDATE issue_items SET status=%s, updated_at=NOW()
           WHERE document_id=%s AND status IN (%s,%s)
           RETURNING count(*) AS n""",
        (CANCELLED, document_id, PENDING, FAILED))
    return (row or {}).get("n", 0)