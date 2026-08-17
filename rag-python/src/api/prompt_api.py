"""Prompt 管理 + HITL 审批 API。"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import pg_store

router = APIRouter(tags=["prompt"])


@router.get("/prompts")
def list_prompts():
    rows = pg_store.query(
        """SELECT code, version, status, updated_at,
                  length(content) AS content_len
           FROM prompt_registry ORDER BY code""")
    return rows


@router.get("/prompts/{code}")
def get_prompt(code: str):
    row = pg_store.query_one(
        "SELECT code, content, version, status, updated_at FROM prompt_registry WHERE code=%s",
        (code,))
    if not row:
        raise HTTPException(404, f"prompt {code} 不存在")
    return row


class ChangeRequest(BaseModel):
    new_content: str


@router.post("/prompts/{code}/change")
def submit_change(code: str, req: ChangeRequest):
    """提交 prompt 变更 → 自动回归对比 → interrupt 等待审批。"""
    from agent.approval_graph import submit_change as do_submit
    try:
        return do_submit(code, req.new_content)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/approvals")
def list_approvals():
    rows = pg_store.query(
        """SELECT id, prompt_code, decision, eval_compare, created_at, decided_at
           FROM prompt_approval ORDER BY id DESC LIMIT 50""")
    for r in rows:
        r["created_at"] = str(r["created_at"])
        r["decided_at"] = str(r["decided_at"]) if r["decided_at"] else None
    return rows


@router.get("/approvals/{approval_id}")
def get_approval(approval_id: int):
    row = pg_store.query_one("SELECT * FROM prompt_approval WHERE id=%s", (approval_id,))
    if not row:
        raise HTTPException(404, "审批单不存在")
    row["created_at"] = str(row["created_at"])
    row["decided_at"] = str(row["decided_at"]) if row["decided_at"] else None
    return row


class ResumeRequest(BaseModel):
    decision: str   # approved / rejected


@router.post("/approvals/{approval_id}/resume")
def resume(approval_id: int, req: ResumeRequest):
    """人工决策：恢复中断的审批图。"""
    from agent.approval_graph import resume_decision
    try:
        out = resume_decision(approval_id, req.decision)
    except ValueError as e:
        raise HTTPException(400, str(e))
    out["decided_at"] = str(out["decided_at"])
    return out
