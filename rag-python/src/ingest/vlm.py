"""
MiMo-V2.5 视觉解析：页面图片 → 结构化 markdown。

用途：
- 通道B：表格区域图 → markdown 表格
- 通道C：扫描页整页 → 结构化文本
- 图片通道：独立图片 → 语义描述 + 文字抽取

成本控制：调用结果按图片内容 hash 缓存落盘（data/parsed/vlm_cache）。
可观测（第一轮修复）：每次调用回传 token/耗时（meta），由调用方落 step_detail/统计。
可靠性（第一轮修复）：输出内容校验 —— VLM 未抛错但返回坏内容（幻觉表格/空转录）时
显式标记失败，不产生脏数据。
"""
import base64
import hashlib
import json
import logging
from pathlib import Path

import config
from llm.mimo_client import get_client, LLMError

logger = logging.getLogger("rag.vlm")

VLM_CACHE_DIR = config.PARSED_DIR / "vlm_cache"
VLM_CACHE_DIR.mkdir(parents=True, exist_ok=True)


class VLMValidationError(ValueError):
    """VLM 返回内容未通过合理性校验（非网络错误，是"坏内容"）。"""


TABLE_PROMPT = """你是文档解析器。图片是文档中的一个区域，可能包含表格。
请把表格转换为 markdown 格式输出，要求：
1. 保留全部行列，不要省略、不要翻译、不要改写单元格文字
2. 合并单元格按展开处理（值填入对应位置）
3. 如果区域中没有表格，输出 {"no_table": true}
输出 JSON：{"markdown": "<表格markdown>", "no_table": false}"""

PAGE_PROMPT = """你是文档解析器。图片是一页扫描文档。请把页面内容完整转录为结构化文本，要求：
1. 保留标题层级（用 # ## 标记）、段落、列表结构
2. 表格转为 markdown 格式
3. 忠实转录，不添加、不改写、不遗漏数字与日期
输出 JSON：{"text": "<转录全文>"}"""

IMAGE_PROMPT = """你是文档解析器。图片是文档配图（架构图/图表/示意图等）。请输出：
1. description：用 2-4 句话描述图片表达的内容与结论（含图中关键数字/标签）
2. text_in_image：图中出现的所有文字（标签、坐标、标题），按出现顺序用换行分隔；没有则为空串
输出 JSON：{"description": "...", "text_in_image": "..."}"""


def _img_to_data_url(png_bytes: bytes) -> str:
    b64 = base64.b64encode(png_bytes).decode()
    return f"data:image/png;base64,{b64}"


def _cache_get(key: str) -> dict | None:
    f = VLM_CACHE_DIR / f"{key}.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
    return None


def _cache_put(key: str, obj: dict):
    f = VLM_CACHE_DIR / f"{key}.json"
    try:
        f.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        logger.warning("vlm cache write failed: %s", e)


def _call_vlm(prompt: str, png_bytes: bytes, cache_prefix: str) -> tuple[dict, dict]:
    """调用 VLM 并返回 (obj, meta)。缓存命中时 meta 标记 cache_hit（无实时 token）。"""
    key = cache_prefix + "_" + hashlib.sha256(png_bytes).hexdigest()[:32]
    cached = _cache_get(key)
    if cached is not None:
        meta = {"cache_hit": True, "token_in": 0, "token_out": 0, "elapsed_ms": 0}
        return cached, meta
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": _img_to_data_url(png_bytes)}},
        ],
    }]
    obj, meta = get_client().chat_json_verbose(messages, thinking=True, max_tokens=8192)
    meta["cache_hit"] = False
    _cache_put(key, obj)
    logger.info("vlm call: %s, in=%d out=%d elapsed=%dms",
                cache_prefix, meta["token_in"], meta["token_out"], meta["elapsed_ms"])
    return obj, meta


# ---------------------------------------------------------------- 输出校验

def _validate_table_md(md: str) -> str:
    """表格 markdown 合理性校验：必须含表头分隔行、行数不超过 100、无空表。"""
    md = (md or "").strip()
    if not md or "|" not in md:
        raise VLMValidationError("表格输出不含管道符，疑似非表格内容")
    lines = [ln for ln in md.splitlines() if "|" in ln]
    if len(lines) < 2:
        raise VLMValidationError("表格输出行数不足（缺表头/数据行）")
    # 表头分隔行：第二行应形如 |---|---|
    sep = lines[1]
    if not all(set(ch).issubset("|-: ") and "---" in ch for ch in sep.split("|")[1:-1] if ch.strip()):
        # 宽容：只要 1~2 行含连续 '-' 即视为分隔行
        if "---" not in sep and "---" not in lines[0]:
            raise VLMValidationError("表格输出缺少表头分隔行（---）")
    if len(lines) > 100:
        raise VLMValidationError(f"表格行数异常（{len(lines)} 行），疑似幻觉")
    return md


def _validate_transcript(text: str) -> str:
    """扫描页转录合理性校验：长度下限 + 结构（非单行乱码）。"""
    text = (text or "").strip()
    if len(text) < 5:
        raise VLMValidationError("转录文本为空或过短，疑似空页/识别失败")
    if "\n" not in text and len(text) < 30:
        raise VLMValidationError("转录文本过短且无换行，疑似截断")
    if len(text) > 20000:
        raise VLMValidationError("转录文本超长（>20000 字符），疑似幻觉重复")
    return text


def _validate_image_info(obj: dict) -> dict:
    desc = (obj.get("description") or "").strip()
    if len(desc) < 5:
        raise VLMValidationError("图片描述为空或过短，疑似识别失败")
    return {"description": desc, "text_in_image": (obj.get("text_in_image") or "").strip()}


# ---------------------------------------------------------------- 对外接口（返回 (result, meta)）

def parse_table_region(png_bytes: bytes) -> tuple[str | None, dict]:
    """表格区域 → (markdown 表格 或 None, meta)；无表格返回 (None, meta)；
    校验失败抛 VLMValidationError（调用方按页级失败处理）。"""
    obj, meta = _call_vlm(TABLE_PROMPT, png_bytes, "tbl")
    if obj.get("no_table"):
        return None, meta
    md = _validate_table_md(obj.get("markdown") or "")
    return md, meta


def parse_scanned_page(png_bytes: bytes) -> tuple[str, dict]:
    """扫描页 → (转录文本, meta)。校验失败抛 VLMValidationError。"""
    obj, meta = _call_vlm(PAGE_PROMPT, png_bytes, "scan")
    text = _validate_transcript(obj.get("text") or "")
    return text, meta


def parse_image(png_bytes: bytes) -> tuple[dict, dict]:
    """独立图片 → ({description, text_in_image}, meta)。校验失败抛 VLMValidationError。"""
    obj, meta = _call_vlm(IMAGE_PROMPT, png_bytes, "img")
    return _validate_image_info(obj), meta