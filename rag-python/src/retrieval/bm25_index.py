"""
BM25 关键词索引（内存）：jieba 分词 + BM25Okapi。

- 启动懒加载：首次查询时从 kb_chunk 全量构建
- 版本机制：入库后 bump_version()，下次查询自动重建
- 文档量 <100 时全量重建成本可接受（留档说明）
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
_corpus_size = 0


def bump_version():
    """入库/删除后调用，标记索引过期。"""
    global _version
    with _lock:
        _version += 1


def _tokenize(text: str) -> list[str]:
    return [t for t in jieba.lcut(text) if t.strip()]


def _ensure_built():
    global _index, _chunk_ids, _chunk_types, _built_version, _corpus_size
    with _lock:
        if _built_version == _version and _index is not None:
            return
        rows = pg_store.query(
            """SELECT id, content, chunk_type FROM kb_chunk WHERE status=1 ORDER BY id""")
        if not rows:
            _index, _chunk_ids, _chunk_types, _corpus_size = None, [], [], 0
            _built_version = _version
            return
        tokenized = [_tokenize(r["content"]) for r in rows]
        _index = BM25Okapi(tokenized)
        _chunk_ids = [r["id"] for r in rows]
        _chunk_types = [r["chunk_type"] for r in rows]
        _corpus_size = len(rows)
        _built_version = _version
        logger.info("BM25 index built: %d chunks", _corpus_size)


def search(query: str, top_n: int = 24, exclude_types: tuple = ()) -> List[Dict]:
    """返回 [{chunk_id, score}]，score 为 BM25 原始分。exclude_types 供 E5 实验。"""
    _ensure_built()
    if _index is None or _corpus_size == 0:
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
        out.append({"chunk_id": _chunk_ids[i], "score": float(scores[i])})
    return out
