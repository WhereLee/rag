"""AgentState：LangGraph 主图状态定义。"""
from typing import Annotated, TypedDict

from langgraph.graph import add_messages


class AgentState(TypedDict):
    # 消息流（LangGraph 管理，用于 HITL 与回放）
    messages: Annotated[list, add_messages]
    # 输入
    query: str
    session_id: str
    history: list          # [{role, content}] 最近轮次
    # 路由与检索
    route: str             # simple/standard/complex/out_of_scope
    search_queries: list   # 实际执行的检索查询（拆解后）
    retrieval_round: int   # 已重检索次数（CRAG 上限 2）
    hits: list             # [{chunk_id, content, doc_name, page_no, score}]
    low_confidence: bool
    grade: dict            # {sufficient, missing}
    # 生成与反思
    answer: str
    reflection: dict       # {faithfulness, relevancy, passed, feedback}
    reflect_retry: int
    # 观测
    trace_id: str
    stages: list           # [{stage, ms, extra}]
    token_in: int
    token_out: int
    final_route: str       # 实际执行路径标签（日志用）
