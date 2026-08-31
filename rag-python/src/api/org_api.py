"""org 知识空间 API（双项目集成）：club 将活动文件推入 rag 知识库。

鉴权：主服务 InternalAuthMiddleware 全局校验 X-Internal-Key；
org 请求不携带 X-User-Id（org 语义替代个人语义，也避免触发网关签名分支）。

入库管线与 Java 网关文件域约定完全对齐（worker 零改动）：
- 物理路径：data/files/{blob.owner_user_id}/{stored_name}，org 文件以 org_id 作为
  目录分区（blob.owner_user_id=org_id，无外键，复用现成路径拼接）
- 秒传语义：file_hash 全局唯一，跨空间内容去重（隔离靠 user_file 引用，物理共享安全）
- 入队：parse_tasks（worker 轮询消费，幂等）

软删：user_file.status=0（检索 SQL 过滤 status=1，立即不可见；物理文件保留，
与个人回收站同语义，未来可加恢复）。
"""
import hashlib
import logging
import os
import uuid

from fastapi import APIRouter, Form, HTTPException, UploadFile
from pydantic import BaseModel

import config
from db import pg_store

logger = logging.getLogger("rag.org_api")

router = APIRouter(tags=["org"])

MAX_FILE_SIZE = 50 * 1024 * 1024  # 与 Java 网关文件域一致
ALLOWED_EXTS = {"txt", "md", "pdf", "docx", "xlsx", "pptx", "png", "jpg", "jpeg", "webp"}
# 注：不做魔数校验——调用方为内部服务（club 上传入口已校验），内部边界职责不重复


class DeactivateRequest(BaseModel):
    org_id: int


class RetrieveRequest(BaseModel):
    query: str
    org_id: int
    top_k: int = 8
    use_rerank: bool = True   # 部署资源紧张时调用方可关闭（降级为 RRF 原始分）


def _org_file_row(file_id: int, org_id: int) -> dict:
    """归属校验：文件必须属于该 org 空间（不存在/不属于一律 404，不泄露存在性）。"""
    row = pg_store.query_one(
        "SELECT id, filename, status FROM user_file "
        "WHERE id=%s AND owner_type='org' AND org_id=%s",
        (file_id, org_id))
    if not row:
        raise HTTPException(404, "文件不存在")
    return row


@router.post("/ingest")
async def org_ingest(file: UploadFile, org_id: int = Form(...),
                     biz_type: str = Form("")):
    """club 推送活动文件入库：落盘（原子）→ blob 去重 → user_file(org) → 入队解析。

    返回 {"file_id", "status": "parsing"}——解析异步，调用方可按需轮询 /files/{id}/status。
    """
    if org_id <= 0:
        raise HTTPException(400, "org_id 无效")
    filename = (file.filename or "").strip()
    if not filename:
        raise HTTPException(400, "文件名为空")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXTS:
        raise HTTPException(400, f"不支持的文件类型：.{ext}")

    org_dir = config.DATA_DIR / "files" / str(org_id)
    org_dir.mkdir(parents=True, exist_ok=True)
    stored_name = uuid.uuid4().hex + "." + ext
    tmp = org_dir / (stored_name + ".tmp")
    md = hashlib.sha256()
    size = 0
    try:
        with open(tmp, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_FILE_SIZE:
                    raise HTTPException(400, "文件过大（上限 50MB）")
                md.update(chunk)
                out.write(chunk)
        if size == 0:
            raise HTTPException(400, "文件内容为空")
        file_hash = md.hexdigest()
        target = org_dir / stored_name
        os.replace(tmp, target)  # 同目录原子 move（先落盘后落库，与网关同序）

        with pg_store.connect() as conn:
            # blob 内容去重（并发同 hash 兜底）；命中已有 blob 时删自己的物理副本并计数
            conn.execute(
                "INSERT INTO file_blob (file_hash, stored_name, file_size, ref_count, owner_user_id) "
                "VALUES (%s,%s,%s,1,%s) ON CONFLICT (file_hash) DO NOTHING",
                (file_hash, stored_name, size, org_id))
            blob = conn.execute(
                "SELECT id, stored_name FROM file_blob WHERE file_hash=%s",
                (file_hash,)).fetchone()
            if blob["stored_name"] != stored_name:
                target.unlink(missing_ok=True)
                conn.execute(
                    "UPDATE file_blob SET ref_count=ref_count+1 WHERE id=%s", (blob["id"],))
            row = conn.execute(
                "INSERT INTO user_file (user_id, blob_id, filename, file_size, content_type, "
                "owner_type, org_id) VALUES (NULL,%s,%s,%s,%s,'org',%s) RETURNING id",
                (blob["id"], filename, size, file.content_type or "", org_id)).fetchone()
            file_id = row["id"]
            conn.execute(
                "INSERT INTO parse_tasks (file_id) VALUES (%s) "
                "ON CONFLICT (file_id) DO NOTHING", (file_id,))
        logger.info("org ingest: org=%s file=%s size=%d biz_type=%s",
                    org_id, filename, size, biz_type or "-")
        return {"file_id": file_id, "status": "parsing"}
    except HTTPException:
        tmp.unlink(missing_ok=True)
        raise
    except Exception as e:
        tmp.unlink(missing_ok=True)
        logger.exception("org ingest failed: org=%s name=%s", org_id, filename)
        raise HTTPException(500, "文件入库失败，请稍后重试") from e


@router.post("/files/{file_id}/deactivate")
def org_deactivate(file_id: int, body: DeactivateRequest):
    """软删：校验归属后置 status=0（检索立即不可见；物理文件保留）。幂等。"""
    row = _org_file_row(file_id, body.org_id)
    if row["status"] != 0:
        pg_store.execute(
            "UPDATE user_file SET status=0, deleted_at=now() WHERE id=%s", (file_id,))
        logger.info("org file deactivated: org=%s file=%s", body.org_id, file_id)
    return {"ok": True}


@router.get("/files/{file_id}/status")
def org_file_status(file_id: int, org_id: int):
    """解析状态查询（club 侧 rag_status 对账用）：无解析记录 → queued。"""
    _org_file_row(file_id, org_id)
    row = pg_store.query_one(
        "SELECT status, error FROM parse_tasks WHERE file_id=%s", (file_id,))
    if not row:
        return {"status": "queued", "error": None}
    return {"status": row["status"], "error": row["error"] or None}


@router.post("/retrieve")
def org_retrieve(body: RetrieveRequest):
    """纯检索（检索器模式，不接生成）：返回 org 空间内混合检索命中的 chunks。

    返回字段含 filename/page_no/heading_path，供调用方（概念 Agent）标注引用来源；
    score 语义同 retriever：rerank 时为 logits（跨查询可比），关闭时为 RRF 原始分。"""
    if body.org_id <= 0 or not body.query.strip():
        raise HTTPException(400, "org_id/query 无效")
    top_k = max(1, min(body.top_k, 20))
    from retrieval.retriever import retrieve
    chunks = retrieve(None, body.query.strip(), top_k=top_k,
                      use_rerank=body.use_rerank, org_id=body.org_id)
    return {
        "items": [
            {
                "file_id": c.file_id,
                "filename": c.filename,
                "chunk_type": c.chunk_type,
                "page_no": c.page_no,
                "heading_path": c.heading_path,
                "content": c.content,
                "score": round(c.score, 4),
            }
            for c in chunks
        ],
        "total": len(chunks),
    }
