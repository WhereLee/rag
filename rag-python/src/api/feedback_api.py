"""反馈闭环 API。多租户版本。"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from feedback import attributor

router = APIRouter(tags=["feedback"])


def _get_user_id(request: Request) -> int | None:
    uid = request.headers.get("X-User-Id")
    if uid is None:
        return None
    try:
        return int(uid)
    except ValueError:
        raise HTTPException(400, "X-User-Id 必须是整数")


class FeedbackRequest(BaseModel):
    qa_log_id: int
    rating: int            # 1 赞 / -1 踩
    correction: str = ""


@router.post("")
def submit(req: FeedbackRequest, request: Request = None):
    if req.rating not in (1, -1):
        raise HTTPException(400, "rating 只能是 1 或 -1")
    user_id = _get_user_id(request) if request else None
    try:
        return attributor.submit_feedback(req.qa_log_id, req.rating, req.correction,
                                          user_id=user_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.get("/bad-cases")
def bad_cases(status: str = "", request: Request = None):
    user_id = _get_user_id(request) if request else None
    return attributor.list_bad_cases(status, user_id=user_id)


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
