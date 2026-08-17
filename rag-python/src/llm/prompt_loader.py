"""
Prompt 加载器：优先读 prompt_registry 表（支持审批生效的版本），缺失时用内置默认。

所有提示词集中在此文件定义默认值，Phase 5 的审批流操作的就是这些 code。
"""
import logging
from db import pg_store

logger = logging.getLogger("rag.prompt")

DEFAULT_PROMPTS = {
    "generate": """你是企业级文档问答助手。请严格依据【参考资料】回答用户问题。
要求：
1. 只使用参考资料中的信息，不得编造；资料不足以回答时明确说明"根据现有文档未找到相关信息"
2. 回答要直接、结构化，涉及数字/日期/名称时必须与资料完全一致
3. 在引用的关键句末尾用 [n] 标注来源序号（对应资料编号）
4. 使用与问题相同的语言回答

【参考资料】
{context}

【用户问题】
{question}""",

    "route": """你是文档问答系统的路由分类器。判断用户问题的"形态"，输出 JSON：{"route": "<类型>"}
类型定义：
- simple：简单事实查询（单一事实/定义/数字，一次检索可答）
- standard：常规问题（需要综合 1-2 段资料）
- complex：复杂问题（跨文档综合、多步推理、对比分析、列举多项内容、需要拆解的复合问题）
- out_of_scope：与知识库明显无关——闲聊、写作创作、天气/价格/汇率等实时外部信息
下面提供该问题检索命中的 top1 片段作为先验参考：
【检索先验】
{prior}
判断原则：
- 问题涉及白皮书/规范/案例/系统/档案等具体名词时，很可能在知识库中，按形态归类（simple/standard/complex），不要判 out_of_scope
- 仅当问题明确属于闲聊、创作、实时外部事实时才判 out_of_scope
- 拒答由后续检索阈值与生成层兜底，路由不做内容相关性裁决

【对话历史】
{history}

【用户问题】
{question}""",

    "rewrite": """你是查询改写器。把用户问题改写为更适合检索的形式：
- 补全省略的指代（结合对话历史）
- 拆分复合意图为关键词组合
只输出改写后的查询文本，不要解释。

【对话历史】
{history}

【用户问题】
{question}""",

    "decompose": """把复杂问题拆解为 2-3 个可独立检索的子问题。
要求：
- 每个子问题自含完整语义（不依赖上下文即可检索）
- 子问题合起来能覆盖原问题的全部信息需求
输出 JSON：{"sub_queries": ["...", "..."]}

【原问题】
{question}""",

    "grade": """评估以下检索结果能否支撑回答用户问题。
输出 JSON：{"sufficient": true/false, "missing": "<若不足，缺少什么信息（ sufficient=true 时为空串）>"}

【用户问题】
{question}

【检索结果】
{context}""",

    "reflect": """你是答案质检员。检查回答的质量，输出 JSON：
{"faithfulness": 0-1, "relevancy": 0-1, "passed": true/false, "feedback": "<不通过的原因与修正方向>"}
- faithfulness：回答是否完全基于参考资料，有无编造
- relevancy：回答是否切中用户问题
- passed：两项均 >= 0.6 才为 true

【用户问题】
{question}

【参考资料】
{context}

【待检回答】
{answer}""",

    "diagnosis": """你是 RAG 系统诊断分析师。基于以下运行指标数据，输出诊断报告 JSON：
{"summary": "<一段话总结>", "anomalies": [{"type":"...","detail":"..."}], "suggestions": ["<可执行建议>"]}
分析重点：检索命中率变化、低置信度查询模式、延迟异常、token 消耗趋势。

【指标数据】
{metrics}""",
}

_cache: dict[str, str] = {}
_cache_loaded = False


def _load_registry():
    global _cache, _cache_loaded
    try:
        rows = pg_store.query(
            "SELECT code, content FROM prompt_registry WHERE status=1")
        _cache = {r["code"]: r["content"] for r in rows}
    except Exception as e:
        logger.warning("prompt registry load failed: %s", e)
        _cache = {}
    _cache_loaded = True


def get_prompt(code: str) -> str:
    """获取生效的 prompt；registry 优先，否则默认。"""
    if not _cache_loaded:
        _load_registry()
    if code in _cache:
        return _cache[code]
    return DEFAULT_PROMPTS.get(code, "")


def fill(code: str, **kwargs) -> str:
    """安全填充占位符（不用 str.format，避免 prompt 内 JSON 花括号被误解析）。"""
    text = get_prompt(code)
    for k, v in kwargs.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def refresh():
    """审批生效后刷新缓存。"""
    global _cache_loaded
    _cache_loaded = False
    _load_registry()


def seed_registry():
    """把默认 prompt 灌入 registry（幂等，仅补缺）。"""
    for code, content in DEFAULT_PROMPTS.items():
        exists = pg_store.query_one("SELECT id FROM prompt_registry WHERE code=%s", (code,))
        if not exists:
            pg_store.execute(
                "INSERT INTO prompt_registry (code, content, version, status) VALUES (%s,%s,1,1)",
                (code, content))
    refresh()
