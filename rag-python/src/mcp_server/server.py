"""
MCP Server（对外集成定位）：知识库工具暴露给任意外部 MCP Client。

多租户：每个 tool 增加 user_id 参数，用于检索隔离。
user_id=None 时全局访问（admin）。

设计决策（架构文档 §8.3）：项目内部 Agent 直调函数，不走 MCP；
MCP 的价值是“任何兼容 Client（IDE Agent 等）可直接接入本知识库”。

启动：python -m mcp_server.server          （stdio，从 src 目录或 src 在 sys.path 时）
      python -m mcp_server.server --sse    （SSE，端口 8091）
兼容 mcp>=2.0（MCPServer API，FastMCP 已移除）。
"""
import json
import sys
from pathlib import Path

# 独立进程运行，需要把 src 加入路径
SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from mcp.server.mcpserver import MCPServer  # noqa: E402

mcp = MCPServer("rag-knowledge-base",
                description="智能文档问答知识库：检索/问答/文档状态查询")

# 新链路解析状态映射（parse_tasks.status → 中文；user_file 只有 正常/已删除）
_STATUS_MAP = {"pending": "处理中", "parsing": "处理中", "done": "已入库", "failed": "失败"}


def _parse_status(s: str | None) -> str:
    return _STATUS_MAP.get(s or "", "处理中")


@mcp.tool()
def search_knowledge(query: str, top_k: int = 8, user_id: int | None = None) -> str:
    """在文档知识库中检索与查询相关的内容片段。

    Args:
        query: 检索查询文本
        top_k: 返回结果数量（默认 8）
        user_id: 用户 ID（多租户隔离；不传则全局访问）
    Returns:
        JSON 格式的检索结果（含来源文档、页码、相关度分数）
    """
    from retrieval.retriever import retrieve
    chunks = retrieve(user_id, query, top_k=top_k)
    return json.dumps({
        "hits": [{"chunk_id": c.chunk_id, "filename": c.filename, "doc_name": c.filename,
                  "page_no": c.page_no, "chunk_type": c.chunk_type, "score": c.score,
                  "content": c.content[:600]} for c in chunks],
        "low_confidence": bool(chunks) and not chunks[0].reranked,
    }, ensure_ascii=False)


@mcp.tool()
def ask_knowledge(query: str, user_id: int | None = None) -> str:
    """向文档知识库提问，得到带引用标注的完整回答（走完整问答管线）。

    Args:
        query: 用户问题
        user_id: 用户 ID（多租户隔离；不传则全局访问）
    Returns:
        JSON：answer（带 [n] 引用标记）+ citations（来源文档/页码）
    """
    from agent.qa_service import ask
    r = ask(query, user_id=user_id)
    return json.dumps({
        "answer": r["answer"],
        "citations": r["citations"],
        "refused": r.get("refused", False),
    }, ensure_ascii=False)


@mcp.tool()
def list_documents(user_id: int | None = None) -> str:
    """列出知识库中的文档及其状态（新链路：user_file + parse_tasks）。

    Args:
        user_id: 用户 ID（多租户过滤；不传则列出全部）
    """
    from db import pg_store
    sql = ("""SELECT uf.id, uf.filename, uf.file_size, uf.created_at,
                      pt.status AS parse_status, pt.stage, pt.error,
                      (SELECT count(*) FROM rag_chunk rc WHERE rc.file_id=uf.id) AS chunk_count
               FROM user_file uf
               LEFT JOIN parse_tasks pt ON pt.file_id=uf.id
               WHERE uf.status=1""")
    params = []
    if user_id is not None:
        sql += " AND uf.user_id=%s"
        params.append(user_id)
    sql += " ORDER BY uf.created_at DESC"
    rows = pg_store.query(sql, tuple(params) or None)
    docs = [{
        "id": d["id"],
        "filename": d["filename"],
        "status": _parse_status(d["parse_status"]),
        "stage": d.get("stage"),
        "error": d.get("error"),
        "chunk_count": d["chunk_count"],
        "file_size": d["file_size"],
        "created_at": str(d["created_at"]),
    } for d in rows]
    return json.dumps(docs, ensure_ascii=False, default=str)


@mcp.tool()
def document_status(doc_id: int) -> str:
    """查询单个文档的入库状态与统计（新链路：user_file + parse_tasks）。

    Args:
        doc_id: 文档 ID
    """
    from db import pg_store
    row = pg_store.query_one(
        """SELECT uf.id, uf.filename, uf.file_size, uf.created_at,
                  pt.status AS parse_status, pt.stage, pt.progress, pt.error, pt.duration_ms,
                  (SELECT count(*) FROM rag_chunk rc WHERE rc.file_id=uf.id) AS chunk_count
           FROM user_file uf
           LEFT JOIN parse_tasks pt ON pt.file_id=uf.id
           WHERE uf.id=%s AND uf.status=1""",
        (doc_id,))
    if not row:
        return json.dumps({"error": f"文档 {doc_id} 不存在"}, ensure_ascii=False)
    doc = {
        "id": row["id"],
        "filename": row["filename"],
        "status": _parse_status(row["parse_status"]),
        "stage": row.get("stage"),
        "progress": row.get("progress"),
        "error": row.get("error"),
        "chunk_count": row["chunk_count"],
        "file_size": row["file_size"],
        "created_at": str(row["created_at"]),
    }
    return json.dumps(doc, ensure_ascii=False, default=str)


if __name__ == "__main__":
    if "--sse" in sys.argv:
        mcp.run(transport="sse", host="127.0.0.1", port=8091)
    else:
        mcp.run(transport="stdio")
