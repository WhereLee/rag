"""
混合检索：向量(pgvector HNSW) + BM25 → RRF 融合 → Rerank 精排 → 阈值判定。

- RRF k=60
- Rerank 超时/排队超限 → 降级 RRF 排序（不阻塞问答）
- 阈值：score < RERANK_REJECT → 空结果；< RERANK_LOW → 低置信标记
- 全程阶段耗时记录（stage_ms），供 retrieval_log 落库
"""
import logging
import time
from typing import Dict, List

import numpy as np

import config
from db import pg_store
from retrieval import bm25_index
from retrieval.embedder import get_embedder
from retrieval.reranker import rerank, RerankBusyError
from observability.tracing import span

logger = logging.getLogger("rag.hybrid")

# E1 实验：向量列 ↔ 查询编码器配对（列维度必须与模型输出一致）
COLUMN_EMBEDDER = {"embedding": "bge-base-zh-v1.5-onnx-int8",
                   "embedding2": "ritrieve-zh-v1-onnx-int8"}


def _query_embedder():
    return get_embedder(COLUMN_EMBEDDER[config.VECTOR_COLUMN])


def _vector_search(qvec: np.ndarray, top_n: int,
                   exclude_types: tuple = (),
                   user_id: int | None = None) -> List[Dict]:
    col = config.VECTOR_COLUMN   # E1 实验列切换，取值已在 config 白名单校验
    type_filter = ""
    if exclude_types:
        type_filter = "AND c.chunk_type != ALL(%s)"
    # 多租户过滤：只查当前用户可见文档的 chunk
    user_filter = ""
    if user_id is not None:
        user_filter = "AND d.id IN (SELECT document_id FROM kb_user_document WHERE user_id = %s)"
    from pgvector.psycopg import register_vector
    with pg_store.connect() as conn:
        register_vector(conn)
        params = [qvec]
        if exclude_types:
            params.append(list(exclude_types))
        if user_id is not None:
            params.append(user_id)
        params += [qvec, top_n]
        cur = conn.execute(
            f"""SELECT c.id, c.content, c.chunk_type, c.page_no, c.document_id,
                      1 - (c.{col} <=> %s::vector) AS score,
                      d.filename AS doc_name
               FROM kb_chunk c JOIN kb_document d ON d.id = c.document_id
               WHERE c.status = 1 AND c.{col} IS NOT NULL {type_filter} {user_filter}
               ORDER BY c.{col} <=> %s::vector LIMIT %s""",
            tuple(params))
        return [dict(r) for r in cur.fetchall()]


def _load_chunks(chunk_ids: list[int]) -> Dict[int, dict]:
    if not chunk_ids:
        return {}
    rows = pg_store.query(
        """SELECT c.id, c.content, c.chunk_type, c.page_no, c.document_id,
                  d.filename AS doc_name
           FROM kb_chunk c JOIN kb_document d ON d.id = c.document_id
           WHERE c.id = ANY(%s)""", (chunk_ids,))
    return {r["id"]: dict(r) for r in rows}


def _rrf(rank_lists: List[List[int]], k: int) -> List[tuple[int, float]]:
    scores: Dict[int, float] = {}
    for lst in rank_lists:
        for rank, cid in enumerate(lst):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: -x[1])


def hybrid_search(query: str, top_k: int = 0, use_rerank: bool = True,
                  exclude_types: tuple = (), user_id: int | None = None) -> Dict:
    """
    返回 {hits:[{chunk_id,content,score,...}], low_confidence, stage_ms, reranked}
    exclude_types：E5 实验用，排除指定 chunk 类型（如 ("table","image")）
    user_id: 多租户过滤。传入时只检索该用户可见文档。
    """
    top_k = top_k or config.FINAL_TOP_K
    stage_ms: Dict[str, int] = {}

    with span("rag.hybrid_search", query_len=len(query), top_k=top_k,
              vector_column=config.VECTOR_COLUMN, user_id=user_id):
        return _hybrid_search_inner(query, top_k, use_rerank, stage_ms, exclude_types, user_id)


def _hybrid_search_inner(query: str, top_k: int, use_rerank: bool,
                         stage_ms: Dict[str, int],
                         exclude_types: tuple = (),
                         user_id: int | None = None) -> Dict:
    # 1. 向量路
    t0 = time.perf_counter()
    qvec = _query_embedder().encode_query(query)
    stage_ms["encode"] = int((time.perf_counter() - t0) * 1000)
    t0 = time.perf_counter()
    vec_hits = _vector_search(qvec, config.VECTOR_TOP_K, exclude_types, user_id)
    stage_ms["vector"] = int((time.perf_counter() - t0) * 1000)

    # 2. BM25 路
    t0 = time.perf_counter()
    bm_hits = bm25_index.search(query, config.BM25_TOP_K, exclude_types, user_id)
    stage_ms["bm25"] = int((time.perf_counter() - t0) * 1000)

    # 3. RRF 融合
    vec_ids = [h["id"] for h in vec_hits]
    bm_ids = [h["chunk_id"] for h in bm_hits]
    fused = _rrf([vec_ids, bm_ids], config.RRF_K)
    candidate_ids = [cid for cid, _ in fused[:max(top_k * 3, 16)]]
    chunks = _load_chunks(candidate_ids)

    # 4. Rerank 精排（可降级）
    reranked = False
    ordered: List[tuple[int, float]] = []
    if use_rerank and candidate_ids:
        t0 = time.perf_counter()
        try:
            passages = [chunks[cid]["content"] for cid in candidate_ids]
            scored = rerank(query, passages)
            ordered = [(candidate_ids[idx], s) for idx, s in scored]
            reranked = True
        except RerankBusyError:
            logger.warning("rerank busy -> fallback RRF")
            ordered = fused
        except Exception as e:
            logger.error("rerank failed -> fallback RRF: %s", e)
            ordered = fused
        stage_ms["rerank"] = int((time.perf_counter() - t0) * 1000)
    else:
        ordered = fused

    # 5. 阈值判定（仅 rerank 分数有语义）
    # 实测结论（scripts/_refuse_probe.py）：rerank 降级时 RRF 分数不可与精排阈值比较，
    # 且此前 low_confidence 恒 False，导致低相关命中全量传给生成层、拒答全靠生成层自觉。
    # 修复：降级路径保守标记 low_confidence=True，由生成层据此强化拒答约束。
    low_confidence = not reranked
    hits = []
    for cid, score in ordered[:top_k]:
        if cid not in chunks:
            continue
        if reranked and score < config.RERANK_REJECT:
            continue  # 明确不相关，剔除
        if reranked and score < config.RERANK_LOW:
            low_confidence = True
        row = chunks[cid]
        hits.append({
            "chunk_id": cid, "content": row["content"],
            "chunk_type": row["chunk_type"], "page_no": row["page_no"],
            "document_id": row["document_id"], "doc_name": row["doc_name"],
            "score": round(float(score), 4),
        })

    return {"hits": hits, "low_confidence": low_confidence,
            "stage_ms": stage_ms, "reranked": reranked,
            "top_score": hits[0]["score"] if hits else None}
