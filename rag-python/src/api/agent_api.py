"""Agent API：LangGraph 主图执行入口。多租户版本。"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(tags=["agent"])


def _get_user_id(request: Request) -> int | None:
    uid = request.headers.get("X-User-Id")
    if uid is None:
        return None
    try:
        return int(uid)
    except ValueError:
        raise HTTPException(400, "X-User-Id 必须是整数")


class AgentRunRequest(BaseModel):
    query: str
    session_id: str = ""
    history: list = []


class ExperimentRequest(BaseModel):
    force_thinking: bool | None = None    # E3：None=默认档位路由
    disable_reflect: bool = False         # E4


@router.post("/experiment")
def set_experiment(req: ExperimentRequest):
    """实验开关（E3/E4）：评估前设置，评估后复位。

    安全守卫：仅 EXPERIMENT_MODE=1 时允许经 HTTP 修改（全局状态影响在线问答质量），
    否则 403。实验脚本（run_experiments.py）进程内直调不受此限制。
    """
    import config
    if not config.EXPERIMENT_MODE:
        raise HTTPException(403, "实验开关仅限实验模式（EXPERIMENT_MODE=1）下通过 HTTP 修改")
    from agent.main_graph import experiment_flags
    experiment_flags["force_thinking"] = req.force_thinking
    experiment_flags["disable_reflect"] = req.disable_reflect
    return {"experiment_flags": experiment_flags}


@router.post("/run")
def run(req: AgentRunRequest, request: Request = None):
    if not req.query.strip():
        raise HTTPException(400, "query 不能为空")
    user_id = _get_user_id(request) if request else None
    from agent.main_graph import run_agent
    try:
        return run_agent(req.query, req.session_id, req.history, user_id=user_id)
    except Exception as e:
        raise HTTPException(500, f"agent 执行失败: {e}")
