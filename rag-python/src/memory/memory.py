"""
记忆系统：会话记忆（qa_log 滑动窗口）+ 长期记忆（语义召回）。

长期记忆提炼：每 5 轮由 LLM 从对话中抽取
- focus：用户持续关注的文档/主题
- open_question：尚未解决的疑问
问答时按语义相似度召回 top3 注入提示词，实现"跨会话记得用户在探讨什么"。
"""
import json
import logging

from db import pg_store
from llm.mimo_client import get_client, LLMError
from llm.prompt_loader import get_prompt
from retrieval.embedder import get_embedder

logger = logging.getLogger("rag.memory")

EXTRACT_EVERY = 5
RECALL_TOP = 3

EXTRACT_PROMPT = """从以下对话中提炼用户的长期关注点，输出 JSON：
{"focus": ["<持续关注的主题/文档，最多3条>"], "open_questions": ["<尚未解决的疑问，最多2条，没有则空数组>"]}
只提炼跨会话有价值的信息，不要记录一次性细节。"""


def get_history(session_id: str, limit: int = 10) -> list[dict]:
    """会话记忆：最近 N 轮问答。"""
    rows = pg_store.query(
        """SELECT query, answer FROM qa_log
           WHERE session_id=%s ORDER BY id DESC LIMIT %s""", (session_id, limit))
    history = []
    for r in reversed(rows):
        history.append({"role": "user", "content": r["query"]})
        history.append({"role": "assistant", "content": r["answer"][:800]})
    return history


def session_turn_count(session_id: str) -> int:
    r = pg_store.query_one(
        "SELECT count(*) AS n FROM qa_log WHERE session_id=%s", (session_id,))
    return r["n"] if r else 0


def extract_and_store(session_id: str, user_id: str = "default"):
    """从会话中提炼长期记忆并入库（幂等：同 session 每 EXTRACT_EVERY 轮一次）。"""
    history = get_history(session_id, limit=12)
    if len(history) < 4:
        return 0
    text = "\n".join(f"{h['role']}: {h['content'][:300]}" for h in history)
    try:
        obj = get_client().chat_json(
            [{"role": "user", "content": EXTRACT_PROMPT + "\n\n【对话】\n" + text}],
            thinking=False, max_tokens=1024)
    except LLMError as e:
        logger.warning("memory extract failed: %s", e)
        return 0

    items = []
    for f in (obj.get("focus") or [])[:3]:
        if isinstance(f, str) and f.strip():
            items.append(("focus", f.strip()))
    for q in (obj.get("open_questions") or [])[:2]:
        if isinstance(q, str) and q.strip():
            items.append(("open_question", q.strip()))
    if not items:
        return 0

    emb = get_embedder()
    vecs = emb.encode([c for _, c in items])
    from pgvector.psycopg import register_vector
    with pg_store.connect() as conn:
        register_vector(conn)
        # 去重：同用户同内容不重复插
        for (mem_type, content), v in zip(items, vecs):
            exists = conn.execute(
                "SELECT id FROM memory_entry WHERE user_id=%s AND content=%s",
                (user_id, content)).fetchone()
            if exists:
                continue
            conn.execute(
                """INSERT INTO memory_entry (user_id, mem_type, content, embedding)
                   VALUES (%s,%s,%s,%s)""",
                (user_id, mem_type, content, v))
    logger.info("memory stored: session=%s items=%d", session_id, len(items))
    return len(items)


def recall(query: str, user_id: str = "default", top: int = RECALL_TOP) -> list[dict]:
    """语义召回相关长期记忆（pgvector ANN 检索，替代全表拉取 + Python 余弦）。"""
    qvec = get_embedder().encode_query(query)
    from pgvector.psycopg import register_vector
    with pg_store.connect() as conn:
        register_vector(conn)
        # cosine 0.4 阈值对应 cosine 距离 0.6（pgvector <=> 为余弦距离）
        cur = conn.execute(
            """SELECT mem_type, content, 1 - (embedding <=> %s::vector) AS sim
               FROM memory_entry
               WHERE user_id=%s AND embedding IS NOT NULL
                 AND (embedding <=> %s::vector) < 0.6
               ORDER BY embedding <=> %s::vector LIMIT %s""",
            (qvec, user_id, qvec, qvec, top))
        return [{"mem_type": r["mem_type"], "content": r["content"],
                 "sim": round(float(r["sim"]), 3)} for r in cur.fetchall()]


def maybe_extract(session_id: str):
    """轮次触发器：每 EXTRACT_EVERY 轮提炼一次。"""
    n = session_turn_count(session_id)
    if n > 0 and n % EXTRACT_EVERY == 0:
        extract_and_store(session_id)
