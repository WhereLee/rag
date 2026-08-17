"""FastAPI 应用骨架。路由按阶段逐步挂载（ingest → rag → agent → eval → feedback → diagnosis）。"""
from fastapi import FastAPI

import config
from observability.logging_setup import setup_logging

setup_logging()

app = FastAPI(
    title="智能文档问答系统",
    description="多格式文档解析 + Agentic RAG + 评估闭环",
    version="0.1.0",
)


@app.get("/health")
def health():
    checks = {"service": "up"}
    # PG 探活
    try:
        from db import pg_store
        pg_store.query_one("SELECT 1 AS ok")
        checks["postgres"] = "up"
    except Exception as e:
        checks["postgres"] = f"down: {e}"
    # Redis 探活
    try:
        import redis as redis_lib
        redis_lib.Redis.from_url(config.REDIS_URL, socket_timeout=2).ping()
        checks["redis"] = "up"
    except Exception as e:
        checks["redis"] = f"down: {e}"
    status = 200 if all(v == "up" for v in checks.values()) else 503
    from fastapi.responses import JSONResponse
    return JSONResponse({"service": config.SERVICE_NAME, "checks": checks}, status_code=status)


# 阶段性路由挂载（逐 Phase 放开）
from api.ingest_api import router as ingest_router  # noqa: E402
from api.rag_api import router as rag_router  # noqa: E402
from api.eval_api import router as eval_router  # noqa: E402
from api.agent_api import router as agent_router  # noqa: E402
from api.feedback_api import router as feedback_router  # noqa: E402
from api.prompt_api import router as prompt_router  # noqa: E402
from api.diagnosis_api import router as diagnosis_router  # noqa: E402
from observability.tracing import setup_tracing  # noqa: E402
app.include_router(ingest_router, prefix="/api/ingest")
app.include_router(rag_router, prefix="/api/rag")
app.include_router(eval_router, prefix="/api/eval")
app.include_router(agent_router, prefix="/api/agent")
app.include_router(feedback_router, prefix="/api/feedback")
app.include_router(prompt_router, prefix="/api/admin")
app.include_router(diagnosis_router, prefix="/api/diagnosis")

setup_tracing()
