"""混合检索（新链路 rag_chunk）：向量召回 + BM25 召回 → RRF 融合 → rerank 精排。

设计（对应 C3 计划，调研结论落地）：
- 权限/软删过滤：JOIN user_file WHERE user_id=? AND status=1——块表不物理删，查询期过滤
  （规避 HNSW 高频删除 tombstone/碎片问题，软删文件永不可见）
- BM25：jieba 分词 + rank_bm25（k1=1.5, b=0.75），文档文本 = heading_path + content
  （标题词参与精确匹配，调研实践：对标题加权提升专有名词召回）
- RRF k=60（Cormack 2009 经验值）：排名倒数融合，绕开向量分 [-1,1] 与 BM25 分无界的尺度不可比
- rerank：bge-reranker-v2-m3 对融合候选打分 → top_k；异常/排队超时降级 RRF 并标记 downgraded
  （调研标准做法：rerank 不可用时保留融合排序，不阻塞链路）
- 全库 BM25 每次查询构建（当前规模千级块毫秒级）；块数上万后换 PG tsvector（演进项）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

from db.pg_store import connect
from pgvector.psycopg import register_vector
from retrieval.embedder import get_embedder

import config

logger = logging.getLogger("rag.retriever")

VECTOR_TOP_N = 50     # 向量召回候选数
BM25_TOP_N = 50       # BM25 召回候选数
BM25_STRONG_TOP = 5   # BM25 强命中保护窗口（rerank 低分时按关键词命中保留）
RRF_K = 60            # 倒数排名融合常数（原论文经验值）
RERANK_TOP_N = 20     # 参与精排的候选数（黄金集对比实验：50→20 质量零损失，延迟降 3.3 倍）
DEFAULT_TOP_K = 5     # 最终返回块数

# 查询停用词（保护判定的内容词提取用）：高频虚词/疑问词不构成“关键词命中”证据
QUERY_STOPWORDS = {
    "的", "了", "是", "在", "和", "与", "或", "用", "为", "有", "个", "中", "里",
    "什么", "怎么", "如何", "多少", "哪些", "哪个", "是否", "吗", "呢", "啊", "吧",
    "文件", "内容", "里面", "这个", "那个", "一个", "请", "我", "你", "它", "要", "可以",
}


@dataclass
class RetrievedChunk:
    """检索结果块（含溯源信息，问答层拼引用）。
    score 语义：rerank 时是 cross-encoder logits 绝对分（跨查询可比，规范 4.2 以 -5 为剔除线）；
    降级时是 RRF 原始分（无 logits，调用方不做阈值拒答，仅标记低置信）。"""

    chunk_id: int
    file_id: int
    filename: str
    chunk_type: str
    content: str
    heading_path: str
    page_no: Optional[int]
    score: float
    reranked: bool = False   # False = 降级（RRF 原始分）


def _vector_search(user_id: int, query: str, top_n: int = VECTOR_TOP_N,
                   dir_id: Optional[int] = None) -> List[dict]:
    """向量召回：HNSW 余弦近邻，用户+软删过滤；dir_id 非空时限定目录（目录级对话/检索）。"""
    qvec = get_embedder().encode_query(query)
    sql = (
        "SELECT c.id, c.file_id, uf.filename, c.chunk_type, c.content, "
        "c.heading_path, c.page_no, 1 - (c.embedding <=> %s::vector) AS sim "
        "FROM rag_chunk c JOIN user_file uf ON uf.id = c.file_id "
        "WHERE uf.user_id=%s AND uf.status=1 AND c.embedding IS NOT NULL ")
    params: list = [qvec, user_id]
    if dir_id is not None:
        sql += "AND uf.dir_id=%s "
        params.append(dir_id)
    sql += "ORDER BY c.embedding <=> %s::vector LIMIT %s"
    params += [qvec, top_n]
    with connect() as conn:
        register_vector(conn)
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _bm25_search(user_id: int, query: str, top_n: int = BM25_TOP_N,
                 dir_id: Optional[int] = None) -> List[dict]:
    """BM25 召回：全库块（同过滤）jieba 分词打分。文档文本含标题路径（标题词加权）。"""
    import jieba
    from rank_bm25 import BM25Okapi

    sql = (
        "SELECT c.id, c.file_id, uf.filename, c.chunk_type, c.content, "
        "c.heading_path, c.page_no "
        "FROM rag_chunk c JOIN user_file uf ON uf.id = c.file_id "
        "WHERE uf.user_id=%s AND uf.status=1")
    params: list = [user_id]
    if dir_id is not None:
        sql += " AND uf.dir_id=%s"
        params.append(dir_id)
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    if not rows:
        return []
    docs = [f"{r['heading_path']} {r['content']}".strip() for r in rows]
    tok_docs = [jieba.lcut(d) for d in docs]
    scores = BM25Okapi(tok_docs, k1=1.5, b=0.75).get_scores(jieba.lcut(query))
    order = np.argsort(-scores)[:top_n]
    out = []
    for i in order:
        if scores[i] <= 0:
            break
        r = dict(rows[i])
        r["bm25"] = float(scores[i])
        out.append(r)
    return out


def _rrf(vec_rows: List[dict], bm25_rows: List[dict], k: int = RRF_K) -> List[dict]:
    """倒数排名融合：score(d) = Σ 1/(k + rank)。仅用排名，分数尺度无关。"""
    scores: dict[int, float] = {}
    for rank, r in enumerate(vec_rows):
        scores[r["id"]] = scores.get(r["id"], 0.0) + 1.0 / (k + rank + 1)
    for rank, r in enumerate(bm25_rows):
        scores[r["id"]] = scores.get(r["id"], 0.0) + 1.0 / (k + rank + 1)
    by_id = {r["id"]: r for r in vec_rows + bm25_rows}
    order = sorted(scores, key=scores.get, reverse=True)
    return [by_id[cid] for cid in order]


def retrieve(user_id: int, query: str, top_k: int = DEFAULT_TOP_K,
             use_rerank: bool = True, dir_id: Optional[int] = None) -> List[RetrievedChunk]:
    """混合检索完整链路：向量 + BM25 → RRF 融合 → rerank 精排（异常降级）。
    dir_id 非空时限定目录（目录级对话的检索范围）。"""
    vec_rows = _vector_search(user_id, query, dir_id=dir_id)
    bm25_rows = _bm25_search(user_id, query, dir_id=dir_id)
    fused = _rrf(vec_rows, bm25_rows)[:RERANK_TOP_N]
    if not fused:
        return []

    if not use_rerank:
        return [_to_chunk(r, r.get("bm25", 0.0), reranked=False) for r in fused[:top_k]]

    from retrieval.reranker import rerank
    try:
        passages = [f"{r['heading_path']} {r['content']}".strip() for r in fused]
        order = rerank(query, passages)   # [(原下标, logits)] 降序
        # 分数用 logits 绝对分（跨查询可比，可设拒答阈值）；softmax 是相对概率不可比
        # 规范 §4.2：rerank 分数低于剔除线（-5）判为不相关，逐块剔除（与旧链路 hybrid.py 行为对齐）；
        # 全部剔除时返回空列表 → qa 层走拒答路径（§4.3 无答案明确拒答）
        # BM25 强命中保护：字面关键词命中是强相关信号（如"缓存"命中含"缓存：Redis"的块），
        # 避免 rerank logits 对部分短查询打分偏低时误杀——降级为 RRF/bm25 分保留（reranked=False），
        # qa 层按低置信处理而非拒答（真实场景："缓存用什么技术" 块 logits=-6.17 < -5 但 BM25=3.82）
        # 保护前提：查询内容词真实出现在块中（"文件/是什么/多少"等高频虚词命中不算）。
        # 内容词提取：英文/数字/下划线整体 token（不随 jieba 拆分，专有名词如 P2_UI_MARK_8842 保持完整，
        # 避免噪声子串如 MARK 命中无关块的 E2E_MARK）+ 中文词（jieba 细粒度，过滤停用词）
        import re
        import jieba
        terms: set[str] = set(re.findall(r"[A-Za-z0-9_]{2,}", query))
        terms.update(t for t in jieba.lcut(query)
                     if re.fullmatch(r"[\u4e00-\u9fff]{2,}", t) and t not in QUERY_STOPWORDS)
        bm25_strong = {
            r["id"] for r in bm25_rows[:BM25_STRONG_TOP]
            if r.get("bm25", 0) > 0
            and any(t.lower() in f"{r['heading_path']} {r['content']}".lower() for t in terms)}
        results = []
        for idx, logit in order:
            if len(results) >= top_k:
                break
            r = fused[idx]
            if logit < config.RERANK_REJECT:
                if r["id"] not in bm25_strong:
                    continue
                results.append(_to_chunk(r, float(r.get("bm25", 0.0)), reranked=False))
                continue
            results.append(_to_chunk(r, float(logit), reranked=True))
        return results
    except Exception as e:
        # 模型不可用/排队超时 → 降级 RRF 原始排序（不阻塞问答链路）
        logger.warning("rerank 降级 RRF: %s", e)
        return [_to_chunk(r, r.get("bm25", 0.0), reranked=False) for r in fused[:top_k]]


def _to_chunk(r: dict, score: float, reranked: bool) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=r["id"], file_id=r["file_id"], filename=r["filename"],
        chunk_type=r["chunk_type"], content=r["content"],
        heading_path=r["heading_path"], page_no=r["page_no"],
        score=score, reranked=reranked)
