"""独立图片解析器：png/jpg/jpeg/webp → VLM 语义描述（description + text_in_image）。

- 大图由 to_png_bytes 统一等比缩放至 VLM 输入上限
- VLM 失败 → 占位文本，不抛错（降级链在 R5 统计失败率）
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from .base import DocumentNode, Parser
from .vlm import VLMClient, to_png_bytes

logger = logging.getLogger("rag.image")

IMAGE_PROMPT = """你是图片分析器。请输出 JSON：
1. description：2-4 句话描述图片内容（图是什么、关键数字/趋势/文字结论）
2. text_in_image：图中出现的所有文字，按出现顺序换行分隔；没有则为空串
输出 JSON：{"description": "...", "text_in_image": "..."}"""


class ImageParser(Parser):
    """独立图片 → image 节点（text 为 VLM 描述）。"""

    def __init__(self, vlm: Optional[VLMClient] = None) -> None:
        self.vlm = vlm

    def parse(self, path: Path) -> List[DocumentNode]:
        path = Path(path)
        if self.vlm is None:
            from .pdf import _default_vlm
            self.vlm = _default_vlm()
        try:
            png = to_png_bytes(path.read_bytes())
            obj = self.vlm.chat_image(IMAGE_PROMPT, png, "img")
            desc = (obj.get("description") or "").strip()
            text_in = (obj.get("text_in_image") or "").strip()
            parts = [desc] + ([text_in] if text_in else [])
            text = "；".join(p for p in parts if p) or "[图片无法解析]"
        except Exception as e:
            logger.warning("image parse failed: %s: %s", path.name, e)
            text = "[图片解析失败]"
        return [DocumentNode("image", text, {"source": path.name, "index": 0})]
