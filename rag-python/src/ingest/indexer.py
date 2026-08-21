"""入库：切块 → embedding → rag_chunk 幂等写入（与 parse_tasks 状态联动）。

一致性设计（对应 C2 计划，调研结论落地）：
- 幂等：UNIQUE(file_id, seq) + ON CONFLICT DO UPDATE——worker 崩溃重跑/消息重投不产生重复块
- 重解析：事务内 DELETE file_id 后批量 INSERT——调研第一大坑"只追加不删除"（多版本残留）
- 无部分成功窗口：切块/embedding/写入在 worker 任务链内同步完成，单点提交
- embedding 文本 = heading_path + content（标题前缀注入，调研实证的检索单点优化）
- 失败即抛：embedding 模型不可用等 → 调用方（worker）标 failed，可走重试补偿
"""
from __future__ import annotations

import logging
from typing import List

from db.pg_store import connect
from ingest.chunker import Chunk, chunk_nodes
from ingest.embedder import embed_batch
from ingest.parser.base import DocumentNode
from pgvector.psycopg import register_vector

logger = logging.getLogger("rag.indexer")

EMBED_MODEL = "bge-base-zh-v1.5-onnx-int8"


def embed_texts(chunks: List[Chunk]) -> List[str]:
    """块 → embedding 输入文本：标题路径前缀注入（空标题路径则用纯正文）。"""
    return [f"{c.heading_path} {c.content}" if c.heading_path else c.content
            for c in chunks]


def ingest(file_id: int, nodes: List[DocumentNode], progress_cb=None) -> int:
    """解析产物入库。返回块数；任何失败抛异常（由调用方决定状态），DB 无残留。

    progress_cb(stage, progress)：阶段回报（chunking→embedding→indexing），可选。
    """
    chunks = chunk_nodes(nodes)
    if not chunks:
        # 无可检索内容（空文档/纯标题）：不产生块，仍标记成功（解析本身有效）
        _record_chunk_count(file_id, 0)
        return 0
    if progress_cb:
        progress_cb("chunking", 0.45)

    texts = embed_texts(chunks)
    if progress_cb:
        progress_cb("embedding", 0.50)
    vectors = embed_batch(texts)   # 事务外先算向量（避免长事务）；失败直接抛
    if len(vectors) != len(chunks):
        raise RuntimeError(f"embedding 数量不一致: {len(vectors)} != {len(chunks)}")

    with connect() as conn:
        register_vector(conn)
        # 同一事务：删旧块 + 插新块 + 记录块数（reparse 幂等，无残留窗口）
        conn.execute("DELETE FROM rag_chunk WHERE file_id=%s", (file_id,))
        for c, vec in zip(chunks, vectors):
            conn.execute(
                "INSERT INTO rag_chunk (file_id, chunk_type, seq, content, chars, "
                "heading_path, page_no, embedding, embed_model) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (file_id, seq) DO UPDATE SET "
                "content=EXCLUDED.content, chars=EXCLUDED.chars, "
                "heading_path=EXCLUDED.heading_path, embedding=EXCLUDED.embedding, "
                "embed_model=EXCLUDED.embed_model",
                (file_id, c.chunk_type, c.seq, c.content, c.chars,
                 c.heading_path, c.page_no, vec, EMBED_MODEL))
        _record_chunk_count(file_id, len(chunks), conn)
    if progress_cb:
        progress_cb("indexing", 0.90)
    # 文件重新入库（reparse）→ 关联问答存档失效（块变了，旧答案不可信）
    try:
        with connect() as conn:
            conn.execute(
                "UPDATE qa_cache SET invalidated=TRUE "
                "WHERE user_id=(SELECT user_id FROM user_file WHERE id=%s) "
                "AND file_ids @> ARRAY[%s]::bigint[]", (file_id, file_id))
    except Exception as e:
        logger.warning("qa_cache invalidate failed file_id=%s: %s", file_id, e)
    logger.info("ingest done file_id=%s chunks=%d", file_id, len(chunks))
    return len(chunks)


def _record_chunk_count(file_id: int, count: int, conn=None) -> None:
    """记录 parse_tasks.chunk_count（复用传入连接保持同事务；无则自开）。"""
    if conn is not None:
        conn.execute("UPDATE parse_tasks SET chunk_count=%s, updated_at=now() WHERE file_id=%s",
                     (count, file_id))
        return
    with connect() as c:
        c.execute("UPDATE parse_tasks SET chunk_count=%s, updated_at=now() WHERE file_id=%s",
                  (count, file_id))
