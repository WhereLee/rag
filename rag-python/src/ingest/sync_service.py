"""
入库编排：文件 → 解析路由 → 切块 → 向量化 → 落库。

多租户模式：
- kb_document 按 file_hash 内容去重（同文件共享底层存储）
- kb_user_document 记录用户-文档映射（引用计数）
- 删除时只删映射，引用归零才清理底层数据

异步任务化（第一轮修复）：
- prepare_ingest / run_ingest_pipeline 拆分：worker 可分阶段执行并回报进度（progress_cb）
- ingest_file 保留为同步快路径（脚本/评估直调，行为不变）
"""
import hashlib
import json
import logging
import time
from pathlib import Path

import numpy as np

import config
from db import pg_store
from ingest.doc_parser import parse_markdown, parse_image_file, parse_docx, parse_text, IMAGE_EXTS
from ingest.pdf_parser import parse_pdf
from retrieval.embedder import get_embedder
from retrieval import bm25_index

logger = logging.getLogger("rag.sync")

DOC_TYPES = {".pdf": "pdf", ".md": "markdown", ".docx": "word", ".txt": "text"}

# 入库任务阶段（与 ingest_job.stage 同步）
STAGE_PARSING = "parsing"
STAGE_CHUNKING = "chunking"
STAGE_EMBEDDING = "embedding"
STAGE_INDEXING = "indexing"
STAGE_DONE = "done"


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _parsed_cache_path(file_hash: str) -> Path:
    return config.PARSED_DIR / f"{file_hash}.json"


def _parse_file(path: Path, file_hash: str, progress_cb=None) -> tuple[list[dict], dict]:
    """解析（带盘缓存）。返回 (pages, stats)。

    progress_cb(stage, progress, detail)：统一三参签名；
    对 PDF 的页级回调自动适配（done,total → 解析阶段进度 0.05~0.45 区间）。
    """
    cache = _parsed_cache_path(file_hash)
    if cache.exists():
        data = json.loads(cache.read_text(encoding="utf-8"))
        stats = data.get("stats", {})
        stats["cache_hit"] = True
        logger.info("parse cache hit: %s", path.name)
        return data["pages"], stats

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        if progress_cb:
            def _page_cb(done: int, total: int):
                progress_cb(STAGE_PARSING,
                            round(0.05 + 0.40 * done / total, 3),
                            {"page": done, "page_total": total})
        else:
            _page_cb = None
        pages, stats = parse_pdf(path, progress_cb=_page_cb)
    elif suffix == ".md":
        pages, stats = parse_markdown(path)
    elif suffix == ".docx":
        pages, stats = parse_docx(path)
    elif suffix in IMAGE_EXTS:
        pages, stats = parse_image_file(path)
    elif suffix == ".txt":
        pages, stats = parse_text(path)
    else:
        raise ValueError(f"不支持的文件类型: {suffix}")

    try:
        cache.write_text(json.dumps({"pages": pages, "stats": stats}, ensure_ascii=False),
                         encoding="utf-8")
    except OSError as e:
        logger.warning("parse cache write failed: %s", e)
    return pages, stats


def _after_kb_changed(user_id: int | None = None):
    """知识库内容变化后的统一收尾：BM25 重建标记 + 语义缓存失效。"""
    bm25_index.bump_version()
    from retrieval import semantic_cache
    semantic_cache.invalidate(user_id)


# ---------------------------------------------------------------- 拆分接口（异步任务用）

def prepare_ingest(path: Path, user_id: int | None = None, user_title: str = "",
                   replace: bool = False, filename_override: str = "",
                   file_hash: str | None = None) -> dict:
    """入库前置：hash / 去重快路径 / 替换下线 / 创建文档(status=0)。

    返回 dict：
      - 去重命中: {deduplicated, document_id, status, shared}
      - 新建:     {document_id, file_hash, title, filename, doc_type}
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    file_hash = file_hash or _file_hash(path)
    title = user_title or path.stem
    suffix = path.suffix.lower()
    doc_type = DOC_TYPES.get(suffix, "image" if suffix in IMAGE_EXTS else "unknown")
    filename = filename_override or path.name

    # 内容级去重：查全局是否已有同 hash 文档
    existing_doc = pg_store.query_one(
        "SELECT id, status FROM kb_document WHERE file_hash=%s AND status=1",
        (file_hash,))
    if existing_doc:
        doc_id = existing_doc["id"]
        if user_id is not None:
            existing_map = pg_store.query_one(
                "SELECT id FROM kb_user_document WHERE user_id=%s AND document_id=%s",
                (user_id, doc_id))
            if existing_map:
                return {"document_id": doc_id, "status": 1,
                        "deduplicated": True, "shared": True}
            pg_store.execute(
                "INSERT INTO kb_user_document (user_id, document_id) VALUES (%s,%s)",
                (user_id, doc_id))
        return {"document_id": doc_id, "status": 1,
                "deduplicated": True, "shared": user_id is not None,
                "note": "同文件已存在" if user_id is None else "同文件已存在，仅创建映射"}

    # 替换语义：同文件名旧文档下线（仅影响当前用户的映射）
    retired: list[int] = []
    if replace and user_id is not None:
        olds = pg_store.query(
            """SELECT d.id FROM kb_document d
               JOIN kb_user_document ud ON ud.document_id=d.id
               WHERE d.filename=%s AND d.status=1 AND ud.user_id=%s""",
            (filename, user_id))
        for row in olds:
            _retire_document_for_user(row["id"], user_id, "replaced")
            retired.append(row["id"])

    doc_id = pg_store.query_one(
        """INSERT INTO kb_document (filename, doc_type, file_hash, status)
           VALUES (%s,%s,%s,0) RETURNING id""",
        (filename, doc_type, file_hash))["id"]
    return {"document_id": doc_id, "file_hash": file_hash, "title": title,
            "filename": filename, "doc_type": doc_type, "retired": retired}


def run_ingest_pipeline(doc_id: int, file_hash: str, path: Path, title: str,
                        filename: str, user_id: int | None = None,
                        progress_cb=None) -> dict:
    """旧同步解析路径（kb_document/kb_chunk 旧表体系）。

    已由新管线取代：HTTP 上传走 ingest.worker（pipeline.parse_file + indexer.ingest，
    user_file/rag_chunk 表体系）。本函数基于旧 doc_parser + 已重构的 chunker 旧 API，
    无法继续工作；保留签名仅为旧脚本（scripts/test_multitenant.py）调用时给出明确迁移提示。
    """
    raise NotImplementedError(
        "run_ingest_pipeline 旧同步解析路径已废弃（chunker 重构后不兼容）："
        "请改用 ingest.worker 异步解析（HTTP 上传已自动走该路径）或新管线 "
        "ingest.pipeline.parse_file + ingest.indexer.ingest")


def mark_document_failed(doc_id: int, err: Exception | str):
    """失败收尾：文档置 status=2 并记录错误（供同步/异步两路径共用）。"""
    msg = str(err) if isinstance(err, Exception) else err
    pg_store.execute("UPDATE kb_document SET status=2, error=%s WHERE id=%s",
                     (msg[:2000], doc_id))


# ---------------------------------------------------------------- 同步快路径（兼容）

def ingest_file(path: Path, user_id: int | None = None, user_title: str = "",
                replace: bool = False, filename_override: str = "") -> dict:
    """同步入库（预留给脚本/评估/单测；HTTP 已改走异步任务）。行为与拆分前一致。"""
    start = time.perf_counter()
    pre = prepare_ingest(path, user_id=user_id, user_title=user_title,
                         replace=replace, filename_override=filename_override)
    if pre.get("deduplicated"):
        return {k: pre[k] for k in ("document_id", "status", "deduplicated", "shared")}
    doc_id = pre["document_id"]
    try:
        result = run_ingest_pipeline(
            doc_id, pre["file_hash"], path, pre["title"], pre["filename"],
            user_id=user_id)
        result["replaced_documents"] = pre.get("retired", [])
        result["elapsed_s"] = round(time.perf_counter() - start, 1)
        logger.info("ingest done: %s -> %s", path.name, result)
        return result
    except Exception as e:
        mark_document_failed(doc_id, e)
        logger.exception("ingest failed: %s", path.name)
        raise


def ingest_directory(dir_path: Path, user_id: int, replace: bool = False) -> list[dict]:
    """批量入库目录下所有支持的文件（递归）。"""
    dir_path = Path(dir_path)
    results = []
    files = sorted(p for p in dir_path.rglob("*")
                   if p.suffix.lower() in set(DOC_TYPES) | IMAGE_EXTS)
    for f in files:
        try:
            results.append({"file": str(f.name),
                            **ingest_file(f, user_id=user_id, replace=replace)})
        except Exception as e:
            results.append({"file": str(f.name), "error": str(e)[:200]})
    return results


def _retire_document(doc_id: int, reason: str = "replaced") -> None:
    """admin 模式：直接软删文档（chunk 失活 + 文档置 3），清理所有用户映射。"""
    with pg_store.connect() as conn:
        conn.execute("UPDATE kb_chunk SET status=0 WHERE document_id=%s AND status=1",
                     (doc_id,))
        conn.execute(
            "UPDATE kb_document SET status=3, error=%s WHERE id=%s",
            (f"retired:{reason}"[:2000], doc_id))
    pg_store.execute("DELETE FROM kb_user_document WHERE document_id=%s", (doc_id,))
    _after_kb_changed()   # 全局缓存失效
    logger.info("document %s retired (%s) [admin]", doc_id, reason)


def _retire_document_for_user(doc_id: int, user_id: int, reason: str = "replaced") -> None:
    """为用户下线文档：删除映射 + 引用计数检查。"""
    # 删除该用户的映射
    pg_store.execute(
        "DELETE FROM kb_user_document WHERE user_id=%s AND document_id=%s",
        (user_id, doc_id))
    # 引用计数：检查是否还有其他用户映射
    remaining = pg_store.query_one(
        "SELECT count(*) AS n FROM kb_user_document WHERE document_id=%s", (doc_id,))
    if remaining and remaining["n"] > 0:
        # 其他用户还在用，不清理底层数据
        _after_kb_changed(user_id)
        logger.info("document %s unmapped for user %s (%s), %d refs remain",
                    doc_id, user_id, reason, remaining["n"])
        return
    # 无引用，软删文档 + chunk 失活
    with pg_store.connect() as conn:
        conn.execute("UPDATE kb_chunk SET status=0 WHERE document_id=%s AND status=1",
                     (doc_id,))
        conn.execute(
            "UPDATE kb_document SET status=3, error=%s WHERE id=%s",
            (f"retired:{reason}"[:2000], doc_id))
    _after_kb_changed(user_id)
    logger.info("document %s retired (%s), no refs left", doc_id, reason)


def delete_document(doc_id: int, user_id: int | None = None) -> dict:
    """删除文档（多租户：只删映射，引用归零才清理底层数据）。
    user_id=None 时 admin 模式：直接软删文档，清理所有映射。
    """
    doc = pg_store.query_one("SELECT id, filename, status FROM kb_document WHERE id=%s",
                             (doc_id,))
    if not doc:
        raise ValueError(f"文档 {doc_id} 不存在")
    if user_id is not None:
        # 权限校验：用户必须拥有该文档映射
        ownership = pg_store.query_one(
            "SELECT id FROM kb_user_document WHERE user_id=%s AND document_id=%s",
            (user_id, doc_id))
        if not ownership:
            raise ValueError(f"文档 {doc_id} 不属于当前用户")
    if doc["status"] == 3:
        # 已下线的文档，清理残留映射
        if user_id is not None:
            pg_store.execute(
                "DELETE FROM kb_user_document WHERE user_id=%s AND document_id=%s",
                (user_id, doc_id))
        else:
            pg_store.execute(
                "DELETE FROM kb_user_document WHERE document_id=%s", (doc_id,))
        return {"document_id": doc_id, "already_retired": True}
    if user_id is not None:
        _retire_document_for_user(doc_id, user_id, "deleted")
    else:
        # admin 模式：直接软删
        _retire_document(doc_id, "deleted")
    return {"document_id": doc_id, "filename": doc["filename"], "deleted": True}


def document_status(doc_id: int) -> dict | None:
    return pg_store.query_one(
        """SELECT d.id, d.filename, d.doc_type, d.status, d.page_count, d.char_count,
                  d.error, d.created_at,
                  (SELECT count(*) FROM kb_chunk c WHERE c.document_id=d.id) AS chunk_count
           FROM kb_document d WHERE d.id=%s""", (doc_id,))


def list_documents(user_id: int | None = None) -> list[dict]:
    if user_id is None:
        # 全局访问（admin）：列出全部文档
        return pg_store.query(
            """SELECT d.id, d.filename, d.doc_type, d.status, d.page_count, d.char_count,
                      (SELECT count(*) FROM kb_chunk c WHERE c.document_id=d.id AND c.status=1) AS chunk_count,
                      d.created_at
               FROM kb_document d
               ORDER BY d.id""")
    return pg_store.query(
        """SELECT d.id, d.filename, d.doc_type, d.status, d.page_count, d.char_count,
                  (SELECT count(*) FROM kb_chunk c WHERE c.document_id=d.id AND c.status=1) AS chunk_count,
                  d.created_at
           FROM kb_document d
           JOIN kb_user_document ud ON ud.document_id=d.id
           WHERE ud.user_id=%s
           ORDER BY d.id""", (user_id,))