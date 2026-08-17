"""反馈闭环 API。"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from feedback import attributor

router = APIRouter(tags=["feedback"])


class FeedbackRequest(BaseModel):
    qa_log_id: int
    rating: int            # 1 赞 / -1 踩
    correction: str = ""


@router.post("")
def submit(req: FeedbackRequest):
    if req.rating not in (1, -1):
        raise HTTPException(400, "rating 只能是 1 或 -1")
    try:
        return attributor.submit_feedback(req.qa_log_id, req.rating, req.correction)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.get("/bad-cases")
def bad_cases(status: str = ""):
    return attributor.list_bad_cases(status)


@router.post("/bad-cases/{bc_id}/attribute")
def attribute(bc_id: int):
    try:
        return attributor.attribute(bc_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post("/bad-cases/{bc_id}/confirm")
def confirm(bc_id: int):
    """人工确认 bad case → 升级回归集（HITL 决策）。"""
    try:
        return attributor.confirm_bad_case(bc_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
