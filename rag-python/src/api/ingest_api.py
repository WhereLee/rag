"""
入库 API（v2：仅保留失败块闭环相关接口）。

v2 变更（SQL 审查重构）：
- 旧上传路径（/upload 走 sync_service 写 kb_document）已删除——上传统一走 Java 网关
  文件域（user_file + parse_tasks + 分片上传），Python 侧不再有第二入口
- 旧文档管理接口（/documents /status /chunks）已删除——等价能力在 MCP/Agent 工具（新链路）
- issues 从 job 维度改为 file 维度（issue_items 挂 file_id，与解析任务解耦）
- describe 兜底写 rag_chunk（旧版写 kb_chunk 导致检索永远读不到）
- retry 走 parse_tasks 通道（重解析整文件，幂等）；replace 走 ingest_job（替代图替换）
"""
import json
import logging
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, HTTPException, Query, Body, Request
from fastapi.responses import JSONResponse

import config
from ingest import job_store

router = APIRouter(tags=["ingest"])

# ===== 安全配置（与 Java 网关文件域一致） =====
MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50MB


def _get_user_id(request: Request) -> int:
    """从请求头 X-User-Id 提取用户 ID（多租户模式必备，由网关注入）。"""
    uid = request.headers.get("X-User-Id")
    if uid is None:
        raise HTTPException(400, "缺少用户身份 X-User-Id")
    try:
        return int(uid)
    except ValueError:
        raise HTTPException(400, "X-User-Id 必须是整数")


def _file_owned(user_id: int, file_id: int) -> dict:
    """归属校验：文件必须属于当前用户（不存在/不属于一律 404，不泄露存在性）。"""
    from db import pg_store
    row = pg_store.query_one(
        "SELECT id, filename FROM user_file WHERE id=%s AND user_id=%s AND status=1",
        (file_id, user_id))
    if not row:
        raise HTTPException(404, "文件不存在")
    return row


# ===== 失败块闭环 =====

@router.get("/files/{file_id}/issues")
def file_issues(file_id: int, request: Request = None):
    """失败块清单（文件维度）。"""
    from ingest import issue_store
    user_id = _get_user_id(request) if request else None
    _file_owned(user_id, file_id)
    return issue_store.list_issues(file_id)


@router.post("/issues/{issue_id}/retry")
def retry_issue(issue_id: int, request: Request = None):
    """重试失败块：置文件解析任务回 pending（整文件重解析，幂等），202 语义由任务通道保证。"""
    from db import pg_store
    from ingest import issue_store
    user_id = _get_user_id(request) if request else None
    issue = issue_store.get_issue(issue_id)
    if not issue:
        raise HTTPException(404, "问题块不存在")
    _file_owned(user_id, issue["file_id"])
    if issue["status"] == "retrying":
        raise HTTPException(409, "该问题块已在处理中")
    marked = issue_store.mark_retrying(issue_id)
    if marked is None:
        raise HTTPException(409, "该问题块已在处理中或已解决")
    try:
        # 置回 pending（attempt 重置：issue 重试属手动操作，不受自动重试上限约束）；
        # 无任务记录时兜底插入（ON CONFLICT 防并发双插）
        pg_store.execute(
            "INSERT INTO parse_tasks (file_id, status) VALUES (%s, 'pending') "
            "ON CONFLICT (file_id) DO UPDATE SET status='pending', attempt=0, "
            "error=NULL, updated_at=now()",
            (issue["file_id"],))
    except Exception as e:
        issue_store.mark_failed(issue_id, f"入队失败: {e}")
        raise HTTPException(500, f"重试入队失败：{e}")
    return {"issue_id": issue_id, "status": "retrying", "action": "reparse"}


@router.post("/issues/{issue_id}/replace")
async def replace_issue(issue_id: int, request: Request = None,
                        file: UploadFile = File(...)):
    """上传替代图（源图损坏时用户自备原图），创建 block_retry 任务（202 异步）。"""
    from ingest import issue_store
    user_id = _get_user_id(request) if request else None
    issue = issue_store.get_issue(issue_id)
    if not issue:
        raise HTTPException(404, "问题块不存在")
    owned = _file_owned(user_id, issue["file_id"])
    if issue["status"] == "retrying":
        raise HTTPException(409, "该问题块已在处理中")
    if issue["block_type"] != "image":
        raise HTTPException(400, "仅图片类失败块支持替代图")
    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(413, "文件过大")
    if not content:
        raise HTTPException(400, "替代图内容为空")
    alt_dir = Path(config.DATA_DIR) / "issue_alts"
    alt_dir.mkdir(parents=True, exist_ok=True)
    alt_path = alt_dir / f"issue_{issue_id}.png"
    alt_path.write_bytes(content)
    marked = issue_store.mark_retrying(issue_id)
    if marked is None:
        raise HTTPException(409, "该问题块已在处理中或已解决")
    # 写 alt 图地址进 issue resolution（worker replace 分支读取）
    from db import pg_store
    pg_store.execute(
        "UPDATE issue_items SET resolution=%s, updated_at=NOW() WHERE id=%s",
        (f"replace:{alt_path}", issue_id))
    job_key = f"retry-issue-{issue_id}"
    jobinfo = job_store.create_job(
        job_key, user_id, owned["filename"], "image", str(alt_path),
        issue["file_id"], "replace", "",
        job_type="block_retry", issue_id=issue_id)
    return JSONResponse({"job_id": jobinfo["id"], "status": "queued"},
                        status_code=202)


@router.post("/issues/{issue_id}/describe")
def describe_issue(issue_id: int, body: dict = Body(...), request: Request = None):
    """文字描述兜底（同步：注入检测+长度限制，直接写入 rag_chunk + embedding）。

    v2 修复：旧版写 kb_chunk（旧表），新链路检索 rag_chunk 永远读不到——功能静默失效；
    现写入 rag_chunk（seq=max+1 追加，不重排现有块）。
    """
    from db import pg_store
    from ingest import issue_store
    user_id = _get_user_id(request) if request else None
    issue = issue_store.get_issue(issue_id)
    if not issue:
        raise HTTPException(404, "问题块不存在")
    _file_owned(user_id, issue["file_id"])
    if issue["status"] == "retrying":
        raise HTTPException(409, "该问题块已在处理中")
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "描述内容不能为空")
    if len(text) > 5000:
        raise HTTPException(400, "描述过长（上限 5000 字）")
    from agent.prompt_guard import detect_document_injection
    hit, pname = detect_document_injection(text)
    if hit:
        raise HTTPException(400, f"描述包含潜在指令注入（{pname}），已拒收")
    marked = issue_store.mark_retrying(issue_id)
    if marked is None:
        raise HTTPException(409, "该问题块已在处理中或已解决")
    try:
        from pgvector.psycopg import register_vector
        from ingest.embedder import embed_batch
        from ingest.indexer import EMBED_MODEL
        file_id = issue["file_id"]
        # seq 追加（max+1，不重排现有块）；并发双插用 ON CONFLICT 覆盖兜底
        max_seq = pg_store.query_one(
            "SELECT coalesce(max(seq),-1) AS m FROM rag_chunk WHERE file_id=%s",
            (file_id,))
        vec = embed_batch([text])[0]
        with pg_store.connect() as conn:
            register_vector(conn)
            conn.execute(
                """INSERT INTO rag_chunk (file_id, chunk_type, seq, content, chars,
                                          heading_path, page_no, embedding, embed_model)
                   VALUES (%s,'text',%s,%s,%s,'',%s,%s,%s)
                   ON CONFLICT (file_id, seq) DO UPDATE SET
                     content=EXCLUDED.content, chars=EXCLUDED.chars,
                     embedding=EXCLUDED.embedding, embed_model=EXCLUDED.embed_model""",
                (file_id, max_seq["m"] + 1, text, len(text),
                 issue.get("page_no"), vec, EMBED_MODEL))
        # 文件内容变化 → 语义缓存失效（BM25 为查询期全量构建，无需失效）
        from retrieval import semantic_cache
        semantic_cache.invalidate(user_id)
        issue_store.mark_resolved(issue_id, "describe")
    except Exception as e:
        issue_store.mark_failed(issue_id, f"描述写入失败: {e}")
        logger = logging.getLogger("rag.ingest_api")
        logger.exception("describe issue failed: %s", e)
        raise HTTPException(500, f"描述写入失败：{e}")
    return {"issue_id": issue_id, "status": "resolved", "action": "describe"}


# ===== 块重试任务查询（replace 场景） =====

@router.get("/jobs/{job_id}")
def job_status(job_id: int, request: Request = None):
    """查询任务进度快照。"""
    user_id = _get_user_id(request) if request else None
    job = job_store.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    if job["user_id"] != user_id:
        raise HTTPException(403, "无权访问该任务")
    return job


@router.get("/jobs")
def jobs(request: Request = None, limit: int = Query(20, le=100)):
    """当前用户的任务列表（含历史）。"""
    user_id = _get_user_id(request) if request else None
    return job_store.list_jobs(user_id, limit)


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: int, request: Request = None):
    """失败/死信任务的人工重试入口。"""
    user_id = _get_user_id(request) if request else None
    job = job_store.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    if job["user_id"] != user_id:
        raise HTTPException(403, "无权操作该任务")
    if job["status"] not in ("failed", "dead"):
        raise HTTPException(400, f"当前状态 {job['status']} 不可重试")
    updated = job_store.retry_job(job_id)
    return {"job_id": job_id, "status": updated["status"] if updated else "error"}
