"""入库 API：上传 / 状态 / 列表。多租户版本。"""
import re
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, HTTPException, Query, Body, Request

import config
from ingest import sync_service

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


def _get_user_id(request: Request) -> int | None:
    """从请求头 X-User-Id 提取用户 ID。缺失时返回 None。"""
    uid = request.headers.get("X-User-Id")
    if uid is None:
        return None
    try:
        return int(uid)
    except ValueError:
        raise HTTPException(400, "X-User-Id 必须是整数")


@router.post("/upload")
async def upload(file: UploadFile = File(...), replace: bool = Query(False),
                 request: Request = None):
    """上传文件入库（解析+切块+向量化，同步执行）。replace=True 替换同文件名旧文档。"""
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

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    try:
        result = sync_service.ingest_file(tmp_path, user_id=user_id,
                                          user_title=Path(safe_name).stem,
                                          replace=replace,
                                          filename_override=safe_name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, "入库失败，请联系管理员")
    finally:
        tmp_path.unlink(missing_ok=True)
    return result


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
