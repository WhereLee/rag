"""
MiMo-V2.5 视觉解析：页面图片 → 结构化 markdown。

用途：
- 通道B：表格区域图 → markdown 表格
- 通道C：扫描页整页 → 结构化文本
- 图片通道：独立图片 → 语义描述 + 文字抽取

成本控制：调用结果按图片内容 hash 缓存落盘（data/parsed/vlm_cache）。
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


def _call_vlm(prompt: str, png_bytes: bytes, cache_prefix: str) -> dict:
    key = cache_prefix + "_" + hashlib.sha256(png_bytes).hexdigest()[:32]
    cached = _cache_get(key)
    if cached is not None:
        return cached
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": _img_to_data_url(png_bytes)}},
        ],
    }]
    result = get_client().chat_json(messages, thinking=True, max_tokens=8192)
    _cache_put(key, result)
    logger.info("vlm call: %s, in=%d out=%d", cache_prefix,
                0, 0)  # token 在 chat_json 内未回传，后续可观测层补齐
    return result


def parse_table_region(png_bytes: bytes) -> str | None:
    """表格区域 → markdown 表格；无表格返回 None。"""
    obj = _call_vlm(TABLE_PROMPT, png_bytes, "tbl")
    if obj.get("no_table"):
        return None
    md = (obj.get("markdown") or "").strip()
    return md or None


def parse_scanned_page(png_bytes: bytes) -> str:
    """扫描页 → 转录文本。失败抛 LLMError（上层降级）。"""
    obj = _call_vlm(PAGE_PROMPT, png_bytes, "scan")
    return (obj.get("text") or "").strip()


def parse_image(png_bytes: bytes) -> dict:
    """独立图片 → {description, text_in_image}。"""
    obj = _call_vlm(IMAGE_PROMPT, png_bytes, "img")
    return {
        "description": (obj.get("description") or "").strip(),
        "text_in_image": (obj.get("text_in_image") or "").strip(),
    }
