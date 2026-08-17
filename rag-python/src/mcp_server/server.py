"""
MCP Server（对外集成定位）：知识库工具暴露给任意外部 MCP Client。

设计决策（架构文档 §8.3）：项目内部 Agent 直调函数，不走 MCP；
MCP 的价值是"任何兼容 Client（IDE Agent 等）可直接接入本知识库"。

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


@mcp.tool()
def search_knowledge(query: str, top_k: int = 8) -> str:
    """在文档知识库中检索与查询相关的内容片段。

    Args:
        query: 检索查询文本
        top_k: 返回结果数量（默认 8）
    Returns:
        JSON 格式的检索结果（含来源文档、页码、相关度分数）
    """
    from retrieval.hybrid import hybrid_search
    result = hybrid_search(query, top_k=top_k)
    return json.dumps({
        "hits": [{**h, "content": h["content"][:600]} for h in result["hits"]],
        "low_confidence": result["low_confidence"],
    }, ensure_ascii=False)


@mcp.tool()
def ask_knowledge(query: str) -> str:
    """向文档知识库提问，得到带引用标注的完整回答（走完整问答管线）。

    Args:
        query: 用户问题
    Returns:
        JSON：answer（带 [n] 引用标记）+ citations（来源文档/页码）
    """
    from agent.qa_service import ask
    r = ask(query)
    return json.dumps({
        "answer": r["answer"],
        "citations": r["citations"],
        "refused": r.get("refused", False),
    }, ensure_ascii=False)


@mcp.tool()
def list_documents() -> str:
    """列出知识库中已入库的全部文档及其状态。"""
    from ingest.sync_service import list_documents as do_list
    docs = do_list()
    for d in docs:
        d["created_at"] = str(d["created_at"])
    return json.dumps(docs, ensure_ascii=False, default=str)


@mcp.tool()
def document_status(doc_id: int) -> str:
    """查询单个文档的入库状态与统计。

    Args:
        doc_id: 文档 ID
    """
    from ingest.sync_service import document_status as do_status
    doc = do_status(doc_id)
    if not doc:
        return json.dumps({"error": f"文档 {doc_id} 不存在"}, ensure_ascii=False)
    doc["created_at"] = str(doc["created_at"])
    return json.dumps(doc, ensure_ascii=False, default=str)


if __name__ == "__main__":
    if "--sse" in sys.argv:
        mcp.run(transport="sse", host="127.0.0.1", port=8091)
    else:
        mcp.run(transport="stdio")
