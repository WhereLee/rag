"""入库 API：上传 / 状态 / 列表。"""
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, HTTPException, Query, Body

import config
from ingest import sync_service

router = APIRouter(tags=["ingest"])


@router.post("/upload")
async def upload(file: UploadFile = File(...), replace: bool = Query(False)):
    """上传文件入库（解析+切块+向量化，同步执行）。replace=True 替换同文件名旧文档。"""
    suffix = Path(file.filename or "upload.bin").suffix.lower()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)
    try:
        result = sync_service.ingest_file(tmp_path, user_title=Path(file.filename).stem,
                                          replace=replace,
                                          filename_override=file.filename or tmp_path.name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"入库失败: {e}")
    finally:
        tmp_path.unlink(missing_ok=True)
    return result


@router.post("/ingest-path")
def ingest_path(path: str = Body(..., embed=True), replace: bool = Query(True)):
    """按本机路径入库（开发/运维用，避免大文件走 HTTP 上传）。
    兼容 body 传参：{"path": "..."}；replace=True：同文件名旧文档下线。"""
    p = Path(path)
    if not p.exists():
        raise HTTPException(404, f"路径不存在: {path}")
    try:
        if p.is_dir():
            return sync_service.ingest_directory(p, replace=replace)
        return sync_service.ingest_file(p, replace=replace)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"入库失败: {e}")


@router.delete("/documents/{doc_id}")
def delete(doc_id: int):
    """删除文档（软删：chunk 下线 + 文档置 retired）。"""
    try:
        return sync_service.delete_document(doc_id)
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
def documents():
    docs = sync_service.list_documents()
    for d in docs:
        d["created_at"] = str(d["created_at"])
    return docs


@router.get("/documents/{doc_id}/chunks")
def document_chunks(doc_id: int, limit: int = 50):
    """文档分块只读抽查（解析质量人工审查用，RAGFlow 式人工干预闭环的起点）。"""
    from db import pg_store
    doc = sync_service.document_status(doc_id)
    if not doc:
        raise HTTPException(404, "文档不存在")
    rows = pg_store.query(
        """SELECT id, chunk_type, page_no, seq, content, status
           FROM kb_chunk WHERE document_id=%s ORDER BY seq LIMIT %s""",
        (doc_id, limit))
    return {"document": doc, "chunks": rows}
