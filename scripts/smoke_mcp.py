"""MCP server 冒烟：stdio client 连接并调用工具。"""
import asyncio
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "rag-python" / "src"


async def main():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-X", "utf8", "-m", "mcp_server.server"],
        cwd=str(SRC),
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            names = [t.name for t in tools.tools]
            print("tools:", names)
            assert "search_knowledge" in names and "ask_knowledge" in names
            out = await s.call_tool("list_documents", {})
            print("list_documents ok, len:", len(out.content[0].text))
            out = await s.call_tool("search_knowledge",
                                    {"query": "白皮书调研企业数量", "top_k": 3})
            print("search_knowledge:", out.content[0].text[:200])
            print("MCP_SMOKE_OK")


asyncio.run(main())
