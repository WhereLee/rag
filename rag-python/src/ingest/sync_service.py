"""
入库编排：文件 → 解析路由 → 切块 → 向量化 → 落库。

多租户模式：
- kb_document 按 file_hash 内容去重（同文件共享底层存储）
- kb_user_document 记录用户-文档映射（引用计数）
- 删除时只删映射，引用归零才清理底层数据
"""
import hashlib
import json
import logging
import time
from pathlib import Path

import numpy as np

import config
from db import pg_store
from ingest.chunker import chunk_document
from ingest.doc_parser import parse_markdown, parse_image_file, parse_docx, IMAGE_EXTS
from ingest.pdf_parser import parse_pdf
from retrieval.embedder import get_embedder
from retrieval import bm25_index

logger = logging.getLogger("rag.sync")

DOC_TYPES = {".pdf": "pdf", ".md": "markdown", ".docx": "word"}


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _parsed_cache_path(file_hash: str) -> Path:
    return config.PARSED_DIR / f"{file_hash}.json"


def _parse_file(path: Path, file_hash: str) -> tuple[list[dict], dict]:
    """解析（带盘缓存）。返回 (pages, stats)。"""
    cache = _parsed_cache_path(file_hash)
    if cache.exists():
        data = json.loads(cache.read_text(encoding="utf-8"))
        stats = data.get("stats", {})
        stats["cache_hit"] = True
        logger.info("parse cache hit: %s", path.name)
        return data["pages"], stats

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        pages, stats = parse_pdf(path)
    elif suffix == ".md":
        pages, stats = parse_markdown(path)
    elif suffix == ".docx":
        pages, stats = parse_docx(path)
    elif suffix in IMAGE_EXTS:
        pages, stats = parse_image_file(path)
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


def ingest_file(path: Path, user_id: int | None = None, user_title: str = "", replace: bool = False,
                filename_override: str = "") -> dict:
    """多租户入库流程。返回统计信息。

    同文件共享：同一 file_hash 只存一份底层数据（chunk + 向量），
    不同用户通过 kb_user_document 映射表关联。

    replace=True 时：同 filename 的已入库文档会被下线（chunk 失活、文档置 retired），
    本文件作为新文档入库（默认替换语义，与主流知识库产品一致）。
    filename_override：HTTP 上传场景传入原始文件名（临时落盘文件名为随机名）。
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    start = time.perf_counter()
    file_hash = _file_hash(path)
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
            # 检查当前用户是否已拥有该文档映射
            existing_map = pg_store.query_one(
                "SELECT id FROM kb_user_document WHERE user_id=%s AND document_id=%s",
                (user_id, doc_id))
            if existing_map:
                return {"document_id": doc_id, "status": 1,
                        "deduplicated": True, "shared": True}
            # 创建映射（引用计数 +1），不重复解析/向量化
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

    try:
        pages, stats = _parse_file(path, file_hash)
        chunks = chunk_document(title, pages)
        if not chunks:
            raise ValueError("解析结果为空（文档可能全为无法解析的内容）")

        # 向量化
        embedder = get_embedder()
        vectors = embedder.encode([c.content for c in chunks])

        # 落库
        from pgvector.psycopg import register_vector
        with pg_store.connect() as conn:
            register_vector(conn)
            for c, v in zip(chunks, vectors):
                conn.execute(
                    """INSERT INTO kb_chunk
                       (document_id, chunk_type, page_no, seq, content, chars,
                        embedding, embed_model, status, meta)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1,%s)""",
                    (doc_id, c.chunk_type, c.page_no, c.seq, c.content,
                     len(c.content), v.astype(np.float32),
                     embedder.model_dir, json.dumps(c.meta, ensure_ascii=False)))
            char_total = sum(len(p.get("text", "")) for p in pages)
            conn.execute(
                """UPDATE kb_document SET status=1, page_count=%s, char_count=%s
                   WHERE id=%s""",
                (stats.get("page_count", len(pages)), char_total, doc_id))

        # 创建用户-文档映射（仅当指定了 user_id）
        if user_id is not None:
            pg_store.execute(
                "INSERT INTO kb_user_document (user_id, document_id) VALUES (%s,%s)",
                (user_id, doc_id))

        elapsed = time.perf_counter() - start
        _after_kb_changed(user_id)
        result = {"document_id": doc_id, "status": 1, "deduplicated": False,
                  "chunks": len(chunks),
                  "chunk_types": {t: sum(1 for c in chunks if c.chunk_type == t)
                                  for t in ("text", "table", "image")},
                  "channels": stats.get("channels", {}),
                  "vlm_calls": stats.get("vlm_calls", 0),
                  "replaced_documents": retired,
                  "elapsed_s": round(elapsed, 1)}
        logger.info("ingest done: %s -> %s", path.name, result)
        return result
    except Exception as e:
        pg_store.execute("UPDATE kb_document SET status=2, error=%s WHERE id=%s",
                         (str(e)[:2000], doc_id))
        logger.error("ingest failed: %s: %s", path.name, e)
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
