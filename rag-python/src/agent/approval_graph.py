"""
审批 Agent 图（Human-in-the-Loop）：Prompt 变更的量化审批闭环。

流程（架构文档 §2.3）：
  START → suggest（自动跑回归集新旧对比）→ interrupt（人工审批）
        → approved: execute（生效 + 刷新缓存）
        → rejected: rollback（记录拒绝）

关键设计：审批不是走形式——interrupt 携带的是回归集上新旧 prompt 的
量化对比数据（eval_compare），人工决策有数据依据。
"""
import json
import logging
from typing import TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command

import config
from db import pg_store
from eval import evaluator
from llm import prompt_loader

logger = logging.getLogger("rag.approval")


class ApprovalState(TypedDict):
    approval_id: int
    prompt_code: str
    old_content: str
    new_content: str
    eval_compare: dict
    decision: str


def suggest_node(state: ApprovalState) -> dict:
    """自动回归对比：当前版本 vs 候选版本（回归集，无 judge 控成本）。"""
    code = state["prompt_code"]
    compare = {"note": ""}
    try:
        # A: 当前版本
        ra = evaluator.run_eval(name=f"approval-{state['approval_id']}-current",
                                regression_only=True)
        # B: 候选版本——临时覆盖内存缓存（registry 未动，安全）
        old_cache = dict(prompt_loader._cache)
        prompt_loader._cache[code] = state["new_content"]
        try:
            rb = evaluator.run_eval(name=f"approval-{state['approval_id']}-candidate",
                                    regression_only=True)
        finally:
            prompt_loader._cache = old_cache
        compare = evaluator.compare_runs(ra["run_id"], rb["run_id"])
    except Exception as e:
        compare = {"note": f"回归对比失败（不阻塞审批）: {e}"}
        logger.error("approval eval compare failed: %s", e)
    # 回归门禁判定：机器预判写进 compare（人工仍可 override，advisory gate）
    try:
        from eval import gate
        g = gate.check_gate(compare.get("delta_b_minus_a", {}))
        compare["gate"] = g
        logger.info("approval gate verdict: %s", g["note"])
    except Exception as e:
        logger.warning("gate check failed (advisory, no block): %s", e)
    pg_store.execute("UPDATE prompt_approval SET eval_compare=%s WHERE id=%s",
                     (json.dumps(compare, ensure_ascii=False, default=str),
                      state["approval_id"]))
    return {"eval_compare": compare}


def human_gate_node(state: ApprovalState) -> dict:
    """interrupt：暂停等待人工决策（状态由 Checkpointer 持久化，可跨请求/重启恢复）。"""
    decision = interrupt({
        "approval_id": state["approval_id"],
        "prompt_code": state["prompt_code"],
        "old_content": state["old_content"],
        "new_content": state["new_content"],
        "eval_compare": state.get("eval_compare"),
        "question": "是否批准该 prompt 变更？resume 时传入 {'decision': 'approved' 或 'rejected'}",
    })
    d = (decision or {}).get("decision", "rejected")
    return {"decision": d if d in ("approved", "rejected") else "rejected"}


def execute_node(state: ApprovalState) -> dict:
    """批准：registry 升版生效 + 刷新缓存 + 审批记录落定。"""
    pg_store.execute(
        """UPDATE prompt_registry SET content=%s, version=version+1, updated_at=NOW()
           WHERE code=%s""", (state["new_content"], state["prompt_code"]))
    pg_store.execute(
        """UPDATE prompt_approval SET decision='approved', decided_at=NOW()
           WHERE id=%s""", (state["approval_id"],))
    prompt_loader.refresh()
    logger.info("prompt approved: %s", state["prompt_code"])
    return {}


def rollback_node(state: ApprovalState) -> dict:
    pg_store.execute(
        """UPDATE prompt_approval SET decision='rejected', decided_at=NOW()
           WHERE id=%s""", (state["approval_id"],))
    logger.info("prompt rejected: %s", state["prompt_code"])
    return {}


_graph = None
_ckpt_conn = None


def get_approval_graph():
    global _graph, _ckpt_conn
    if _graph is not None:
        return _graph
    g = StateGraph(ApprovalState)
    g.add_node("suggest", suggest_node)
    g.add_node("human_gate", human_gate_node)
    g.add_node("execute", execute_node)
    g.add_node("rollback", rollback_node)
    g.add_edge(START, "suggest")
    g.add_edge("suggest", "human_gate")
    g.add_conditional_edges("human_gate",
                            lambda s: "execute" if s["decision"] == "approved" else "rollback",
                            {"execute": "execute", "rollback": "rollback"})
    g.add_edge("execute", END)
    g.add_edge("rollback", END)

    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver
    _ckpt_conn = psycopg.connect(config.PG_DSN, autocommit=True)
    saver = PostgresSaver(_ckpt_conn)
    saver.setup()
    _graph = g.compile(checkpointer=saver)
    return _graph


def submit_change(prompt_code: str, new_content: str) -> dict:
    """提交 prompt 变更 → 建审批单 → 启动审批图（跑到 interrupt 暂停）。"""
    current = pg_store.query_one(
        "SELECT content FROM prompt_registry WHERE code=%s", (prompt_code,))
    old_content = current["content"] if current else prompt_loader.DEFAULT_PROMPTS.get(prompt_code, "")
    if not old_content:
        raise ValueError(f"未知 prompt code: {prompt_code}")
    if new_content.strip() == old_content.strip():
        raise ValueError("新旧 prompt 相同，无需审批")
    approval_id = pg_store.query_one(
        """INSERT INTO prompt_approval (prompt_code, old_content, new_content)
           VALUES (%s,%s,%s) RETURNING id""",
        (prompt_code, old_content, new_content))["id"]
    graph = get_approval_graph()
    thread_id = f"approval-{approval_id}"
    graph.invoke(
        {"approval_id": approval_id, "prompt_code": prompt_code,
         "old_content": old_content, "new_content": new_content,
         "eval_compare": {}, "decision": ""},
        config={"configurable": {"thread_id": thread_id}})
    return {"approval_id": approval_id, "thread_id": thread_id,
            "status": "waiting_human"}


def resume_decision(approval_id: int, decision: str) -> dict:
    """人工决策 → 恢复中断的审批图执行到底。"""
    if decision not in ("approved", "rejected"):
        raise ValueError("decision 只能是 approved/rejected")
    row = pg_store.query_one("SELECT * FROM prompt_approval WHERE id=%s", (approval_id,))
    if not row:
        raise ValueError("审批单不存在")
    if row["decision"] != "pending":
        raise ValueError(f"审批单已决策: {row['decision']}")
    graph = get_approval_graph()
    thread_id = f"approval-{approval_id}"
    # 从中断点恢复：必须用 Command(resume=...) 传递人工决策
    graph.invoke(Command(resume={"decision": decision}),
                 config={"configurable": {"thread_id": thread_id}})
    return pg_store.query_one("SELECT id, prompt_code, decision, decided_at FROM prompt_approval WHERE id=%s",
                              (approval_id,))
