"""测试 Adaptive RAG + Function Calling 新功能。"""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rag-python", "src"))

from agent.tools import execute_tool, TOOL_SCHEMAS

print("=== Tool System Test ===")

# 1. Test list_documents
result = execute_tool("list_documents", {}, user_id=10)
data = json.loads(result)
print(f"list_documents: {data['count']} docs")
for d in data["documents"][:3]:
    print(f"  [{d['id']}] {d['filename']} ({d['status']}, {d['chunk_count']} chunks)")

# 2. Test get_token_usage
result = execute_tool("get_token_usage", {}, user_id=10)
data = json.loads(result)
print(f"\nget_token_usage: used={data['used']}, limit={data['limit']}, remaining={data['remaining']}")

# 3. Test get_document_chunks (if we have docs)
if data.get("remaining", 0) >= 0:  # always true, just to use data
    docs_result = json.loads(execute_tool("list_documents", {}, user_id=10))
    if docs_result["count"] > 0:
        doc_id = docs_result["documents"][0]["id"]
        result = execute_tool("get_document_chunks", {"document_id": doc_id, "limit": 3}, user_id=10)
        chunks_data = json.loads(result)
        print(f"\nget_document_chunks for doc {doc_id}: {chunks_data['count']} chunks")
        for c in chunks_data["chunks"][:2]:
            print(f"  p.{c['page']} seq.{c['seq']}: {c['content'][:80]}...")

# 4. Tool schemas summary
print(f"\n=== {len(TOOL_SCHEMAS)} Tool Schemas ===")
for t in TOOL_SCHEMAS:
    fn = t["function"]
    print(f"  {fn['name']}: {fn['description'][:60]}")

# 5. Graph structure test
print("\n=== Graph Structure Test ===")
from agent.main_graph import _build_graph
g = _build_graph()
nodes = list(g.nodes.keys())
print(f"Graph: {len(nodes)} nodes: {nodes}")

# 6. MiMo chat_with_tools test
print("\n=== MiMo Function Calling Test ===")
from llm.mimo_client import get_client
client = get_client()
result = client.chat_with_tools(
    [{"role": "user", "content": "列出我的文档"}],
    tools=TOOL_SCHEMAS,
    thinking=False, max_tokens=512)
print(f"tool_calls: {len(result['tool_calls'])}")
for tc in result["tool_calls"]:
    print(f"  -> {tc['name']}({tc['arguments']})")
print(f"content: {result['content'][:100] if result['content'] else '(empty)'}")
print(f"tokens: in={result['token_in']} out={result['token_out']} elapsed={result['elapsed_ms']}ms")
