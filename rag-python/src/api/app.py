"""FastAPI 应用骨架。路由按阶段逐步挂载（ingest → rag → agent → eval → feedback → diagnosis）。"""
import hmac
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

import config
from observability.logging_setup import setup_logging

setup_logging()

logger = logging.getLogger("rag.security")

app = FastAPI(
    title="智能文档问答系统",
    description="多格式文档解析 + Agentic RAG + 评估闭环",
    version="0.1.0",
)


# ===== 全局异常处理：错误信息脱敏 =====
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """捕获未处理的异常，返回通用错误消息，不泄露内部细节。
    详细错误信息仅记录到日志。"""
    from fastapi import HTTPException
    # HTTPException 已经是我们主动抛出的业务错误，保持原样
    if isinstance(exc, HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    # 未预期的异常：记录完整信息到日志，对外只返回通用消息
    logger.error("Unhandled exception on %s %s: %s", request.method, request.url.path, exc,
                 exc_info=True)
    return JSONResponse(
        {"error": "服务内部错误，请稍后重试"},
        status_code=500
    )


# ===== 安全中间件：内部 API Key 验证 =====
class InternalAuthMiddleware(BaseHTTPMiddleware):
    """Python 服务纵深防御层。

    策略：
    - INTERNAL_API_KEY 已配置：验证 X-Internal-Key 请求头（来自网关注入）
    - INTERNAL_API_KEY 未配置：仅允许 localhost 访问（开发环境）
    - /health 接口始终放行
    - 同时提取 X-Request-ID 注入日志上下文（跨服务关联）
    """

    async def dispatch(self, request: Request, call_next):
        # 提取 correlation ID（来自 Java 网关 X-Request-ID）
        request_id = request.headers.get("X-Request-ID", "")
        if request_id:
            request.state.request_id = request_id

        # /health 不受限
        if request.url.path == "/health":
            return await call_next(request)

        key = config.INTERNAL_API_KEY
        if key:
            # 生产模式：验证 API Key
            provided = request.headers.get("X-Internal-Key", "")
            if not hmac.compare_digest(key, provided):
                return JSONResponse(
                    {"error": "Forbidden: invalid or missing X-Internal-Key"},
                    status_code=403
                )
        else:
            # 开发模式：仅允许 localhost
            client_host = request.client.host if request.client else ""
            if client_host not in ("127.0.0.1", "::1", "localhost"):
                return JSONResponse(
                    {"error": "Forbidden: direct access not allowed"},
                    status_code=403
                )
        return await call_next(request)


# ===== 请求日志中间件 =====
class AccessLogMiddleware(BaseHTTPMiddleware):
    """记录每个请求的入参/出参/耗时，注入 correlation ID 到日志上下文。"""

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)

        import time
        request_id = getattr(request.state, "request_id", "")
        user_id = request.headers.get("X-User-Id", "-")
        start = time.perf_counter()

        # 设置日志上下文
        extra = {"ctx_request_id": request_id, "ctx_user_id": user_id,
                 "ctx_method": request.method, "ctx_path": request.url.path}

        try:
            response = await call_next(request)
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            if elapsed_ms > 5000:
                logger.warning("SLOW %s %s user=%s status=%s elapsed=%dms",
                               request.method, request.url.path, user_id,
                               response.status_code, elapsed_ms, extra=extra)
            else:
                logger.info("%s %s user=%s status=%s elapsed=%dms",
                            request.method, request.url.path, user_id,
                            response.status_code, elapsed_ms, extra=extra)
            return response
        except Exception as e:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            logger.error("%s %s user=%s elapsed=%dms ERROR: %s",
                         request.method, request.url.path, user_id,
                         elapsed_ms, e, extra=extra, exc_info=True)
            raise


app.add_middleware(InternalAuthMiddleware)
app.add_middleware(AccessLogMiddleware)


@app.get("/health")
def health():
    checks = {"service": "up"}
    # PG 探活
    try:
        from db import pg_store
        pg_store.query_one("SELECT 1 AS ok")
        checks["postgres"] = "up"
    except Exception as e:
        logger.warning("postgres health check failed: %s", e)
        checks["postgres"] = "down"
    # Redis 探活
    try:
        import redis as redis_lib
        redis_lib.Redis.from_url(config.REDIS_URL, socket_timeout=2).ping()
        checks["redis"] = "up"
    except Exception as e:
        logger.warning("redis health check failed: %s", e)
        checks["redis"] = "down"
    status = 200 if all(v == "up" for v in checks.values()) else 503
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
