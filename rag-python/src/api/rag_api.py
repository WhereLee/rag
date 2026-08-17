"""问答 API：同步 + SSE 流式。"""
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agent import qa_service

router = APIRouter(tags=["rag"])


class AskRequest(BaseModel):
    query: str
    session_id: str = ""
    top_k: int = 0
    stream: bool = False


@router.post("/ask")
def ask(req: AskRequest):
    if not req.query.strip():
        raise HTTPException(400, "query 不能为空")
    if req.stream:
        def gen():
            for evt in qa_service.ask_stream(req.query, req.session_id, req.top_k):
                yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")
    return qa_service.ask(req.query, req.session_id, req.top_k)


@router.get("/history/{session_id}")
def history(session_id: str, limit: int = 20):
    from db import pg_store
    rows = pg_store.query(
        """SELECT query, answer, created_at, total_ms FROM qa_log
           WHERE session_id=%s ORDER BY id DESC LIMIT %s""", (session_id, limit))
    for r in rows:
        r["created_at"] = str(r["created_at"])
    return list(reversed(rows))
