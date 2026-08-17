"""评估 API：触发评估 / 查询结果 / 对比。"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import pg_store
from eval import evaluator

router = APIRouter(tags=["eval"])


class RunRequest(BaseModel):
    name: str = ""
    regression_only: bool = False
    with_judge: bool = False
    top_k: int = 0
    engine: str = "baseline"   # baseline / agent


@router.post("/seed")
def seed():
    return {"added": evaluator.seed_questions()}


@router.post("/run")
def run(req: RunRequest):
    try:
        return evaluator.run_eval(req.name, req.regression_only,
                                  req.with_judge, req.top_k, req.engine)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/runs")
def runs():
    rows = pg_store.query(
        "SELECT id, name, metrics, created_at FROM eval_run ORDER BY id DESC LIMIT 50")
    for r in rows:
        r["created_at"] = str(r["created_at"])
    return rows


@router.get("/runs/{run_id}/results")
def results(run_id: int):
    rows = pg_store.query(
        """SELECT r.question_id, q.question, q.dimension, r.scores, r.answer
           FROM eval_result r JOIN eval_question q ON q.id = r.question_id
           WHERE r.run_id=%s ORDER BY r.question_id""", (run_id,))
    return rows


@router.get("/compare")
def compare(run_a: int, run_b: int):
    try:
        return evaluator.compare_runs(run_a, run_b)
    except ValueError as e:
        raise HTTPException(404, str(e))
