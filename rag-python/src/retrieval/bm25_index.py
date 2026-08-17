"""
BM25 关键词索引（内存）：jieba 分词 + BM25Okapi。

- 启动懒加载：首次查询时从 kb_chunk 全量构建
- 版本机制：入库后 bump_version()，下次查询自动重建
- 多租户：检索时按 user_id 过滤（通过 kb_user_document 映射）
"""
import logging
import threading
from typing import List, Dict

import jieba
from rank_bm25 import BM25Okapi

from db import pg_store

logger = logging.getLogger("rag.bm25")

_lock = threading.Lock()
_version = 0
_built_version = -1
_index: BM25Okapi | None = None
_chunk_ids: List[int] = []
_chunk_types: List[str] = []
_chunk_doc_ids: List[int] = []   # 新增：chunk_id -> document_id 映射
_corpus_size = 0


def bump_version():
    """入库/删除后调用，标记索引过期。"""
    global _version
    with _lock:
        _version += 1


def _tokenize(text: str) -> list[str]:
    return [t for t in jieba.lcut(text) if t.strip()]


def _ensure_built():
    global _index, _chunk_ids, _chunk_types, _chunk_doc_ids, _built_version, _corpus_size
    with _lock:
        if _built_version == _version and _index is not None:
            return
        rows = pg_store.query(
            """SELECT id, content, chunk_type, document_id
               FROM kb_chunk WHERE status=1 ORDER BY id""")
        if not rows:
            _index, _chunk_ids, _chunk_types, _chunk_doc_ids, _corpus_size = None, [], [], [], 0
            _built_version = _version
            return
        tokenized = [_tokenize(r["content"]) for r in rows]
        _index = BM25Okapi(tokenized)
        _chunk_ids = [r["id"] for r in rows]
        _chunk_types = [r["chunk_type"] for r in rows]
        _chunk_doc_ids = [r["document_id"] for r in rows]
        _corpus_size = len(rows)
        _built_version = _version
        logger.info("BM25 index built: %d chunks", _corpus_size)


def _get_user_doc_ids(user_id: int) -> set[int]:
    """查询用户可见的 document_id 集合。"""
    rows = pg_store.query(
        "SELECT document_id FROM kb_user_document WHERE user_id=%s", (user_id,))
    return {r["document_id"] for r in rows}


def search(query: str, top_n: int = 24, exclude_types: tuple = (),
           user_id: int | None = None) -> List[Dict]:
    """返回 [{chunk_id, score}]，score 为 BM25 原始分。

    user_id: 多租户过滤。传入时只返回该用户可见文档的 chunk。
    exclude_types: 供 E5 实验。
    """
    _ensure_built()
    if _index is None or _corpus_size == 0:
        return []

    # 多租户：预加载用户可见文档集合
    visible_doc_ids: set[int] | None = None
    if user_id is not None:
        visible_doc_ids = _get_user_doc_ids(user_id)
        if not visible_doc_ids:
            return []

    scores = _index.get_scores(_tokenize(query))
    ranked = sorted(range(len(scores)), key=lambda i: -scores[i])
    out = []
    for i in ranked:
        if len(out) >= top_n:
            break
        if scores[i] <= 0:
            continue
        if _chunk_types[i] in exclude_types:
            continue
        if visible_doc_ids is not None and _chunk_doc_ids[i] not in visible_doc_ids:
            continue
        out.append({"chunk_id": _chunk_ids[i], "score": float(scores[i])})
    return out
