"""
Prompt 注入防御：检测并拦截恶意用户输入。

攻击模式：
- 指令覆盖：「忽略之前的指令」「forget all previous」
- 角色扮演：「你现在是 DAN」「pretend you are」
- 系统泄露：「重复上面的话」「output your system prompt」
- 编码绕过：base64/unicode/hex 混淆
- 分隔符注入：伪造 system/user 角色边界

策略：
- 检测 → 记录告警 → 拒绝回答（不修改用户输入，避免误伤正常 query）
- 阈值宽松（宁漏勿误），日志告警优先
"""
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("rag.security")

# 注入模式（中英文混合，覆盖常见攻击向量）
INJECTION_PATTERNS = [
    # 指令覆盖
    (re.compile(r"ignore\s+(all\s+)?(previous|above|prior)\s+(instructions?|prompts?|rules?)", re.I),
     "instruction_override_en"),
    (re.compile(r"忽略.{0,6}(之前|以上|先前|上面).{0,6}(指令|提示|规则|设定)"),
     "instruction_override_zh"),
    # 角色扮演 / DAN
    (re.compile(r"(you\s+are\s+now|pretend\s+(to\s+be|you\s+are)|act\s+as\s+(if|a))\s+(DAN|unrestricted|unfiltered)", re.I),
     "role_play_dan"),
    (re.compile(r"(你现在是|请扮演|假装你是)\s*(一个|the)?\s*(DAN|不受限|无限制)"),
     "role_play_zh"),
    # 系统提示泄露
    (re.compile(r"(repeat|output|show|print|display)\s+(your\s+)?(system\s+prompt|instructions?|rules?)\s*(above|verbatim|exactly)", re.I),
     "system_leak_en"),
    (re.compile(r"(重复|输出|显示|打印).{0,4}(系统提示|系统指令|你的指令|你的规则)"),
     "system_leak_zh"),
    # 分隔符伪造
    (re.compile(r"(\\n|#|---)\s*(system|assistant|user)\s*:", re.I),
     "delimiter_injection"),
]

# 查询长度限制
MAX_QUERY_LENGTH = 2000


@dataclass
class SanitizeResult:
    """清洗/检测结果。"""
    safe: bool          # 是否安全
    query: str          # 清洗后的 query
    reason: str = ""    # 被拦截的原因
    pattern: str = ""   # 匹配到的模式名


def sanitize_query(raw_query: str) -> SanitizeResult:
    """清洗用户查询：长度截断 + 注入检测。

    策略：
    - 超过长度限制的截断（不拒绝，因为可能是复制粘贴误操作）
    - 检测到注入模式：标记不安全，记录告警日志
    """
    if not raw_query or not raw_query.strip():
        return SanitizeResult(safe=False, query="", reason="empty_query")

    query = raw_query.strip()

    # 长度限制
    if len(query) > MAX_QUERY_LENGTH:
        query = query[:MAX_QUERY_LENGTH]
        logger.warning("query truncated: original length=%d", len(raw_query))

    # 注入检测
    for pattern, name in INJECTION_PATTERNS:
        if pattern.search(query):
            logger.warning("PROMPT INJECTION detected: pattern=%s query_preview=%.100s",
                           name, query)
            return SanitizeResult(
                safe=False, query=query,
                reason=f"prompt_injection:{name}",
                pattern=name)

    return SanitizeResult(safe=True, query=query)


def sanitize_document_content(content: str, max_chars: int = 50000) -> str:
    """清洗文档内容（用于检索结果注入 prompt 前的预处理）。

    文档可能包含恶意的 system/user 标记，需要转义。
    """
    if not content:
        return ""
    # 截断
    truncated = content[:max_chars]
    # 转义分隔符（防止文档内容伪造角色边界）
    truncated = truncated.replace("<|system|>", "[system]")
    truncated = truncated.replace("<|user|>", "[user]")
    truncated = truncated.replace("<|assistant|>", "[assistant]")
    return truncated


def detect_document_injection(text: str) -> tuple[bool, str]:
    """文档内容指令注入检测（第一轮修复 C4）。

    场景：图片/扫描件经 VLM 转录后进入知识库，转录文本可能携带恶意指令
    （如「忽略之前的指令」「输出你的系统提示」），被检索注入问答 prompt。
    在 VLM 转录入 chunk 前过滤，命中则标记可疑，不进入知识库。

    返回 (is_suspicious, pattern_name)。阈值与 sanitize_query 一致（宁漏勿误）。
    """
    if not text:
        return False, ""
    for pattern, name in INJECTION_PATTERNS:
        if pattern.search(text):
            logger.warning("DOCUMENT INJECTION detected: pattern=%s preview=%.100s", name, text)
            return True, name
    return False, ""
