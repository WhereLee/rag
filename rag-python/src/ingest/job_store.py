"""
入库任务存储（ingest_job 表访问层）。

职责边界：只读写 ingest_job 表，不含任务执行逻辑。
- create_job：入队（幂等键唯一约束兜底并发双插）
- claim_next：SKIP LOCKED 抢任务，置 running + 续租
- update_progress：阶段/进度/明细更新
- mark_failed：attempt+1；未超限回 queued 并写入退避到期时间，超限置 dead（死信）
- retry_job：dead/failed 人工重试入口（attempt 清零，立即可执行）

租约/退避时间全部用 DB 侧 NOW() 计算，规避时区/时钟漂移。
lease_until 双语义：running 时=租约截止；queued 时=退避到期时间（NULL=立即可抢）。
"""
import json
import logging

from db import pg_store

logger = logging.getLogger("rag.job_store")

LEASE_MINUTES = 5          # 单次执行租约时长；超时未完成视为 worker 崩溃，可被回收
BACKOFF_BASE = 5           # 失败退避基数（秒）：attempt1→5s, attempt2→10s
MAX_BACKOFF = 60


def _row_to_dict(r) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in ("created_at", "updated_at", "lease_until"):
        d[k] = str(d[k]) if d.get(k) else None
    if isinstance(d.get("step_detail"), str):      # 兼容 JSONB 已解串的场景
        try:
            d["step_detail"] = json.loads(d["step_detail"])
        except (json.JSONDecodeError, TypeError):
            d["step_detail"] = {}
    return d


def create_job(job_key: str, user_id: int, filename: str, doc_type: str,
               file_path: str, document_id: int, file_hash: str,
               trace_id: str = "", job_type: str = "full", issue_id: int = None) -> dict:
    """创建任务。job_key 冲突（并发双插）时返回已存在任务。

    第三轮：job_type='block_retry' 表示单块重试（issue_id 指向 issue_items）。
    """
    row = pg_store.query_one(
        """INSERT INTO ingest_job
              (job_key, user_id, filename, doc_type, file_path, document_id, file_hash,
               trace_id, job_type, issue_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (job_key) DO UPDATE SET updated_at=NOW()
           RETURNING id, status""",
        (job_key, user_id, filename, doc_type, file_path, document_id, file_hash,
         trace_id, job_type, issue_id))
    return {"id": row["id"], "status": row["status"]}


def _backoff_secs(attempt_after: int) -> int:
    """第 attempt_after 次失败后的退避秒数：5, 10, 20, ... 上限 60。"""
    return min(BACKOFF_BASE * (2 ** (attempt_after - 1)), MAX_BACKOFF)


def claim_next() -> dict | None:
    """抢一个可执行任务（原子）。FOR UPDATE SKIP LOCKED 保证多 worker 并发消费不重复。
    可执行 = queued 且退避到期（lease_until 为空或过去）或 running 且租约过期（worker 崩溃回收）。"""
    row = pg_store.query_one(
        """UPDATE ingest_job AS j
           SET status='running',
               lease_until=NOW() + INTERVAL '%s minutes',
               updated_at=NOW()
           FROM (
               SELECT id FROM ingest_job
               WHERE (status='queued' AND (lease_until IS NULL OR lease_until < NOW()))
                  OR (status='running' AND lease_until < NOW())
               ORDER BY id
               LIMIT 1
               FOR UPDATE SKIP LOCKED
           ) AS sub
           WHERE j.id = sub.id
           RETURNING j.*""",
        (LEASE_MINUTES,))
    return _row_to_dict(row)


def update_progress(job_id: int, stage: str = None, progress: float = None,
                    detail: dict = None, status: str = None):
    """阶段/进度/明细更新。detail 追加进 step_detail.series 数组。

    审查修订 R1（续租）：每次进度更新同时续租——worker 每完成一块/一页回调即续期，
    消除长任务（块级并发拉长耗时）租约到期被其他 worker 抢走重复处理的竞态。
    终态（done/dead/failed）不再续租。
    """
    sets = ["updated_at=NOW()"]
    params: list = []
    if stage is not None:
        sets.append("stage=%s")
        params.append(stage)
    if progress is not None:
        sets.append("progress=%s")
        params.append(progress)
    if status is not None:
        sets.append("status=%s")
        params.append(status)
    if status not in ("done", "dead", "failed"):
        sets.append(f"lease_until = NOW() + INTERVAL '{LEASE_MINUTES} minutes'")
    if detail is not None:
        # series 数组追加一条阶段记录（对象合并会按 key 覆盖，必须走数组拼接；
        # 自动补全 stage/progress，保证各调用方记录格式一致）
        merged = dict(detail)
        if stage is not None:
            merged.setdefault("stage", stage)
        if progress is not None:
            merged.setdefault("progress", progress)
        sets.append("step_detail = jsonb_build_object('series', "
                    "coalesce(step_detail->'series','[]'::jsonb) || %s::jsonb)")
        params.append(json.dumps([merged], ensure_ascii=False))
    params.append(job_id)
    pg_store.execute(
        f"UPDATE ingest_job SET {', '.join(sets)} WHERE id=%s", tuple(params))


def mark_done(job_id: int, result: dict = None):
    update_progress(job_id, stage="done", progress=1.0, status="done",
                    detail={"stage": "done", "progress": 1.0, "result": result or {}})
    logger.info("job %s done", job_id)


def mark_failed(job_id: int, error: str) -> dict | None:
    """记录失败并决策：attempt+1；未超限回 queued（写退避到期时间），超限置 dead。
    返回 {status, attempt} 或 None（任务不存在）。"""
    cur = pg_store.query_one(
        "SELECT attempt, max_attempts FROM ingest_job WHERE id=%s", (job_id,))
    if not cur:
        return None
    new_attempt = cur["attempt"] + 1
    backoff = _backoff_secs(new_attempt)
    row = pg_store.query_one(
        """UPDATE ingest_job
           SET attempt=%s,
               status=CASE WHEN %s >= max_attempts THEN 'dead' ELSE 'queued' END,
               lease_until=CASE WHEN %s < max_attempts
                                THEN NOW() + INTERVAL '%s seconds' ELSE NULL END,
               error=%s,
               updated_at=NOW()
           WHERE id=%s
           RETURNING status, attempt""",
        (new_attempt, new_attempt, new_attempt, backoff, error[:2000], job_id))
    if row["status"] == "dead":
        logger.error("job %s DEAD after %d attempts: %.300s", job_id, row["attempt"], error)
    else:
        logger.warning("job %s failed (attempt %d/%d): %.300s (backoff %ss)",
                       job_id, row["attempt"], row["attempt"], error, backoff)
    return {"status": row["status"], "attempt": row["attempt"]}


def mark_dead(job_id: int, error: str) -> dict | None:
    """直接置死信（不消耗重试额度）：文件级确定性失败等。返回 {status, attempt} 或 None。"""
    row = pg_store.query_one(
        """UPDATE ingest_job
           SET status='dead', error=%s, lease_until=NULL, updated_at=NOW()
           WHERE id=%s
           RETURNING status, attempt""", (error[:2000], job_id))
    if row:
        logger.error("job %s DEAD (no-retry): %s", job_id, error[:300])
        return {"status": row["status"], "attempt": row["attempt"]}
    return None


def retry_job(job_id: int) -> dict | None:
    """人工重试：failed/dead → queued，attempt 清零，clearing 错误与退避。"""
    row = pg_store.query_one(
        """UPDATE ingest_job
           SET status='queued', attempt=0, error=NULL, lease_until=NULL,
               step_detail=coalesce(step_detail,'{}'::jsonb) || '{"retried":true}'::jsonb,
               updated_at=NOW()
           WHERE id=%s AND status IN ('failed','dead')
           RETURNING id, status, attempt""", (job_id,))
    if not row:
        return None
    logger.info("job %s manually retried", job_id)
    return dict(row)


def get_job(job_id: int) -> dict | None:
    return _row_to_dict(pg_store.query_one(
        "SELECT * FROM ingest_job WHERE id=%s", (job_id,)))


def list_jobs(user_id: int, limit: int = 20) -> list[dict]:
    rows = pg_store.query(
        """SELECT * FROM ingest_job WHERE user_id=%s ORDER BY id DESC LIMIT %s""",
        (user_id, limit))
    return [_row_to_dict(r) for r in rows]