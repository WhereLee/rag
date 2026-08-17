"""诊断 API。多租户版本：admin 全局，普通用户仅看自己数据。"""
from fastapi import APIRouter, HTTPException, Request

from agent import diagnosis

router = APIRouter(tags=["diagnosis"])


def _get_user_id(request: Request) -> int | None:
    uid = request.headers.get("X-User-Id")
    if uid is None:
        return None
    try:
        return int(uid)
    except ValueError:
        raise HTTPException(400, "X-User-Id 必须是整数")


@router.get("/latest")
def latest():
    return diagnosis.latest_report() or {"note": "暂无报告，请先 trigger"}


@router.get("/metrics")
def metrics(request: Request = None):
    """采集指标的只读快照。user_id=None 时全局聚合（admin）。"""
    user_id = _get_user_id(request) if request else None
    return diagnosis.collect_metrics(user_id=user_id)


@router.get("/history")
def history(limit: int = 20):
    return diagnosis.history(limit)


@router.post("/trigger")
def trigger():
    return diagnosis.generate_report()
