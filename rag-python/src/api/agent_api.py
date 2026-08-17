"""Agent API：LangGraph 主图执行入口（HITL resume 在 Phase 5 加入）。"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(tags=["agent"])


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
def run(req: AgentRunRequest):
    if not req.query.strip():
        raise HTTPException(400, "query 不能为空")
    from agent.main_graph import run_agent
    try:
        return run_agent(req.query, req.session_id, req.history)
    except Exception as e:
        raise HTTPException(500, f"agent 执行失败: {e}")
