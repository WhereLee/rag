"""诊断 API。"""
from fastapi import APIRouter

from agent import diagnosis

router = APIRouter(tags=["diagnosis"])


@router.get("/latest")
def latest():
    return diagnosis.latest_report() or {"note": "暂无报告，请先 trigger"}


@router.get("/metrics")
def metrics():
    """采集指标的只读快照（供外部监控/Grafana 接入，不落库不调 LLM）。"""
    return diagnosis.collect_metrics()


@router.get("/history")
def history(limit: int = 20):
    return diagnosis.history(limit)


@router.post("/trigger")
def trigger():
    return diagnosis.generate_report()
