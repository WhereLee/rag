"""问答服务（新链路）：检索 → 上下文组装 → MiMo 流式生成 → SSE。

- 独立进程端口 8091（与旧 api 8090 隔离）；用户身份由 Java 网关注入 X-User-Id（不信任前端）
- 会话体系（目录-对话-记忆）：会话可绑定目录（限定检索范围）；会话历史拼入上下文（多轮追问）；
  每 5 轮触发长期记忆抽取（memory.maybe_extract）；回答落 qa_log
- 检索：retriever 混合检索 top5（user_id 维度隔离 + 软删过滤 + 可选目录限定）
- 拒答：无结果 或 精排 logits 低于规范阈值（LT-S 001-2026 §4.2：低于 -5 判不相关）；
  降级结果（rerank 不可用，无 logits）不拒答，仅 meta 标记低置信
- 上下文：≤4000 token（中文 ≈0.6 token/字 粗估，无本地 tokenizer 的软约束）；历史对话单独预算
- SSE 事件：meta（引用/拒答）→ delta（正文逐块）→ done；异常 → error 事件
"""
import asyncio
import json
import sys
import time
import uuid
import hashlib
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import config
from db import pg_store
from llm.mimo_client import MiMoClient
from retrieval.retriever import retrieve

app = FastAPI(title="rag-qa")

REJECT_LOGITS = config.RERANK_REJECT      # 规范 §4.2 剔除线（默认 -5.0），C5 用黄金集标定
MAX_CONTEXT_TOKENS = 4000                 # 上下文预算（软约束）
MAX_HISTORY_TOKENS = 1000                 # 历史对话预算（多轮注入，避免挤占资料段）
HISTORY_LIMIT = 6                         # 最多注入的历史轮数

SYSTEM_PROMPT = (
    "你是企业知识库问答助手。你只能依据【资料】中的内容回答，每个关键事实后必须用"
    "[来源: 文件名 第X页] 标注出处（文件没有页码则只标文件名）。"
    "如果资料中没有答案，必须明确回复「资料中未找到相关内容」，禁止编造或使用自身知识作答。"
    "回答使用中文，简洁准确。"
)

# Agent 分级路由：轻量规则预筛（零 LLM 成本）。命中 → Agent 图（多轮检索+评估+反思），
# 未命中 → 直筒 RAG。依据：P0 成本实测（simple 44s / complex 213s，大头是 LLM 多轮调用），
# 分级 = 把 Agent 代价花在真正复杂的问题上。
AGENT_KEYWORDS = ("对比", "差异", "区别", "比较", "总结", "分析", "分别", "哪些文档", "关系", "影响")
AGENT_LEN_THRESHOLD = 40


def _agent_route(query: str) -> bool:
    """Agent 路由判定：关键词命中或超长问题 → 走 Agent 图。"""
    if len(query) >= AGENT_LEN_THRESHOLD:
        return True
    return any(k in query for k in AGENT_KEYWORDS)


class AskRequest(BaseModel):
    query: str
    session_id: str = ""
    thinking: bool = True   # 前端开关：关闭时跳过深度推理（省 reasoning token，忠实度略降）


class CreateSessionRequest(BaseModel):
    dir_id: int | None = None
    summary: str = ""


def _normalize_query(query: str) -> str:
    """查询归一化：去首尾空白 + 全角标点转半角 + 压缩空白（精确命中前的规范化）。"""
    q = query.strip()
    table = str.maketrans({
        "\u3000": " ", "\uff0c": ",", "\u3002": ".", "\uff01": "!", "\uff1f": "?",
        "\uff1b": ";", "\uff1a": ":", "\uff08": "(", "\uff09": ")",
        "\u3010": "[", "\u3011": "]", "\u201c": "\"", "\u201d": "\"",
        "\u2018": "'", "\u2019": "'",
    })
    q = re.sub(r"\s+", " ", q.translate(table)).strip()
    # 尾部标点（。？！）与疑问语气语义等价，去掉提高精确命中率（不误伤句中标点）
    return re.sub(r"[。！？?!.]+$", "", q)


def _query_hash(query: str) -> str:
    return hashlib.md5(query.encode("utf-8")).hexdigest()


def _estimate_tokens(text: str) -> int:
    """中文 ≈0.6 token/字，英文/数字 ≈0.25 token/字（无本地 tokenizer 的粗估）。"""
    cn = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return int(cn * 0.6 + (len(text) - cn) * 0.25)


def _build_context(chunks, max_tokens: int = MAX_CONTEXT_TOKENS):
    """检索块 → 上下文文本（含来源与标题路径），超预算截断。返回 (文本, 估算 token)。"""
    parts, used = [], 0
    for c in chunks:
        path = f"（{c.heading_path}）" if c.heading_path else ""
        page = f" 第{c.page_no}页" if c.page_no else ""
        piece = f"[来源: {c.filename}{page}]{path}\n{c.content}"
        t = _estimate_tokens(piece)
        if used + t > max_tokens and parts:
            break
        parts.append(piece)
        used += t
    return "\n\n---\n\n".join(parts), used


def _build_history(history, max_tokens: int = MAX_HISTORY_TOKENS):
    """历史问答（memory.get_history 的 role/content 序列）→ 注入文本，超预算截断。
    保留最近轮次；返回 (文本, 估算 token)。"""
    pairs = []
    for i in range(0, len(history) - 1, 2):   # 成对：user + assistant
        u, a = history[i], history[i + 1]
        pairs.append((u["content"], a["content"]))
    parts, used = [], 0
    for q, a in pairs[-HISTORY_LIMIT:]:
        piece = f"问：{q[:200]}\n答：{a[:300]}"
        t = _estimate_tokens(piece)
        if used + t > max_tokens and parts:
            break
        parts.append(piece)
        used += t
    if not parts:
        return "", 0
    return "\n\n".join(parts), used


def _session_or_403(session_id: str, user_id: int) -> dict:
    """会话归属校验：不存在或不属于当前用户 → 403；返回会话行。"""
    row = pg_store.query_one("SELECT id, user_id, dir_id FROM qa_session WHERE id=%s", (session_id,))
    if not row:
        raise HTTPException(403, "会话不存在或无权访问")
    if row["user_id"] != user_id:
        raise HTTPException(403, "会话不存在或无权访问")
    return row


def _prepare_ask(req: AskRequest, user_id: int) -> dict:
    """问答准备阶段（同步阻塞密集，整体放线程池执行，避免阻塞事件循环）。

    包含：会话校验/目录范围、L1 精确缓存、跨用户复用（L1 shared）、混合检索
    （embedding + BM25 + RRF + rerank，实测 rerank top50 ≈ 3.4s）、拒答判定、
    L2 语义缓存参考、长期记忆召回、会话历史。缓存命中时提前返回（不检索不调 LLM）。
    """
    query = (req.query or "").strip()

    # 会话归属校验 + 检索范围（会话绑定目录则限定）
    dir_id = None
    if req.session_id:
        sess = _session_or_403(req.session_id, user_id)
        dir_id = sess["dir_id"]

    # 问答存档 L1：精确命中直接返回（不检索不调 LLM）；拒答不存档
    q_hash = _query_hash(_normalize_query(query))
    cache = pg_store.query_one(
        "SELECT id, answer FROM qa_cache "
        "WHERE user_id=%s AND query_hash=%s AND NOT invalidated", (user_id, q_hash))

    # 跨用户精确复用（L1 shared）：其他用户对相同内容文件（秒传同 blob）问过相同问题 → 直接复用。
    # 安全边界：缓存引用的每个文件，当前用户都必须持有同 blob 文件（回答完全基于双方共有内容才
    # 复用，防止经缓存泄露对方私有文件）；复用后写入当前用户自己的缓存（file_ids 映射为本用户文件，
    # 后续重解析/软删按本人维度失效），并记录来源 cache id（cache_shared_from）供诊断审计。
    shared_cache = None
    if not cache:
        shared = pg_store.query_one(
            """SELECT qc.id, qc.answer, qc.query_embedding, qc.file_ids FROM qa_cache qc
               WHERE qc.query_hash=%s AND NOT qc.invalidated AND qc.user_id <> %s
                 AND qc.file_ids IS NOT NULL
                 AND NOT EXISTS (
                   SELECT 1 FROM user_file uf1
                   WHERE uf1.id = ANY(qc.file_ids)
                     AND uf1.blob_id NOT IN (
                       SELECT DISTINCT blob_id FROM user_file WHERE user_id=%s AND status=1)
                 )
               ORDER BY qc.hit_count DESC, qc.updated_at DESC LIMIT 1""",
            (q_hash, user_id, user_id))
        if shared:
            try:
                # A 的 file_ids → 当前用户同 blob 文件映射（失效维度归本人）
                b_rows = pg_store.query(
                    """SELECT DISTINCT b.id AS file_id FROM user_file a
                       JOIN user_file b ON b.blob_id=a.blob_id AND b.user_id=%s AND b.status=1
                       WHERE a.id = ANY(%s) AND a.status=1""",
                    (user_id, list(shared["file_ids"])))
                b_ids = sorted(r["file_id"] for r in b_rows)
                pg_store.execute(
                    "UPDATE qa_cache SET hit_count=hit_count+1, updated_at=now() WHERE id=%s",
                    (shared["id"],))
                if b_ids:
                    from pgvector.psycopg import register_vector
                    with pg_store.connect() as conn:
                        register_vector(conn)
                        conn.execute(
                            """INSERT INTO qa_cache (user_id, query_hash, query, query_embedding,
                                                     answer, chunk_ids, file_ids, cache_shared_from)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                               ON CONFLICT (user_id, query_hash) DO UPDATE SET
                                 answer=EXCLUDED.answer, chunk_ids=EXCLUDED.chunk_ids,
                                 file_ids=EXCLUDED.file_ids, invalidated=FALSE, updated_at=now()""",
                            (user_id, q_hash, query, shared["query_embedding"], shared["answer"],
                             [], b_ids, shared["id"]))
                shared_cache = shared["answer"]
            except Exception as e:
                print(f"[qa_cache] shared write failed: {e}", file=sys.stderr)
                shared_cache = shared["answer"]

    if cache or shared_cache:
        return {"cached": True,
                "answer": (cache or {}).get("answer") or shared_cache,
                "is_shared": cache is None,
                "chunks": [], "rejected": False, "reject_reason": "",
                "cache_ref": None, "qvec": None,
                "memories": [], "history": [], "hist_text": "", "hist_tokens": 0}

    # Agent 分级：规则预筛命中 → Agent 图（多轮检索/评估/反思，run_agent 内部落 qa_log）。
    # 异常降级直筒（Agent 失败不阻塞问答）。
    if _agent_route(query):
        try:
            from agent.main_graph import run_agent
            ar = run_agent(query, session_id=req.session_id or "", user_id=user_id)
            return {"cached": False, "agent_mode": True,
                    "answer": ar.get("answer", ""),
                    "citations": ar.get("citations") or [],
                    "rejected": bool(ar.get("refused")),
                    "reject_reason": f"agent:{ar.get('final_route') or ar.get('route', '')}",
                    "agent_route": ar.get("final_route") or ar.get("route", ""),
                    "chunks": [], "cache_ref": None, "qvec": None,
                    "memories": [], "history": [], "hist_text": "", "hist_tokens": 0,
                    "q_hash": q_hash}
        except Exception as e:
            print(f"[agent] run failed, fallback to direct: {e}", file=sys.stderr)

    chunks = retrieve(user_id, query, top_k=5, dir_id=dir_id)

    # 拒答判定：无检索结果，或精排 logits 低于规范阈值（跨查询可比）
    rejected = False
    reject_reason = ""
    if not chunks:
        rejected, reject_reason = True, "检索无结果"
    elif chunks[0].reranked and chunks[0].score < REJECT_LOGITS:
        rejected, reject_reason = True, f"检索置信度低（{chunks[0].score:.2f} < {REJECT_LOGITS}）"

    # 问答存档 L2：精确未命中后语义检索历史存档（同用户、未失效、sim>=0.9 top1），
    # 命中则作为 few-shot 参考注入 prompt（仅非拒答路径需要，避免无谓的 embedding 调用）
    cache_ref = None
    qvec = None
    if not rejected:
        try:
            from pgvector.psycopg import register_vector
            from retrieval.embedder import get_embedder
            qvec = get_embedder().encode_query(query)
            with pg_store.connect() as conn:
                register_vector(conn)
                row = conn.execute(
                    """SELECT id, query, answer, 1 - (query_embedding <=> %s::vector) AS sim
                       FROM qa_cache
                       WHERE user_id=%s AND NOT invalidated AND query_embedding IS NOT NULL
                         AND (query_embedding <=> %s::vector) < 0.1
                       ORDER BY query_embedding <=> %s::vector LIMIT 1""",
                    (qvec, user_id, qvec, qvec)).fetchone()
            if row and float(row["sim"]) >= 0.9:
                cache_ref = {"query": row["query"], "answer": row["answer"]}
            if not cache_ref:
                # 跨用户语义参考：其他用户对同内容文件（同 blob）问过语义近似问题 → 注入参考段
                # （同样受同 blob 边界约束，防止经参考注入泄露对方私有文件内容）
                row2 = conn.execute(
                    """SELECT qc.query, qc.answer, 1 - (qc.query_embedding <=> %s::vector) AS sim
                       FROM qa_cache qc
                       WHERE qc.user_id <> %s AND NOT qc.invalidated
                         AND qc.query_embedding IS NOT NULL AND qc.file_ids IS NOT NULL
                         AND (qc.query_embedding <=> %s::vector) < 0.1
                         AND NOT EXISTS (
                           SELECT 1 FROM user_file uf1
                           WHERE uf1.id = ANY(qc.file_ids)
                             AND uf1.blob_id NOT IN (
                               SELECT DISTINCT blob_id FROM user_file WHERE user_id=%s AND status=1)
                         )
                       ORDER BY qc.query_embedding <=> %s::vector LIMIT 1""",
                    (qvec, user_id, qvec, user_id, qvec)).fetchone()
                if row2 and float(row2["sim"]) >= 0.9:
                    cache_ref = {"query": row2["query"], "answer": row2["answer"]}
        except Exception as e:
            print(f"[qa_cache] L2 lookup failed: {e}", file=sys.stderr)

    # 长期记忆召回：跨会话记得用户关注的文档/未解疑问（旧链路已验证的注入方式）
    memories = []
    if not rejected:
        try:
            from memory import memory as mem
            memories = mem.recall(query, user_id, top=3)
        except Exception as e:
            print(f"[memory] recall failed: {e}", file=sys.stderr)

    history = []
    hist_text, hist_tokens = "", 0
    if req.session_id:
        from memory import memory as mem
        history = mem.get_history(req.session_id, limit=HISTORY_LIMIT)
        hist_text, hist_tokens = _build_history(history)

    return {"cached": False, "answer": "", "is_shared": False, "chunks": chunks,
            "rejected": rejected, "reject_reason": reject_reason,
            "cache_ref": cache_ref, "qvec": qvec,
            "memories": memories, "history": history,
            "hist_text": hist_text, "hist_tokens": hist_tokens,
            "q_hash": q_hash}


@app.post("/qa/sessions")
async def create_session(req: CreateSessionRequest, x_user_id: str = Header(default="")):
    if not x_user_id:
        raise HTTPException(401, "缺少 X-User-Id")
    try:
        user_id = int(x_user_id)
    except ValueError:
        raise HTTPException(400, "X-User-Id 非法")
    if req.dir_id is not None:
        d = pg_store.query_one(
            "SELECT id FROM user_dir WHERE id=%s AND user_id=%s", (req.dir_id, user_id))
        if not d:
            raise HTTPException(404, "目录不存在")
    sid = uuid.uuid4().hex
    pg_store.execute(
        "INSERT INTO qa_session (id, user_id, dir_id, summary) VALUES (%s,%s,%s,%s)",
        (sid, user_id, req.dir_id, (req.summary or "")[:200]))
    return {"session_id": sid, "dir_id": req.dir_id, "summary": req.summary or ""}


@app.get("/qa/sessions")
async def list_sessions(dir_id: int | None = None, x_user_id: str = Header(default="")):
    if not x_user_id:
        raise HTTPException(401, "缺少 X-User-Id")
    try:
        user_id = int(x_user_id)
    except ValueError:
        raise HTTPException(400, "X-User-Id 非法")
    if dir_id is not None:
        d = pg_store.query_one(
            "SELECT id FROM user_dir WHERE id=%s AND user_id=%s", (dir_id, user_id))
        if not d:
            raise HTTPException(404, "目录不存在")
        rows = pg_store.query(
            """SELECT s.id AS session_id, s.dir_id, s.summary, s.created_at,
                      (SELECT count(*) FROM qa_log q WHERE q.session_id=s.id) AS turns
               FROM qa_session s WHERE s.user_id=%s AND s.dir_id=%s
               ORDER BY s.created_at DESC""", (user_id, dir_id))
    else:
        rows = pg_store.query(
            """SELECT s.id AS session_id, s.dir_id, s.summary, s.created_at,
                      (SELECT count(*) FROM qa_log q WHERE q.session_id=s.id) AS turns
               FROM qa_session s WHERE s.user_id=%s
               ORDER BY s.created_at DESC""", (user_id,))
    out = []
    for r in rows:
        d = dict(r)
        d["created_at"] = str(d["created_at"])
        out.append(d)
    return {"items": out}


@app.get("/qa/sessions/{session_id}/history")
async def session_history(session_id: str, x_user_id: str = Header(default="")):
    if not x_user_id:
        raise HTTPException(401, "缺少 X-User-Id")
    try:
        user_id = int(x_user_id)
    except ValueError:
        raise HTTPException(400, "X-User-Id 非法")
    _session_or_403(session_id, user_id)
    rows = pg_store.query(
        """SELECT query, answer, created_at, chunk_ids FROM qa_log
           WHERE session_id=%s AND user_id=%s ORDER BY id ASC""", (session_id, user_id))
    out = []
    for r in rows:
        d = dict(r)
        d["created_at"] = str(d["created_at"])
        d["chunk_ids"] = list(d["chunk_ids"] or [])
        out.append(d)
    return {"items": out}


@app.post("/qa/ask")
async def ask(req: AskRequest, x_user_id: str = Header(default="")):
    query = (req.query or "").strip()
    if not query:
        raise HTTPException(400, "query 不能为空")
    if len(query) > 500:
        raise HTTPException(400, "问题过长（上限 500 字）")
    if not x_user_id:
        raise HTTPException(401, "缺少 X-User-Id")
    try:
        user_id = int(x_user_id)
    except ValueError:
        raise HTTPException(400, "X-User-Id 非法")

    # 准备阶段（会话/缓存/检索/L2/记忆/历史）整体放线程池执行：全是同步阻塞调用，
    # 其中检索含 rerank（实测 top50 ≈ 3.4s），直接跑事件循环会让所有并发请求排队等待
    prep = await asyncio.to_thread(_prepare_ask, req, user_id)

    if prep["cached"]:
        answer = prep["answer"]
        is_shared = prep["is_shared"]

        def event_cached(etype: str, **data) -> str:
            return f"data: {json.dumps({'type': etype, **data}, ensure_ascii=False)}\n\n"

        async def gen_cached():
            yield event_cached("meta", rejected=False, citations=[], cached=True,
                               cache_shared=is_shared, context_tokens=0, low_confidence=False)
            yield event_cached("delta", text=answer)
            # 缓存命中也落 qa_log（cache_hit=TRUE）：反馈闭环/审计需要 qa_log_id
            qa_log_id = None
            try:
                qa_log_id = pg_store.query_one(
                    """INSERT INTO qa_log (session_id, user_id, query, answer, route,
                                           chunk_ids, total_ms, token_in, token_out, thinking, cache_hit)
                       VALUES (%s,%s,%s,%s,'qa',NULL,0,%s,%s,FALSE,TRUE) RETURNING id""",
                    (req.session_id or None, user_id, query, answer,
                     _estimate_tokens(query), _estimate_tokens(answer)))["id"]
            except Exception as e:
                print(f"[qa_log] cached write failed: {e}", file=sys.stderr)
            yield event_cached("done", qa_log_id=qa_log_id)

        return StreamingResponse(gen_cached(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    chunks = prep["chunks"]
    rejected = prep["rejected"]
    reject_reason = prep["reject_reason"]
    cache_ref = prep["cache_ref"]
    qvec = prep["qvec"]
    q_hash = prep["q_hash"]
    memories = prep["memories"]
    hist_text = prep["hist_text"]
    hist_tokens = prep["hist_tokens"]
    history = prep["history"]
    agent_mode = prep.get("agent_mode", False)
    agent_route = prep.get("agent_route", "")

    t0 = time.time()

    def event(etype: str, **data) -> str:
        return f"data: {json.dumps({'type': etype, **data}, ensure_ascii=False)}\n\n"

    async def gen():
        citations = [
            {"index": i + 1, "doc_name": c.filename, "page_no": c.page_no,
             "score": round(c.score, 3), "reranked": c.reranked}
            for i, c in enumerate(chunks)]
        answer_buf = []

        def persist(interrupted: bool = False):
            """落 qa_log（正常/客户端中断都落，审计与反馈可用）；qa_cache 仅正常完成且非拒答时写。
            Agent 档由 run_agent 内部落 qa_log（route 记录实际档位），此处仅写缓存。
            返回 qa_log_id（失败返回 None，不阻塞流式）。"""
            answer = "".join(answer_buf)
            total_ms = int((time.time() - t0) * 1000)
            log_id = None
            if not agent_mode:
                try:
                    log_id = pg_store.query_one(
                        """INSERT INTO qa_log (session_id, user_id, query, answer, route,
                                               chunk_ids, total_ms, token_in, token_out, thinking, cache_hit)
                           VALUES (%s,%s,%s,%s,'qa',%s,%s,%s,%s,FALSE,FALSE) RETURNING id""",
                        (req.session_id or None, user_id, query, answer,
                         [c.chunk_id for c in chunks], total_ms,
                         _estimate_tokens(query) + (hist_tokens if history else 0),
                         _estimate_tokens(answer)))["id"]
                    if req.session_id and not interrupted:
                        from memory import memory as mem
                        mem.maybe_extract(req.session_id, user_id)
                except Exception as e:
                    print(f"[qa_log] write failed: {e}", file=sys.stderr)
            # 问答存档写入：非拒答才存（存坏答案会污染后续命中）；文件变更时由 ingest 失效。
            # 注意：BM25 强命中保护降级路径（reranked=False）rejected=False 但 LLM 可能仍判无相关内容
            # （输出"资料中未找到..."），这类拒答文案同样不得存档，否则 L1 直接命中坏答案；
            # 中断（interrupted）不写缓存——回答不完整，写了会污染 L1 命中
            if (not interrupted and not rejected and answer and qvec is not None
                    and not answer.startswith("资料中未找到")):
                try:
                    from pgvector.psycopg import register_vector
                    file_rows = pg_store.query(
                        "SELECT DISTINCT file_id FROM rag_chunk WHERE id = ANY(%s)",
                        ([c.chunk_id for c in chunks],))
                    file_ids = sorted(r["file_id"] for r in file_rows)
                    with pg_store.connect() as conn:
                        register_vector(conn)
                        conn.execute(
                            """INSERT INTO qa_cache (user_id, query_hash, query, query_embedding,
                                                     answer, chunk_ids, file_ids)
                               VALUES (%s,%s,%s,%s,%s,%s,%s)
                               ON CONFLICT (user_id, query_hash) DO UPDATE SET
                                 answer=EXCLUDED.answer, chunk_ids=EXCLUDED.chunk_ids,
                                 file_ids=EXCLUDED.file_ids, invalidated=FALSE, updated_at=now()""",
                            (user_id, q_hash, query, qvec, answer,
                             [c.chunk_id for c in chunks], file_ids))
                except Exception as e:
                    print(f"[qa_cache] write failed: {e}", file=sys.stderr)
            return log_id

        done_sent = False
        try:
            if agent_mode:
                # Agent 档：run_agent 已完整生成（多轮检索+评估+反思），非流式一次性输出
                agent_answer = prep.get("answer", "")
                answer_buf.append(agent_answer)
                yield event("meta", rejected=prep["rejected"], citations=prep.get("citations") or [],
                            agent_route=agent_route, context_tokens=0, history_tokens=0,
                            cache_ref=False, memory_hits=0, low_confidence=False)
                if agent_answer:
                    yield event("delta", text=agent_answer)
            elif rejected:
                answer = "资料中未找到相关内容"
                yield event("meta", rejected=True, message=answer, reason=reject_reason, citations=[])
                answer_buf.append(answer)
            else:
                ctx, used_tokens = _build_context(chunks, MAX_CONTEXT_TOKENS - hist_tokens)
                sections = []
                if memories:
                    mem_lines = [f"{m['mem_type']}: {m['content']}" for m in memories]
                    sections.append("【长期记忆】\n" + "\n".join(mem_lines) +
                                    "\n（长期记忆仅供了解用户背景，回答仍须依据【资料】）")
                if cache_ref:
                    sections.append("【历史回答参考】\n"
                                    f"问：{cache_ref['query']}\n答：{cache_ref['answer'][:400]}\n"
                                    "（历史回答参考，可能不完全相关，请以【资料】为准）")
                if hist_text:
                    sections.append(f"【历史对话】\n{hist_text}\n（历史对话仅供理解上下文，回答仍须依据【资料】）")
                sections.append(f"【资料】\n{ctx}")
                sections.append(f"【问题】\n{query}")
                messages = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": "\n\n".join(sections)},
                ]
                yield event("meta", rejected=False, citations=citations,
                            context_tokens=used_tokens, history_tokens=hist_tokens,
                            cache_ref=bool(cache_ref), memory_hits=len(memories),
                            low_confidence=not chunks[0].reranked)
                try:
                    # 思考过程逐块透出（thinking 事件，仅展示不落库）；正文仍走 delta
                    # thinking 开关：前端可关闭（省 reasoning token，但忠实度下降——E3 消融数据）；
                    # with_reasoning 恒 True 保证事件恒为 (etype, text) 元组，关闭时仅不发 thinking 事件
                    # max_tokens=8192：reasoning 计入预算，给足思考+长回答空间（流式无法事后重试）
                    # MiMoClient().stream 是同步 httpx 流式：放后台线程逐块产出、队列桥接到事件循环，
                    # 否则生成全程（5-15s）独占事件循环，并发请求全部排队（FastAPI 假并发经典坑）
                    import queue as _queue
                    import threading as _threading
                    _llm_q: _queue.Queue = _queue.Queue(maxsize=64)

                    def _llm_producer():
                        try:
                            for ev in MiMoClient().stream(messages, thinking=req.thinking,
                                                          temperature=0.2, max_tokens=8192,
                                                          with_reasoning=True):
                                _llm_q.put(("ev", ev))
                            _llm_q.put(("end", None))
                        except Exception as e:
                            _llm_q.put(("err", e))

                    _threading.Thread(target=_llm_producer, daemon=True).start()
                    while True:
                        kind, payload = await asyncio.to_thread(_llm_q.get)
                        if kind == "end":
                            break
                        if kind == "err":
                            yield event("error", message=f"生成失败: {payload}")
                            break
                        etype, piece = payload
                        if etype == "thinking":
                            if req.thinking:
                                yield event("thinking", text=piece)
                        else:
                            answer_buf.append(piece)
                            yield event("delta", text=piece)
                except Exception as e:
                    yield event("error", message=f"生成失败: {e}")

            yield event("done", qa_log_id=await asyncio.to_thread(persist))
            done_sent = True
        finally:
            # 客户端中断兜底（GeneratorExit 在 yield 处抛出）：已生成部分仍落 qa_log
            # （审计与反馈可用），不写 qa_cache（回答不完整）；正常路径 done_sent=True 已落库
            if not done_sent and answer_buf:
                try:
                    await asyncio.to_thread(persist, interrupted=True)
                except Exception as e:
                    print(f"[qa_log] interrupted persist failed: {e}", file=sys.stderr)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
