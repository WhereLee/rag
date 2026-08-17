"""
Function Calling 工具系统：定义工具 schema + 执行框架。

工具集：
- list_documents：列出用户文档
- delete_document：删除文档（需要用户确认）
- get_token_usage：查看 token 预算使用情况
- get_document_chunks：预览文档分块内容

设计原则：
- 工具 schema 遵循 OpenAI function calling 格式
- 执行器统一入口，按 tool name 分发
- 所有工具接收 user_id 参数（多租户隔离）
- 错误统一返回 JSON 格式，不抛异常
"""
import json
import logging

logger = logging.getLogger("rag.tools")

# ===== 工具 Schema 定义 =====

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_documents",
            "description": "列出当前用户的所有文档，包括文件名、类型、状态、分块数和入库时间",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_document",
            "description": "删除指定文档。删除前请向用户确认文档名称。",
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {
                        "type": "integer",
                        "description": "要删除的文档 ID"
                    }
                },
                "required": ["document_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_token_usage",
            "description": "查看当前用户今日的 token 使用情况，包括已用量、剩余量和限额",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_document_chunks",
            "description": "预览指定文档的分块内容（最多 20 块），用于了解文档包含什么信息",
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {
                        "type": "integer",
                        "description": "文档 ID"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多返回多少块，默认 10",
                        "default": 10
                    }
                },
                "required": ["document_id"]
            }
        }
    },
]


# ===== 工具执行器 =====

def execute_tool(name: str, arguments: dict, user_id: int | None = None) -> str:
    """统一工具执行入口。返回 JSON 字符串结果。

    Args:
        name: 工具名称
        arguments: 工具参数（已解析为 dict）
        user_id: 当前用户 ID（多租户隔离）

    Returns:
        JSON 字符串结果
    """
    try:
        if name == "list_documents":
            return _exec_list_documents(user_id)
        elif name == "delete_document":
            return _exec_delete_document(arguments.get("document_id"), user_id)
        elif name == "get_token_usage":
            return _exec_get_token_usage(user_id)
        elif name == "get_document_chunks":
            return _exec_get_document_chunks(
                arguments.get("document_id"), arguments.get("limit", 10), user_id)
        else:
            return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)
    except Exception as e:
        logger.error("Tool execution failed: %s(%s) -> %s", name, arguments, e)
        return json.dumps({"error": f"工具执行失败: {str(e)[:200]}"}, ensure_ascii=False)


def _exec_list_documents(user_id: int | None) -> str:
    from ingest.sync_service import list_documents
    docs = list_documents(user_id=user_id)
    result = []
    for d in docs:
        result.append({
            "id": d["id"],
            "filename": d["filename"],
            "doc_type": d["doc_type"],
            "status": {0: "处理中", 1: "已入库", 2: "失败", 3: "已下线"}.get(d["status"], "未知"),
            "chunk_count": d.get("chunk_count", 0),
            "created_at": str(d.get("created_at", "")),
        })
    return json.dumps({"documents": result, "count": len(result)}, ensure_ascii=False)


def _exec_delete_document(doc_id: int | None, user_id: int | None) -> str:
    if doc_id is None:
        return json.dumps({"error": "缺少 document_id 参数"}, ensure_ascii=False)
    from ingest.sync_service import delete_document
    try:
        result = delete_document(doc_id, user_id=user_id)
        return json.dumps({"success": True, "message": f"文档 {doc_id} 已删除", **result},
                          ensure_ascii=False)
    except ValueError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def _exec_get_token_usage(user_id: int | None) -> str:
    from agent.token_budget import get_usage
    usage = get_usage(user_id)
    return json.dumps(usage, ensure_ascii=False)


def _exec_get_document_chunks(doc_id: int | None, limit: int, user_id: int | None) -> str:
    if doc_id is None:
        return json.dumps({"error": "缺少 document_id 参数"}, ensure_ascii=False)
    from db import pg_store
    # 权限校验
    if user_id is not None:
        ownership = pg_store.query_one(
            "SELECT id FROM kb_user_document WHERE user_id=%s AND document_id=%s",
            (user_id, doc_id))
        if not ownership:
            return json.dumps({"error": "无权访问该文档"}, ensure_ascii=False)
    doc = pg_store.query_one(
        "SELECT id, filename, doc_type, page_count FROM kb_document WHERE id=%s",
        (doc_id,))
    if not doc:
        return json.dumps({"error": f"文档 {doc_id} 不存在"}, ensure_ascii=False)
    chunks = pg_store.query(
        """SELECT id, chunk_type, page_no, seq, content
           FROM kb_chunk WHERE document_id=%s AND status=1
           ORDER BY seq LIMIT %s""",
        (doc_id, min(limit, 20)))
    result = {
        "document": {"id": doc["id"], "filename": doc["filename"],
                     "doc_type": doc["doc_type"], "page_count": doc["page_count"]},
        "chunks": [{"page": c["page_no"] + 1, "seq": c["seq"],
                    "type": c["chunk_type"], "content": c["content"][:500]}
                   for c in chunks],
        "count": len(chunks)
    }
    return json.dumps(result, ensure_ascii=False)
