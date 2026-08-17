"""
基础问答服务（Phase 2 版本）：混合检索 → 生成 → 引用标注 → 日志落库。

Phase 3 将由 LangGraph 主图接管（路由/纠错/反思），本模块保留为快路径与对照基线。
"""
import logging
import time
import uuid
from typing import Dict, Iterator

from db import pg_store
from llm.mimo_client import get_client
from llm.prompt_loader import fill
from retrieval.hybrid import hybrid_search
from retrieval import semantic_cache
from retrieval.embedder import get_embedder
from memory import memory as memory_mod
from observability.tracing import span

logger = logging.getLogger("rag.qa")

NO_ANSWER_TEXT = "根据现有文档未找到相关信息，无法回答该问题。"


def _build_context(hits: list[dict]) -> str:
    parts = []
    for i, h in enumerate(hits, 1):
        loc = f"{h['doc_name']} p.{h['page_no'] + 1}"
        parts.append(f"[{i}] ({loc})\n{h['content']}")
    return "\n\n".join(parts)


def _log_retrieval(trace_id: str, query: str, result: Dict, user_id: int | None = None):
    try:
        import json
        pg_store.execute(
            """INSERT INTO retrieval_log (user_id, trace_id, query, hit_count, top_score,
                                          low_confidence, stage_ms)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (user_id, trace_id, query, len(result["hits"]), result.get("top_score"),
             result["low_confidence"], json.dumps(result["stage_ms"])))
    except Exception as e:
        logger.warning("retrieval_log write failed: %s", e)


def _log_qa(session_id, trace_id, query, answer, chunk_ids, total_ms,
            token_in, token_out, thinking, cache_hit=False, user_id=None, route="standard"):
    try:
        pg_store.execute(
            """INSERT INTO qa_log (session_id, user_id, trace_id, query, answer, route,
                                   chunk_ids, total_ms, token_in, token_out, thinking, cache_hit)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (session_id, user_id, trace_id, query, answer, route, chunk_ids,
             total_ms, token_in, token_out, thinking, cache_hit))
    except Exception as e:
        logger.warning("qa_log write failed: %s", e)


def ask(query: str, session_id: str = "", top_k: int = 0,
        user_id: int | None = None) -> Dict:
    """同步问答（非流式）：语义缓存 → 检索 → 生成 → 引用标注 → 日志落库。"""
    with span("rag.ask", query_len=len(query)):
        return _ask_inner(query, session_id, top_k, user_id)


def _ask_inner(query: str, session_id: str, top_k: int,
               user_id: int | None = None) -> Dict:
    start = time.perf_counter()
    trace_id = uuid.uuid4().hex[:16]
    session_id = session_id or f"s-{uuid.uuid4().hex[:8]}"

    # Prompt 注入防御
    from agent.prompt_guard import sanitize_query
    guard = sanitize_query(query)
    if not guard.safe:
        total_ms = int((time.perf_counter() - start) * 1000)
        refuse_answer = "抱歉，您的查询包含潜在的指令注入，无法回答。"
        _log_qa(session_id, trace_id, query, refuse_answer, [], total_ms, 0, 0, False,
                user_id=user_id, route="guard")
        return {"answer": refuse_answer, "citations": [],
                "low_confidence": False, "refused": True,
                "guard_reason": guard.reason,
                "trace_id": trace_id, "session_id": session_id, "total_ms": total_ms}
    query = guard.query  # 使用清洗后的 query

    # Token 预算检查
    from agent.token_budget import check_budget, record_usage
    budget_ok, budget_reason = check_budget(user_id)
    if not budget_ok:
        total_ms = int((time.perf_counter() - start) * 1000)
        _log_qa(session_id, trace_id, query, budget_reason, [], total_ms, 0, 0, False,
                user_id=user_id, route="budget")
        return {"answer": budget_reason, "citations": [],
                "low_confidence": False, "refused": True,
                "budget_exceeded": True,
                "trace_id": trace_id, "session_id": session_id, "total_ms": total_ms}

    # 语义缓存（入库后自动失效）
    qvec = get_embedder().encode_query(query)
    cached = semantic_cache.lookup(qvec, user_id=user_id)
    if cached:
        return {**cached, "trace_id": trace_id, "session_id": session_id,
                "cache_hit": True,
                "total_ms": int((time.perf_counter() - start) * 1000)}

    result = hybrid_search(query, top_k=top_k, user_id=user_id)
    _log_retrieval(trace_id, query, result, user_id)

    hits = result["hits"]
    if not hits:
        total_ms = int((time.perf_counter() - start) * 1000)
        _log_qa(session_id, trace_id, query, NO_ANSWER_TEXT, [], total_ms, 0, 0, False,
                user_id=user_id)
        payload = {"answer": NO_ANSWER_TEXT, "citations": [],
                   "low_confidence": False, "refused": True}
        semantic_cache.store(query, qvec, payload, user_id=user_id)
        return {**payload, "trace_id": trace_id,
                "session_id": session_id, "total_ms": total_ms}

    # 长期记忆召回（跨会话关注点注入）
    memories = memory_mod.recall(query, user_id=user_id) if user_id else []
    mem_line = ""
    if memories:
        mem_line = "【用户历史关注】\n" + "\n".join(
            f"- {m['content']}" for m in memories) + "\n\n"
    # 低置信信号入 prompt：rerank 降级/低分时强化拒答约束（refuse 实测的修复）
    low_note = ""
    if result["low_confidence"]:
        low_note = ("【检索置信提示】以下参考资料与问题相关度较低（精排降级或低分）。"
                    "若证据不足，必须明确回答\"根据现有文档未找到相关信息\"，不得编造。\n\n")
    # 断路器检查：LLM 不可用时降级为“仅检索模式”
    from agent.circuit_breaker import get_breaker
    breaker = get_breaker()
    citations = [{"index": i + 1, "chunk_id": h["chunk_id"],
                  "doc_name": h["doc_name"], "page_no": h["page_no"] + 1,
                  "score": h["score"]} for i, h in enumerate(hits)]

    if not breaker.allow_request():
        # 降级模式：直接返回 top-3 原文片段，不调 LLM
        logger.warning("Circuit breaker OPEN: fallback to retrieval-only mode")
        raw_chunks = []
        for i, h in enumerate(hits[:3], 1):
            raw_chunks.append(f"[{i}] {h['doc_name']} p.{h['page_no'] + 1}\n{h['content']}")
        fallback_answer = ("⚠ AI 服务暂时不可用，以下是检索到的最相关原文片段：\n\n"
                           + "\n\n---\n\n".join(raw_chunks))
        total_ms = int((time.perf_counter() - start) * 1000)
        _log_qa(session_id, trace_id, query, fallback_answer,
                [h["chunk_id"] for h in hits[:3]], total_ms, 0, 0, False,
                user_id=user_id, route="fallback")
        return {"answer": fallback_answer, "citations": citations[:3],
                "low_confidence": False, "refused": False, "degraded": True,
                "trace_id": trace_id, "session_id": session_id, "total_ms": total_ms}

    # 正常 LLM 调用
    prompt = fill("generate",
                  context=low_note + mem_line + _build_context(hits), question=query)
    try:
        llm = get_client().chat(
            [{"role": "user", "content": prompt}], thinking=True, temperature=0.2)
        breaker.record_success()
    except Exception as e:
        breaker.record_failure()
        raise

    answer = llm.content.strip()
    total_ms = int((time.perf_counter() - start) * 1000)
    # 记录 Token 消耗
    record_usage(user_id, llm.token_in + llm.token_out)
    _log_qa(session_id, trace_id, query, answer,
            [h["chunk_id"] for h in hits], total_ms,
            llm.token_in, llm.token_out, True, user_id=user_id)
    if user_id:
        memory_mod.maybe_extract(session_id, user_id)
    payload = {"answer": answer, "citations": citations,
               "low_confidence": result["low_confidence"], "refused": False}
    semantic_cache.store(query, qvec, payload, user_id=user_id)
    return {**payload, "trace_id": trace_id,
            "session_id": session_id, "total_ms": total_ms,
            "stage_ms": {**result["stage_ms"], "generate": llm.elapsed_ms}}


def ask_stream(query: str, session_id: str = "", top_k: int = 0,
               user_id: int | None = None) -> Iterator[Dict]:
    """流式问答：先 yield 检索元信息，再逐块 yield 答案文本，最后 yield 落库元信息。"""
    start = time.perf_counter()
    trace_id = uuid.uuid4().hex[:16]
    session_id = session_id or f"s-{uuid.uuid4().hex[:8]}"

    # Prompt 注入防御
    from agent.prompt_guard import sanitize_query
    guard = sanitize_query(query)
    if not guard.safe:
        refuse = "抱歉，您的查询包含潜在的指令注入，无法回答。"
        yield {"type": "delta", "text": refuse}
        yield {"type": "done", "total_ms": int((time.perf_counter() - start) * 1000),
               "refused": True, "guard_reason": guard.reason}
        _log_qa(session_id, trace_id, query, refuse, [],
                int((time.perf_counter() - start) * 1000), 0, 0, False,
                user_id=user_id, route="guard")
        return
    query = guard.query

    # Token 预算检查
    from agent.token_budget import check_budget, record_usage
    budget_ok, budget_reason = check_budget(user_id)
    if not budget_ok:
        yield {"type": "delta", "text": budget_reason}
        yield {"type": "done", "total_ms": int((time.perf_counter() - start) * 1000),
               "refused": True, "budget_exceeded": True}
        _log_qa(session_id, trace_id, query, budget_reason, [],
                int((time.perf_counter() - start) * 1000), 0, 0, False,
                user_id=user_id, route="budget")
        return

    result = hybrid_search(query, top_k=top_k, user_id=user_id)
    _log_retrieval(trace_id, query, result, user_id)
    hits = result["hits"]
    citations = [{"index": i + 1, "chunk_id": h["chunk_id"],
                  "doc_name": h["doc_name"], "page_no": h["page_no"] + 1,
                  "score": h["score"]} for i, h in enumerate(hits)]
    yield {"type": "citations", "citations": citations, "trace_id": trace_id,
           "session_id": session_id, "low_confidence": result["low_confidence"]}

    if not hits:
        yield {"type": "delta", "text": NO_ANSWER_TEXT}
        yield {"type": "done", "total_ms": int((time.perf_counter() - start) * 1000),
               "refused": True}
        _log_qa(session_id, trace_id, query, NO_ANSWER_TEXT, [],
                int((time.perf_counter() - start) * 1000), 0, 0, False, user_id=user_id)
        return

    # 低置信信号入 prompt（与 ask 同步路径一致）
    low_note = ""
    if result["low_confidence"]:
        low_note = ("【检索置信提示】以下参考资料与问题相关度较低（精排降级或低分）。"
                    "若证据不足，必须明确回答\"根据现有文档未找到相关信息\"，不得编造。\n\n")
    # 断路器检查
    from agent.circuit_breaker import get_breaker
    breaker = get_breaker()
    if not breaker.allow_request():
        logger.warning("Circuit breaker OPEN (stream): fallback to retrieval-only mode")
        raw_chunks = []
        for i, h in enumerate(hits[:3], 1):
            raw_chunks.append(f"[{i}] {h['doc_name']} p.{h['page_no'] + 1}\n{h['content']}")
        fallback = ("⚠ AI 服务暂时不可用，以下是检索到的最相关原文片段：\n\n"
                    + "\n\n---\n\n".join(raw_chunks))
        yield {"type": "delta", "text": fallback}
        yield {"type": "done", "total_ms": int((time.perf_counter() - start) * 1000),
               "refused": False, "degraded": True}
        _log_qa(session_id, trace_id, query, fallback,
                [h["chunk_id"] for h in hits[:3]],
                int((time.perf_counter() - start) * 1000), 0, 0, False,
                user_id=user_id, route="fallback")
        return

    # 正常 LLM 流式调用
    prompt = fill("generate",
                  context=low_note + _build_context(hits), question=query)
    pieces = []
    try:
        for piece in get_client().stream(
                [{"role": "user", "content": prompt}], thinking=True, temperature=0.2):
            pieces.append(piece)
            yield {"type": "delta", "text": piece}
        breaker.record_success()
    except Exception as e:
        breaker.record_failure()
        raise
    answer = "".join(pieces)
    total_ms = int((time.perf_counter() - start) * 1000)
    # 流式 API 不返回精确 token 数，估算：中文约 1.3 token/字，英文约 0.75 token/词
    est_tokens_out = int(len(answer) * 1.3)
    est_tokens_in = int(len(prompt) * 1.3)
    record_usage(user_id, est_tokens_in + est_tokens_out)
    _log_qa(session_id, trace_id, query, answer,
            [h["chunk_id"] for h in hits], total_ms,
            est_tokens_in, est_tokens_out, True, user_id=user_id)
    yield {"type": "done", "total_ms": total_ms, "refused": False}
