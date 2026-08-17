"""
LangGraph 主图：Agentic RAG 问答流程。

结构（架构文档 §5.1）：
  START → route
    ├─ out_of_scope → guard → END
    ├─ simple       → retrieve → generate(关思考) → END
    ├─ standard     → retrieve → grade ─足→ generate(开思考) → END
    │                            └不足→ rewrite+retrieve（≤2 轮）→ no_answer
    └─ complex      → decompose → retrieve(多查询) → grade → generate → reflect(≤1 次修正) → END

设计取舍：
- 子查询检索在 retrieve 节点内串行执行（v1 求确定性；LangGraph Send 并行留作优化项）
- 每节点记录 stage 耗时与 token，落 qa_log.route 可观测实际走的档位
"""
import logging
import time
import uuid

from langgraph.graph import StateGraph, START, END

import config
from agent.state import AgentState
from llm.mimo_client import get_client, LLMError
from llm.prompt_loader import fill
from retrieval.hybrid import hybrid_search
from observability.tracing import span

logger = logging.getLogger("rag.graph")

MAX_RERETRIEVE = 2
REFLECT_THRESHOLD = 0.6

# 实验开关（E3 思考档位 / E4 反思开关）：None/False 不干预默认行为
experiment_flags = {"force_thinking": None, "disable_reflect": False}


def _stage(state: AgentState, name: str, t0: float, **extra):
    stages = list(state.get("stages") or [])
    stages.append({"stage": name, "ms": int((time.perf_counter() - t0) * 1000),
                   **extra})
    return stages


def _fmt_history(history: list) -> str:
    if not history:
        return "（无）"
    return "\n".join(f"{h['role']}: {h['content'][:200]}" for h in history[-6:])


def _build_context(hits: list) -> str:
    parts = []
    for i, h in enumerate(hits, 1):
        parts.append(f"[{i}] ({h['doc_name']} p.{h['page_no'] + 1})\n{h['content']}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------- 节点

def _route_prior(query: str) -> str:
    """轻量检索先验：top1 片段（不精排），供路由参考知识库内容。
    失败不阻塞（降级为空先验）。"""
    try:
        r = hybrid_search(query, top_k=1, use_rerank=False)
        if r["hits"]:
            h = r["hits"][0]
            return f"({h['doc_name']} p.{h['page_no'] + 1}) {h['content'][:200]}"
        return "（无命中）"
    except Exception as e:
        logger.warning("route prior failed: %s", e)
        return "（先验不可用）"


def route_node(state: AgentState) -> dict:
    t0 = time.perf_counter()
    try:
        obj = get_client().chat_json(
            [{"role": "user", "content": fill(
                "route", prior=_route_prior(state["query"]),
                history=_fmt_history(state.get("history") or []),
                question=state["query"])}],
            thinking=False, max_tokens=1024)
        route = obj.get("route", "standard")
        if route not in ("simple", "standard", "complex", "out_of_scope"):
            route = "standard"
    except LLMError as e:
        logger.warning("route failed -> standard: %s", e)
        route = "standard"
    return {"route": route,
            "search_queries": [state["query"]],
            "stages": _stage(state, "route", t0, route=route)}


def decompose_node(state: AgentState) -> dict:
    t0 = time.perf_counter()
    try:
        obj = get_client().chat_json(
            [{"role": "user", "content": fill("decompose", question=state["query"])}],
            thinking=False, max_tokens=1024)
        subs = [s for s in obj.get("sub_queries", []) if isinstance(s, str) and s.strip()]
        if not subs:
            subs = [state["query"]]
        subs = subs[:3]
    except LLMError:
        subs = [state["query"]]
    return {"search_queries": subs,
            "stages": _stage(state, "decompose", t0, n=len(subs))}


def _retrieve_multi(original_query: str, sub_queries: list[str]) -> tuple[dict[int, dict], bool]:
    """多子查询检索：并行粗筛（无精排，避免 reranker 信号量排队雪崩）
    → 合并候选 → 以原问题单次精排。返回 (hits_by_id, low_conf)。

    与原串行实现相比：rerank 从 N 次降为 1 次（省 CPU），粗筛并行缩短墙钟时间。
    降级口径：rerank 失败/超时 → RRF 顺序 + low_conf=True（诚实标记，生成层强化拒答）。
    """
    from concurrent.futures import ThreadPoolExecutor
    from retrieval.reranker import rerank, RerankBusyError

    all_hits: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=min(3, len(sub_queries))) as pool:
        results = list(pool.map(lambda q: hybrid_search(q, use_rerank=False), sub_queries))
    for result in results:
        for h in result["hits"]:
            cid = h["chunk_id"]
            if cid not in all_hits:
                all_hits[cid] = h
    # RRF 分数量纲一致，可直接合并排序；取 top VECTOR_TOP_K 精排
    merged = sorted(all_hits.values(), key=lambda x: -x["score"])[:config.VECTOR_TOP_K]
    if not merged:
        return {}, True
    ordered = merged
    rerank_ok = False
    try:
        scored = rerank(original_query, [h["content"] for h in merged])
        reranked_hits = []
        for idx, s in scored:
            h = dict(merged[idx])
            h["score"] = float(s)
            reranked_hits.append(h)
        ordered = reranked_hits
        rerank_ok = True
    except RerankBusyError:
        logger.warning("multi-retrieve rerank busy -> RRF order (low confidence)")
    except Exception as e:
        logger.error("multi-retrieve rerank failed -> RRF order: %s", e)
    out: dict[int, dict] = {}
    for h in ordered:
        out[h["chunk_id"]] = h
    # low_conf 基于最终保留的 top_k 判定（与 hybrid_search 口径一致）
    top_hits = sorted(out.values(), key=lambda x: -x["score"])[:config.FINAL_TOP_K]
    low_conf = (not rerank_ok) or any(
        h["score"] < config.RERANK_LOW for h in top_hits)
    return out, low_conf


def retrieve_node(state: AgentState) -> dict:
    t0 = time.perf_counter()
    queries = state.get("search_queries") or [state["query"]]
    all_hits: dict[int, dict] = {}
    low_conf = False
    if len(queries) > 1:
        # 多子查询：并行粗筛 + 单次精排（complex 档主路径）
        all_hits, low_conf = _retrieve_multi(state["query"], queries)
    else:
        result = hybrid_search(queries[0])
        low_conf = result["low_confidence"]
        for h in result["hits"]:
            all_hits[h["chunk_id"]] = h
    hits = sorted(all_hits.values(), key=lambda x: -x["score"])[:config.FINAL_TOP_K]
    return {"hits": hits, "low_confidence": low_conf,
            "retrieval_round": state.get("retrieval_round", 0) + 1,
            "stages": _stage(state, "retrieve", t0,
                             queries=len(queries),
                             hits=len(hits))}


def grade_node(state: AgentState) -> dict:
    """CRAG 式检索质量评估（simple 档跳过，直接视为充分）。"""
    if state.get("route") == "simple":
        return {"grade": {"sufficient": True, "missing": ""}}
    t0 = time.perf_counter()
    if not state.get("hits"):
        return {"grade": {"sufficient": False, "missing": "无检索结果"},
                "stages": _stage(state, "grade", t0)}
    try:
        obj = get_client().chat_json(
            [{"role": "user", "content": fill(
                "grade", question=state["query"],
                context=_build_context(state["hits"][:5]))}],
            thinking=False, max_tokens=1024)
        grade = {"sufficient": bool(obj.get("sufficient", True)),
                 "missing": obj.get("missing", "")}
    except LLMError:
        grade = {"sufficient": True, "missing": ""}  # 评估失败不阻塞
    return {"grade": grade, "stages": _stage(state, "grade", t0, **grade)}


def rewrite_node(state: AgentState) -> dict:
    """CRAG 纠错：结合缺失信息改写查询。"""
    t0 = time.perf_counter()
    missing = (state.get("grade") or {}).get("missing", "")
    try:
        r = get_client().chat(
            [{"role": "user", "content": fill(
                "rewrite", history=_fmt_history(state.get("history") or []),
                question=f"{state['query']}（此前检索缺少：{missing}）")}],
            thinking=False, max_tokens=1024)
        new_q = r.content.strip() or state["query"]
    except LLMError:
        new_q = state["query"] + " " + missing
    return {"search_queries": [new_q],
            "stages": _stage(state, "rewrite", t0, q=new_q[:80])}


def generate_node(state: AgentState) -> dict:
    t0 = time.perf_counter()
    thinking = state.get("route") != "simple"   # 思考档位路由
    if experiment_flags["force_thinking"] is not None:
        thinking = experiment_flags["force_thinking"]   # E3 实验覆盖
    # 低置信信号入 prompt：rerank 降级/低分时强化拒答约束（与 qa_service 一致）
    low_note = ""
    if state.get("low_confidence"):
        low_note = ("【检索置信提示】以下参考资料与问题相关度较低（精排降级或低分）。"
                    "若证据不足，必须明确回答\"根据现有文档未找到相关信息\"，不得编造。\n\n")
    prompt = fill("generate",
                  context=low_note + _build_context(state["hits"]),
                  question=state["query"])
    r = get_client().chat([{"role": "user", "content": prompt}],
                          thinking=thinking, temperature=0.2)
    return {"answer": r.content.strip(),
            "token_in": state.get("token_in", 0) + r.token_in,
            "token_out": state.get("token_out", 0) + r.token_out,
            "stages": _stage(state, "generate", t0, thinking=thinking)}


def reflect_node(state: AgentState) -> dict:
    t0 = time.perf_counter()
    try:
        obj = get_client().chat_json(
            [{"role": "user", "content": fill(
                "reflect", question=state["query"],
                context=_build_context(state["hits"][:5]),
                answer=state["answer"])}],
            thinking=False, max_tokens=1024)
        refl = {"faithfulness": float(obj.get("faithfulness", 1)),
                "relevancy": float(obj.get("relevancy", 1)),
                "passed": bool(obj.get("passed", True)),
                "feedback": obj.get("feedback", "")}
    except (LLMError, ValueError, TypeError):
        refl = {"faithfulness": 1.0, "relevancy": 1.0, "passed": True,
                "feedback": "reflect 失败，默认通过"}
    retry = state.get("reflect_retry", 0) + (0 if refl["passed"] else 1)
    return {"reflection": refl, "reflect_retry": retry,
            "stages": _stage(state, "reflect", t0, passed=refl["passed"])}


def guard_node(state: AgentState) -> dict:
    answer = ("这个问题超出了当前知识库的范围。我负责解答与已入库文档相关的问题，"
              "请围绕文档内容提问。")
    return {"answer": answer, "final_route": "guard"}


def no_answer_node(state: AgentState) -> dict:
    answer = ("根据现有文档未找到相关信息，无法回答该问题。"
              + (f"（检索评估显示缺少：{(state.get('grade') or {}).get('missing', '')[:100]}）"
                 if (state.get("grade") or {}).get("missing") else ""))
    return {"answer": answer, "final_route": "no_answer"}


# ---------------------------------------------------------------- 路由函数

def route_by_type(state: AgentState) -> str:
    return {"out_of_scope": "guard", "simple": "retrieve",
            "standard": "retrieve", "complex": "decompose"}[state["route"]]


def after_grade(state: AgentState) -> str:
    if (state.get("grade") or {}).get("sufficient", True):
        return "generate"
    if state.get("retrieval_round", 0) > MAX_RERETRIEVE:
        return "no_answer"
    return "rewrite"


def after_reflect(state: AgentState) -> str:
    refl = state.get("reflection") or {}
    if not refl.get("passed", True) and state.get("reflect_retry", 0) <= 1:
        return "generate"   # 修正重生成一次（reflect_retry 已在 reflect 节点递增）
    return END


# ---------------------------------------------------------------- 图构建

_graph = None
_eval_graph = None
_ckpt_conn = None


def _build_graph() -> StateGraph:
    """构建图结构（不含 checkpointer），供 get_graph/get_eval_graph 复用。"""
    g = StateGraph(AgentState)
    g.add_node("route", route_node)
    g.add_node("decompose", decompose_node)
    g.add_node("retrieve", retrieve_node)
    g.add_node("grade", grade_node)
    g.add_node("rewrite", rewrite_node)
    g.add_node("generate", generate_node)
    g.add_node("reflect", reflect_node)
    g.add_node("guard", guard_node)
    g.add_node("no_answer", no_answer_node)

    g.add_edge(START, "route")
    g.add_conditional_edges("route", route_by_type,
                            {"guard": "guard", "retrieve": "retrieve",
                             "decompose": "decompose"})
    g.add_edge("decompose", "retrieve")
    g.add_edge("retrieve", "grade")
    g.add_conditional_edges("grade", after_grade,
                            {"generate": "generate", "rewrite": "rewrite",
                             "no_answer": "no_answer"})
    g.add_edge("rewrite", "retrieve")
    # simple/standard 直接出；complex 走反思（E4 实验可全局关闭）
    g.add_conditional_edges(
        "generate",
        lambda s: ("reflect" if s.get("route") == "complex"
                   and not experiment_flags["disable_reflect"] else END),
        {"reflect": "reflect", END: END})
    g.add_conditional_edges("reflect", after_reflect, {"generate": "generate", END: END})
    g.add_edge("guard", END)
    g.add_edge("no_answer", END)
    return g


def get_eval_graph():
    """评估专用图：无 checkpointer，线程安全，供并发评估使用。"""
    global _eval_graph
    if _eval_graph is None:
        _eval_graph = _build_graph().compile()
        logger.info("eval graph compiled (no checkpointer)")
    return _eval_graph


def get_graph():
    global _graph
    if _graph is not None:
        return _graph

    g = _build_graph()

    # Checkpointer：PostgreSQL 持久化（HITL 中断恢复依赖）
    # 专用长连接（autocommit），与业务连接池隔离，避免事务交叉
    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver
    global _ckpt_conn
    _ckpt_conn = psycopg.connect(config.PG_DSN, autocommit=True)
    saver = PostgresSaver(_ckpt_conn)
    saver.setup()
    _graph = g.compile(checkpointer=saver)
    logger.info("agent graph compiled with PostgresSaver")
    return _graph


def run_agent(query: str, session_id: str = "", history: list = None) -> dict:
    """执行主图，返回 {answer, citations, meta}。"""
    with span("rag.agent_run", query_len=len(query)):
        return _run_agent_inner(query, session_id, history)


def run_agent_eval(query: str) -> dict:
    """评估专用：无 checkpointer，线程安全，不写 qa_log。"""
    graph = get_eval_graph()
    trace_id = uuid.uuid4().hex[:16]
    t0 = time.perf_counter()
    init = {"messages": [], "query": query, "session_id": f"eval-{trace_id}",
            "history": [], "route": "", "search_queries": [],
            "retrieval_round": 0, "hits": [], "low_confidence": False,
            "grade": {}, "answer": "", "reflection": {}, "reflect_retry": 0,
            "trace_id": trace_id, "stages": [], "token_in": 0, "token_out": 0,
            "final_route": ""}
    final = graph.invoke(init)
    total_ms = int((time.perf_counter() - t0) * 1000)
    hits = final.get("hits") or []
    citations = [{"index": i + 1, "chunk_id": h["chunk_id"],
                  "doc_name": h["doc_name"], "page_no": h["page_no"] + 1,
                  "score": h["score"]} for i, h in enumerate(hits)]
    route = final.get("route", "standard")
    final_route = final.get("final_route") or (
        "guard" if route == "out_of_scope" else route)
    return {"answer": final.get("answer", ""), "citations": citations,
            "trace_id": trace_id, "session_id": f"eval-{trace_id}",
            "route": route, "final_route": final_route,
            "low_confidence": final.get("low_confidence", False),
            "reflection": final.get("reflection") or {},
            "stages": final.get("stages") or [],
            "retrieval_round": final.get("retrieval_round", 0),
            "total_ms": total_ms,
            "refused": final_route in ("guard", "no_answer")}


def _run_agent_inner(query: str, session_id: str, history: list) -> dict:
    graph = get_graph()
    session_id = session_id or f"s-{uuid.uuid4().hex[:8]}"
    trace_id = uuid.uuid4().hex[:16]
    t0 = time.perf_counter()
    init = {"messages": [], "query": query, "session_id": session_id,
            "history": history or [], "route": "", "search_queries": [],
            "retrieval_round": 0, "hits": [], "low_confidence": False,
            "grade": {}, "answer": "", "reflection": {}, "reflect_retry": 0,
            "trace_id": trace_id, "stages": [], "token_in": 0, "token_out": 0,
            "final_route": ""}
    final = graph.invoke(init, config={"configurable": {"thread_id": session_id}})

    total_ms = int((time.perf_counter() - t0) * 1000)
    hits = final.get("hits") or []
    citations = [{"index": i + 1, "chunk_id": h["chunk_id"],
                  "doc_name": h["doc_name"], "page_no": h["page_no"] + 1,
                  "score": h["score"]} for i, h in enumerate(hits)]
    route = final.get("route", "standard")
    final_route = final.get("final_route") or (
        "guard" if route == "out_of_scope" else route)

    # 落 qa_log（route 记录实际档位）
    try:
        from db import pg_store
        pg_store.execute(
            """INSERT INTO qa_log (session_id, trace_id, query, answer, route, chunk_ids,
                                   total_ms, token_in, token_out, thinking, cache_hit)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE)""",
            (session_id, trace_id, query, final.get("answer", ""),
             final_route, [h["chunk_id"] for h in hits], total_ms,
             final.get("token_in", 0), final.get("token_out", 0),
             route != "simple"))
        import json as _json
        pg_store.execute(
            """INSERT INTO retrieval_log (trace_id, query, hit_count, top_score,
                                          low_confidence, stage_ms)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (trace_id, query, len(hits),
             hits[0]["score"] if hits else None, final.get("low_confidence", False),
             _json.dumps({s["stage"]: s["ms"] for s in final.get("stages") or []})))
    except Exception as e:
        logger.warning("agent log failed: %s", e)

    return {"answer": final.get("answer", ""), "citations": citations,
            "trace_id": trace_id, "session_id": session_id,
            "route": route, "final_route": final_route,
            "low_confidence": final.get("low_confidence", False),
            "reflection": final.get("reflection") or {},
            "stages": final.get("stages") or [],
            "retrieval_round": final.get("retrieval_round", 0),
            "total_ms": total_ms,
            "refused": final_route in ("guard", "no_answer")}
