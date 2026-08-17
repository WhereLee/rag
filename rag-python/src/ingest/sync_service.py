"""
入库编排：文件 → 解析路由 → 切块 → 向量化 → 落库。

- 按 file_hash 幂等：重复上传直接返回已有文档
- 解析产物（页级中间表示）落盘缓存 data/parsed/{hash}.json，重跑不重复调 VLM
- 进度与统计全程记录（成本基线）
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


def _retire_document(doc_id: int, reason: str = "replaced") -> None:
    """下线文档：chunk 全部失活 + 文档置 3（retired）。不物理删除，保留溯源。"""
    with pg_store.connect() as conn:
        conn.execute("UPDATE kb_chunk SET status=0 WHERE document_id=%s AND status=1",
                     (doc_id,))
        conn.execute(
            "UPDATE kb_document SET status=3, error=%s WHERE id=%s",
            (f"retired:{reason}"[:2000], doc_id))
    bm25_index.bump_version()
    from retrieval import semantic_cache
    semantic_cache.invalidate()
    logger.info("document %s retired (%s)", doc_id, reason)


def _after_kb_changed():
    """知识库内容变化后的统一收尾：BM25 重建标记 + 语义缓存失效。"""
    bm25_index.bump_version()
    from retrieval import semantic_cache
    semantic_cache.invalidate()


def ingest_file(path: Path, user_title: str = "", replace: bool = False,
                filename_override: str = "") -> dict:
    """完整入库流程。返回统计信息。

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

    # 幂等检查
    existing = pg_store.query_one("SELECT id, status FROM kb_document WHERE file_hash=%s",
                                  (file_hash,))
    if existing:
        return {"document_id": existing["id"], "status": existing["status"],
                "deduplicated": True}

    # 替换语义：同文件名旧文档下线
    retired: list[int] = []
    if replace:
        olds = pg_store.query(
            "SELECT id FROM kb_document WHERE filename=%s AND status=1", (filename,))
        for row in olds:
            _retire_document(row["id"], "replaced")
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

        elapsed = time.perf_counter() - start
        _after_kb_changed()
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


def ingest_directory(dir_path: Path, replace: bool = False) -> list[dict]:
    """批量入库目录下所有支持的文件（递归）。replace 透传给 ingest_file。"""
    dir_path = Path(dir_path)
    results = []
    files = sorted(p for p in dir_path.rglob("*")
                   if p.suffix.lower() in set(DOC_TYPES) | IMAGE_EXTS)
    for f in files:
        try:
            results.append({"file": str(f.name), **ingest_file(f, replace=replace)})
        except Exception as e:
            results.append({"file": str(f.name), "error": str(e)[:200]})
    return results


def delete_document(doc_id: int) -> dict:
    """删除文档（软删：chunk 下线 + 文档置 retired）。返回下线统计。"""
    doc = pg_store.query_one("SELECT id, filename, status FROM kb_document WHERE id=%s",
                             (doc_id,))
    if not doc:
        raise ValueError(f"文档 {doc_id} 不存在")
    if doc["status"] == 3:
        return {"document_id": doc_id, "already_retired": True}
    active = pg_store.query_one(
        "SELECT count(*) AS n FROM kb_chunk WHERE document_id=%s AND status=1", (doc_id,))
    _retire_document(doc_id, "deleted")
    return {"document_id": doc_id, "filename": doc["filename"],
            "chunks_offlined": active["n"] or 0}


def document_status(doc_id: int) -> dict | None:
    return pg_store.query_one(
        """SELECT d.id, d.filename, d.doc_type, d.status, d.page_count, d.char_count,
                  d.error, d.created_at,
                  (SELECT count(*) FROM kb_chunk c WHERE c.document_id=d.id) AS chunk_count
           FROM kb_document d WHERE d.id=%s""", (doc_id,))


def list_documents() -> list[dict]:
    return pg_store.query(
        """SELECT d.id, d.filename, d.doc_type, d.status, d.page_count, d.char_count,
                  (SELECT count(*) FROM kb_chunk c WHERE c.document_id=d.id) AS chunk_count,
                  d.created_at
           FROM kb_document d ORDER BY d.id""")
