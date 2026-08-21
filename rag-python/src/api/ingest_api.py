"""
入库 API：提交（异步任务）/ 任务状态 / 文档列表 / 删除。多租户版本。

第一轮修复（异步任务化）：
- POST /upload 校验文件后立即返回 202 {job_id}，重活交给 ingest.worker 后台执行
- 新增 jobs 系列接口：进度查询 / 任务列表 / 失败重试
- 文件持久化到 data/jobs/{doc_id}{suffix} 供 worker 读取（临时文件不再随请求删除）
"""
import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, HTTPException, Query, Body, Request
from fastapi.responses import JSONResponse

import config
from ingest import sync_service, job_store

router = APIRouter(tags=["ingest"])

# ===== 安全配置 =====
ALLOWED_SUFFIXES = {".pdf", ".md", ".docx", ".png", ".jpg", ".jpeg", ".txt"}
MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50MB
# 文件名清洗：仅保留字母、数字、中文、点、下划线、连字符
SAFE_FILENAME_RE = re.compile(r"[^\w.\u4e00-\u9fff-]")


def _sanitize_filename(raw: str) -> str:
    """清洗文件名：移除路径穿越字符和危险字符。"""
    # 只取文件名部分（防止 ../../etc/passwd）
    name = Path(raw).name if raw else "upload.bin"
    # 移除危险字符
    safe = SAFE_FILENAME_RE.sub("_", name)
    # 防止纯点号或空名
    if not safe or safe == "." or safe == "..":
        safe = "upload.bin"
    return safe


def _get_user_id(request: Request) -> int:
    """从请求头 X-User-Id 提取用户 ID（多租户模式必备，由网关注入）。"""
    uid = request.headers.get("X-User-Id")
    if uid is None:
        raise HTTPException(400, "缺少用户身份 X-User-Id")
    try:
        return int(uid)
    except ValueError:
        raise HTTPException(400, "X-User-Id 必须是整数")


def _file_hash_from_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@router.post("/upload")
async def upload(file: UploadFile = File(...), replace: bool = Query(False),
                 request: Request = None):
    """上传文件入库（异步任务：提交即 202，worker 后台执行解析+切块+向量化）。

    去重快路径：同文件且当前用户已有映射 → 同步返回 200 {deduplicated}。
    replace=True 替换同文件名旧文档。
    """
    user_id = _get_user_id(request) if request else None

    # 安全校验 1：文件类型白名单
    raw_name = file.filename or "upload.bin"
    suffix = Path(raw_name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(400, f"不支持的文件类型: {suffix}，允许: {', '.join(sorted(ALLOWED_SUFFIXES))}")

    # 安全校验 2：文件大小限制（50MB）
    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(413, f"文件过大（{len(content) // 1024 // 1024}MB），上限 50MB")

    # 安全校验 3：文件名清洗
    safe_name = _sanitize_filename(raw_name)

    # 安全校验 4：magic bytes 内容嗅探（防伪装扩展名）
    from ingest.magic import assert_content_type
    assert_content_type(content[:32], suffix, safe_name)

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)

        file_hash = _file_hash_from_bytes(content)
        pre = sync_service.prepare_ingest(
            tmp_path, user_id=user_id, user_title=Path(safe_name).stem,
            replace=replace, filename_override=safe_name, file_hash=file_hash)
        if pre.get("deduplicated"):
            return {k: pre[k] for k in ("document_id", "status", "deduplicated",
                                        "shared", "note") if k in pre}

        # 持久化文件到 jobs 目录（worker 读取，不随请求删除）
        jobs_dir = config.DATA_DIR / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        target = jobs_dir / f"{pre['document_id']}{suffix}"
        shutil.move(str(tmp_path), str(target))
        tmp_path = None  # 已 move，不再清理

        trace_id = getattr(request.state, "request_id", "") if request else ""
        job_key = hashlib.sha256(
            f"{user_id}:{file_hash}:{int(replace)}".encode()).hexdigest()
        jobinfo = job_store.create_job(
            job_key, user_id, safe_name, pre["doc_type"], str(target),
            pre["document_id"], file_hash, trace_id)
        return JSONResponse(
            {"job_id": jobinfo["id"], "status": "queued",
             "document_id": pre["document_id"], "filename": safe_name},
            status_code=202)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"入库失败：{e}")
    finally:
        # 未 move 的临时文件（异常路径）清理；Windows 文件锁延迟重试
        if tmp_path is not None and tmp_path.exists():
            import time
            for attempt in range(3):
                try:
                    tmp_path.unlink(missing_ok=True)
                    break
                except PermissionError:
                    if attempt < 2:
                        time.sleep(0.1)
                    else:
                        import logging
                        logging.getLogger("rag.upload").warning(
                            f"无法删除临时文件 {tmp_path}，文件被占用")


# ===== 任务查询（异步化配套） =====

@router.get("/jobs/{job_id}/issues")
def job_issues(job_id: int, request: Request = None):
    """失败块清单（第三轮：用户自决恢复入门）。"""
    from ingest import issue_store
    user_id = _get_user_id(request) if request else None
    job = job_store.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    if job["user_id"] != user_id:
        raise HTTPException(403, "无权访问该任务")
    return issue_store.list_issues(job_id)


@router.post("/issues/{issue_id}/retry")
def retry_issue(issue_id: int, request: Request = None):
    """提交单块重试（创建 block_retry job，202 异步）。"""
    from ingest import issue_store
    user_id = _get_user_id(request) if request else None
    issue = issue_store.get_issue(issue_id)
    if not issue:
        raise HTTPException(404, "问题块不存在")
    parent = job_store.get_job(issue["job_id"])
    if not parent or parent["user_id"] != user_id:
        raise HTTPException(403, "无权操作该问题块")
    marked = issue_store.mark_retrying(issue_id)
    if marked is None:
        raise HTTPException(409, "该问题块已在处理中或已解决")
    job_key = f"retry-issue-{issue_id}"
    jobinfo = job_store.create_job(
        job_key, user_id, parent["filename"], parent["doc_type"], parent["file_path"],
        parent["document_id"], parent["file_hash"], "",
        job_type="block_retry", issue_id=issue_id)
    return JSONResponse({"job_id": jobinfo["id"], "status": "queued"},
                        status_code=202)


@router.post("/issues/{issue_id}/replace")
async def replace_issue(issue_id: int, request: Request = None,
                        file: UploadFile = File(...)):
    """上传替代图（源图损坏时用户自备原图，多模态识别，202 异步）。"""
    from ingest import issue_store
    user_id = _get_user_id(request) if request else None
    issue = issue_store.get_issue(issue_id)
    if not issue:
        raise HTTPException(404, "问题块不存在")
    parent = job_store.get_job(issue["job_id"])
    if not parent or parent["user_id"] != user_id:
        raise HTTPException(403, "无权操作该问题块")
    if getattr(issue, "status", "pending") not in ("pending", "failed"):
        raise HTTPException(409, "该问题块当前不可替换")
    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(413, "文件过大")
    import hashlib
    import shutil
    alt_dir = Path(config.DATA_DIR) / "issue_alts"
    alt_dir.mkdir(parents=True, exist_ok=True)
    alt_path = alt_dir / f"issue_{issue_id}.png"
    alt_path.write_bytes(content)
    # 写 alt 图地址进 issue resolution（供 worker retry 分支读取）
    from db import pg_store
    pg_store.execute("UPDATE issue_items SET resolution=%s, updated_at=NOW() WHERE id=%s",
                     (f"replace:{alt_path}", issue_id))
    marked = issue_store.mark_retrying(issue_id)
    if marked is None:
        raise HTTPException(409, "该问题块已在处理中")
    job_key = f"retry-issue-{issue_id}"
    jobinfo = job_store.create_job(
        job_key, user_id, parent["filename"], parent["doc_type"], parent["file_path"],
        parent["document_id"], parent["file_hash"], "",
        job_type="block_retry", issue_id=issue_id)
    return JSONResponse({"job_id": jobinfo["id"], "status": "queued"},
                        status_code=202)


@router.post("/issues/{issue_id}/describe")
def describe_issue(issue_id: int, body: dict = Body(...), request: Request = None):
    """文字描述兜底（同步：注入检测+长度限制，直接入 chunk）。"""
    from ingest import issue_store
    from db import pg_store
    user_id = _get_user_id(request) if request else None
    issue = issue_store.get_issue(issue_id)
    if not issue:
        raise HTTPException(404, "问题块不存在")
    parent = job_store.get_job(issue["job_id"])
    if not parent or parent["user_id"] != user_id:
        raise HTTPException(403, "无权操作该问题块")
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
    # append chunk（seq 最大+1，不重排现有 chunk）
    max_seq = pg_store.query_one(
        "SELECT coalesce(max(seq),-1) AS m FROM kb_chunk WHERE document_id=%s",
        (issue["document_id"],))
    pg_store.execute(
        """INSERT INTO kb_chunk (document_id, chunk_type, page_no, seq, content, chars, status, meta)
           VALUES (%s,%s,%s,%s,%s,%s,1,%s)""",
        (issue["document_id"], "text", issue["page_no"], (max_seq["m"] + 1),
         text, len(text), json.dumps({"source": "user_describe", "issue_id": issue_id})))
    # BM25 失效 + 语义缓存失效
    from retrieval import bm25_index, semantic_cache
    bm25_index.bump_version()
    semantic_cache.invalidate(user_id)
    issue_store.mark_resolved(issue_id, "describe")
    return {"issue_id": issue_id, "status": "resolved", "action": "describe"}

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


@router.post("/ingest-path")
def ingest_path(path: str = Body(..., embed=True), replace: bool = Query(True),
                request: Request = None):
    """多租户模式下禁用：允许读取服务器任意文件，存在安全风险。
    请使用 /upload 接口上传文件。"""
    raise HTTPException(403, "多租户模式下 ingest-path 已禁用，请使用 /upload 上传文件")


@router.delete("/documents/{doc_id}")
def delete(doc_id: int, request: Request = None):
    """删除文档（多租户：只删映射，引用归零才清理底层数据）。"""
    user_id = _get_user_id(request) if request else None
    try:
        return sync_service.delete_document(doc_id, user_id=user_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.get("/status/{doc_id}")
def status(doc_id: int):
    doc = sync_service.document_status(doc_id)
    if not doc:
        raise HTTPException(404, "文档不存在")
    doc["created_at"] = str(doc["created_at"])
    return doc


@router.get("/documents")
def documents(request: Request = None):
    """列出当前用户的文档（多租户过滤）。"""
    user_id = _get_user_id(request) if request else None
    docs = sync_service.list_documents(user_id=user_id)
    for d in docs:
        d["created_at"] = str(d["created_at"])
    return docs


@router.get("/documents/{doc_id}/chunks")
def document_chunks(doc_id: int, limit: int = 50, request: Request = None):
    """文档分块只读抽查（解析质量人工审查用）。"""
    from db import pg_store
    user_id = _get_user_id(request) if request else None
    # 权限校验：用户必须拥有该文档
    if user_id is not None:
        ownership = pg_store.query_one(
            "SELECT id FROM kb_user_document WHERE user_id=%s AND document_id=%s",
            (user_id, doc_id))
        if not ownership:
            raise HTTPException(403, "无权访问该文档")
    doc = sync_service.document_status(doc_id)
    if not doc:
        raise HTTPException(404, "文档不存在")
    rows = pg_store.query(
        """SELECT id, chunk_type, page_no, seq, content, status
           FROM kb_chunk WHERE document_id=%s ORDER BY seq LIMIT %s""",
        (doc_id, limit))
    return {"document": doc, "chunks": rows}