"""
失败分类与文案映射（第二轮 Step3，方案 §4.4 + 审查修订 R5）。

原则：把"原始异常"翻译成"可决策的分类 + 人话化文案"，让任务状态决策和用户提示都有依据。

分类体系（kind）与处置语义：已与 job 状态机对齐
  unfixable_file : 文件自身缺陷（损坏图像流等），重试无效 → 不消耗重试额度
  retriable      : 网络/限流/超时，重试有意义
  discard_block  : 内容校验失败（VLM 输出不合法），丢弃该块/页，不影响其他内容
  unclassified   : 未知异常（兜底：宁可重试一次也不冤枉文件，同时记录供观测学习）

对外文案保持人话，避免把 `code=4: Invalid bandwriter...` 这类工程师黑话直接甩给用户。
"""
import logging

logger = logging.getLogger("rag.errors")

# 已知文件缺陷的关键字特征（按消息匹配，宁少勿误：匹配到才判文件缺陷）
_FILE_DEFECT_MARKERS = (
    "bandwriter",
    "Invalid bandwriter header",
    "cannot read xref",
    "syntax error",
    "jbig2dec",      # JBIG2 解码失败（扫描件压缩流损坏）
    "unsupported filter",   # 压缩滤镜不受支持/损坏
    "damage"         # 触发防损坏恢复的提示
)

# 可重试的网络/服务端异常
_RETRIABLE_TYPES = ("httpx.ConnectError", "httpx.ReadTimeout", "httpx.ConnectTimeout",
                    "httpx.HTTPStatusError")

def _summary(msg: str, limit: int = 120) -> str:
    msg = (msg or "").strip().replace("\n", " ").replace("\r", " ")
    return msg[:limit] + ("…" if len(msg) > limit else "")

def classify_exception(exc: Exception) -> dict:
    """分类异常并生成人话文案。返回 {"kind","message","detail"}。"""
    cls = type(exc).__name__
    module = type(exc).__module__ or ""
    msg = str(exc) or cls
    low = msg.lower()

    # 1) 文件缺陷（PyMuPDF 渲染/解码失败等）
    if "pymupdf" in module or isinstance(exc, Exception) and any(m in low for m in _FILE_DEFECT_MARKERS):
        return {
            "kind": "unfixable_file",
            "message": f"该文件内部的图片数据损坏或格式不规范，相关部分已跳过（详情：{_summary(msg)}）。请尝试重新导出或修复后再上传。",
            "detail": _summary(msg, 300),
        }

    # 2) 内容校验失败（VLM 输出不合法）
    if cls == "VLMValidationError" or "validation" in cls.lower() or "ValueError" == cls:
        return {
            "kind": "discard_block",
            "message": f"识别结果异常，该部分内容已丢弃（详情：{_summary(msg)}）。不影响其余正常内容。",
            "detail": _summary(msg, 300),
        }

    # 3) 网络/服务端错误（重试有意义）
    if cls in _RETRIABLE_TYPES or cls == "LLMError" or "timeout" in low or "429" in str(msg):
        return {
            "kind": "retriable",
            "message": f"识别服务暂不可用，稍后将自动重试（详情：{_summary(msg)}）。",
            "detail": _summary(msg, 300),
        }

    # 4) 兜底
    logger.warning("unclassified exception: %s: %s", cls, msg)
    return {
        "kind": "unclassified",
        "message": f"解析出现异常，已记录（详情：{_summary(msg)}）。可手动重试。",
        "detail": _summary(msg, 300),
    }