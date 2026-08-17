"""问答 API：同步 + SSE 流式。"""
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agent import qa_service

router = APIRouter(tags=["rag"])


def _get_user_id(request: Request) -> int | None:
    """从请求头 X-User-Id 提取用户 ID。缺失时返回 None（全局访问）。"""
    uid = request.headers.get("X-User-Id")
    if uid is None:
        return None
    try:
        return int(uid)
    except ValueError:
        raise HTTPException(400, "X-User-Id 必须是整数")


class AskRequest(BaseModel):
    query: str
    session_id: str = ""
    top_k: int = 0
    stream: bool = False


@router.post("/ask")
def ask(req: AskRequest, request: Request):
    if not req.query.strip():
        raise HTTPException(400, "query 不能为空")
    user_id = _get_user_id(request)
    if req.stream:
        def gen():
            for evt in qa_service.ask_stream(req.query, req.session_id, req.top_k, user_id=user_id):
                yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")
    return qa_service.ask(req.query, req.session_id, req.top_k, user_id=user_id)


@router.get("/history/{session_id}")
def history(session_id: str, limit: int = 20, request: Request = None):
    """查看会话历史（多租户：校验 session 归属）。"""
    from db import pg_store
    user_id = _get_user_id(request) if request else None
    if user_id is not None:
        # 权限校验：session 必须属于当前用户
        session = pg_store.query_one(
            "SELECT user_id FROM qa_session WHERE id=%s", (session_id,))
        if session and session["user_id"] is not None and session["user_id"] != user_id:
            raise HTTPException(403, "无权访问该会话")
        rows = pg_store.query(
            """SELECT query, answer, created_at, total_ms FROM qa_log
               WHERE session_id=%s AND user_id=%s ORDER BY id DESC LIMIT %s""",
            (session_id, user_id, limit))
    else:
        rows = pg_store.query(
            """SELECT query, answer, created_at, total_ms FROM qa_log
               WHERE session_id=%s ORDER BY id DESC LIMIT %s""", (session_id, limit))
    for r in rows:
        r["created_at"] = str(r["created_at"])
    return list(reversed(rows))
