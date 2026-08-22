"""
反馈闭环：负反馈收集 → 自动归因 → 升级回归集。

归因逻辑（架构文档 §7.3）：
- retrieval：正确答案的证据不在检索结果里（检索召回问题）
- generation：证据在检索结果里但答案错误（生成/prompt 问题）
规则预判 + LLM 复核，归因结论写入 bad_case.attribution。
"""
import json
import logging
import threading

from db import pg_store
from llm.mimo_client import get_client, LLMError

logger = logging.getLogger("rag.feedback")


def _safe_attribute(bc_id: int) -> None:
    """后台线程执行归因：LLM 调用耗时 20-60s，同步会拖垮提交响应（网关 30s 超时截断）。"""
    try:
        attribute(bc_id)
    except Exception as e:
        logger.warning("auto attribution failed: %s", e)

ATTR_PROMPT = """用户对问答系统的回答不满意。请判断问题出在哪个阶段：
- retrieval：检索到的资料里没有能回答问题的内容（需要改进检索/切块/embedding）
- generation：检索资料里其实有答案，但系统答错/答偏/编造（需要改进 prompt/生成）
输出 JSON：{"attribution": "retrieval 或 generation", "reason": "<一句话依据>",
"suggested_fix": "<一句话修复方向>"}

【用户问题】{question}
【用户纠错/不满】{correction}
【系统回答】{answer}
【当时的检索资料】{context}"""


def submit_feedback(qa_log_id: int, rating: int, correction: str = "",
                    user_id: int | None = None) -> dict:
    qa = pg_store.query_one(
        "SELECT * FROM qa_log WHERE id=%s", (qa_log_id,))
    if not qa:
        raise ValueError(f"qa_log {qa_log_id} 不存在")
    fb_id = pg_store.query_one(
        "INSERT INTO feedback (qa_log_id, user_id, rating, correction) VALUES (%s,%s,%s,%s) RETURNING id",
        (qa_log_id, user_id, rating, correction))["id"]
    result = {"feedback_id": fb_id}
    if rating < 0:
        snapshot = _build_snapshot(qa)
        bc_id = pg_store.query_one(
            """INSERT INTO bad_case (qa_log_id, user_id, query, snapshot, attribution, status)
               VALUES (%s,%s,%s,%s,'pending','open') RETURNING id""",
            (qa_log_id, user_id, qa["query"], json.dumps(snapshot, ensure_ascii=False)))["id"]
        result["bad_case_id"] = bc_id
        # 异步自动归因（LLM 20-60s）：提交立即返回，归因结果稍后可见于 bad case 列表
        threading.Thread(target=_safe_attribute, args=(bc_id,), daemon=True).start()
    return result


def _build_snapshot(qa: dict) -> dict:
    chunks = []
    if qa.get("chunk_ids"):
        rows = pg_store.query(
            "SELECT id, content FROM rag_chunk WHERE id = ANY(%s)", (qa["chunk_ids"],))
        chunks = [{"id": r["id"], "content": r["content"][:500]} for r in rows]
    return {"query": qa["query"], "answer": qa["answer"],
            "route": qa.get("route"), "chunks": chunks}


def attribute(bad_case_id: int) -> dict:
    """对单个 bad case 做归因。"""
    bc = pg_store.query_one("SELECT * FROM bad_case WHERE id=%s", (bad_case_id,))
    if not bc:
        raise ValueError("bad_case 不存在")
    snap = bc["snapshot"] or {}
    correction = ""
    fb = pg_store.query_one(
        "SELECT correction FROM feedback WHERE qa_log_id=%s ORDER BY id DESC LIMIT 1",
        (bc["qa_log_id"],))
    if fb:
        correction = fb.get("correction") or ""
    context = "\n".join(c["content"] for c in snap.get("chunks", []))[:2500] or "（无）"
    prompt = (ATTR_PROMPT
              .replace("{question}", snap.get("query", ""))
              .replace("{correction}", correction or "未提供")
              .replace("{answer}", snap.get("answer", ""))
              .replace("{context}", context))
    try:
        obj = get_client().chat_json(
            [{"role": "user", "content": prompt}],
            thinking=False, max_tokens=1024)
        attr = obj.get("attribution", "")
        if attr not in ("retrieval", "generation"):
            attr = "generation"
        out = {"attribution": attr, "reason": obj.get("reason", ""),
               "suggested_fix": obj.get("suggested_fix", "")}
    except LLMError as e:
        # 规则兜底：无检索结果 → retrieval；有 → generation
        attr = "retrieval" if not snap.get("chunks") else "generation"
        out = {"attribution": attr, "reason": f"LLM 归因失败，规则兜底: {e}",
               "suggested_fix": ""}
    pg_store.execute("UPDATE bad_case SET attribution=%s WHERE id=%s",
                     (out["attribution"], bad_case_id))
    return out


def confirm_bad_case(bad_case_id: int) -> dict:
    """确认 bad case 有效 → 升级为回归集题目（HITL 决策点）。"""
    bc = pg_store.query_one("SELECT * FROM bad_case WHERE id=%s", (bad_case_id,))
    if not bc:
        raise ValueError("bad_case 不存在")
    snap = bc["snapshot"] or {}
    fb = pg_store.query_one(
        "SELECT correction FROM feedback WHERE qa_log_id=%s ORDER BY id DESC LIMIT 1",
        (bc["qa_log_id"],))
    correction = (fb or {}).get("correction") or ""
    # 用户纠错内容作为参考答案；无纠错则标记待人工补充
    exists = pg_store.query_one("SELECT id FROM eval_question WHERE question=%s",
                                (bc["query"],))
    if exists:
        pg_store.execute(
            "UPDATE eval_question SET in_regression=TRUE WHERE id=%s", (exists["id"],))
        qid = exists["id"]
    else:
        qid = pg_store.query_one(
            """INSERT INTO eval_question (question, reference_answer, dimension,
                                          in_regression, meta)
               VALUES (%s,%s,'factual',TRUE,%s) RETURNING id""",
            (bc["query"], correction,
             json.dumps({"origin": f"bad_case:{bad_case_id}",
                         "attribution": bc["attribution"]}, ensure_ascii=False)))["id"]
    pg_store.execute("UPDATE bad_case SET status='in_regression' WHERE id=%s",
                     (bad_case_id,))
    return {"question_id": qid, "bad_case_id": bad_case_id,
            "regression_size": pg_store.query_one(
                "SELECT count(*) AS n FROM eval_question WHERE in_regression")["n"]}


def list_bad_cases(status: str = "", user_id: int | None = None) -> list[dict]:
    sql = "SELECT * FROM bad_case"
    params = []
    conditions = []
    if status:
        conditions.append("status=%s")
        params.append(status)
    if user_id is not None:
        conditions.append("user_id=%s")
        params.append(user_id)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY id DESC LIMIT 100"
    return pg_store.query(sql, params or None)
