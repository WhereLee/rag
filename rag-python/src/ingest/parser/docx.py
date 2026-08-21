"""docx 解析器：段落（样式层级）→ heading/paragraph，表格 → Markdown 表格，内嵌图 → VLM。

- 顺序：按 body 子元素（w:p / w:tbl）真实文档顺序遍历，图片随所在段落原位输出
- 标题层级：内置 Heading 1-3 → heading（level 1-3）；自定义样式按 outlineLvl（未设 outline 的样式视为正文）
- 图片：w:drawing 内 a:blip 的 rId 取 blob → 统一转 PNG → VLM 描述；失败占位不中断
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from .base import DocumentNode, Parser, rows_to_markdown
from .vlm import VLMClient, to_png_bytes

logger = logging.getLogger("rag.docx")

IMAGE_PROMPT = "你是文档解析器。图片是 Word 文档中的插图（架构图/图表/照片等）。请输出 JSON：{\"description\": \"2-4 句话描述图片内容与关键信息（含图中数字/标签）\", \"text_in_image\": \"图中出现的文字，按出现顺序换行分隔；没有则为空串\"}"


def iter_block_items(doc: Document):
    """按 body 顺序产出 Paragraph / Table（python-docx 官方示例）。"""
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, doc)
        elif child.tag == qn("w:tbl"):
            yield Table(child, doc)


def _heading_level(para: Paragraph) -> Optional[int]:
    """内置 Heading N → N；自定义样式按 outlineLvl（1 起）。"""
    style = para.style
    if style is not None:
        name = (style.name or "")
        if name.startswith("Heading"):
            for i in range(1, 10):
                if name == f"Heading {i}":
                    return i
            return 1
    pPr = para._p.pPr
    if pPr is not None and pPr.outlineLvl is not None:
        return pPr.outlineLvl.val + 1
    return None


def _drawing_images(para: Paragraph, doc: Document) -> List[bytes]:
    """段落内 w:drawing 图片 blob（含浮动图，行内/浮动都在 w:p 下）。"""
    out: List[bytes] = []
    for drawing in para._p.findall(".//" + qn("w:drawing")):
        for blip in drawing.findall(".//" + qn("a:blip")):
            embed = blip.get(qn("r:embed"))
            if embed:
                part = doc.part.related_parts[embed]
                if part.content_type.startswith("image/"):
                    out.append(part.blob)
    return out


def _table_rows(table: Table) -> List[List[str]]:
    rows: List[List[str]] = []
    for row in table.rows:
        rows.append([cell.text.strip() for cell in row.cells])
    return rows


class DocxParser(Parser):
    """Word 解析：段落/标题/表格/内嵌图，输出 DocumentNode 列表。"""

    def __init__(self, vlm: Optional[VLMClient] = None) -> None:
        self.vlm = vlm

    def parse(self, path: Path) -> List[DocumentNode]:
        path = Path(path)
        doc = Document(path)
        source = path.name
        nodes: List[DocumentNode] = []
        for item in iter_block_items(doc):
            if isinstance(item, Paragraph):
                text = item.text.strip()
                level = _heading_level(item)
                if text:
                    if level:
                        nodes.append(DocumentNode("heading", text,
                                                  {"source": source, "level": level, "index": len(nodes)}))
                    else:
                        nodes.append(DocumentNode("paragraph", text,
                                                  {"source": source, "index": len(nodes)}))
                for blob in _drawing_images(item, doc):
                    nodes.append(DocumentNode("image", self._describe_image(blob, source),
                                              {"source": source, "index": len(nodes)}))
            elif isinstance(item, Table):
                md = rows_to_markdown(_table_rows(item))
                if md.strip("| -"):
                    nodes.append(DocumentNode("table", md,
                                              {"source": source, "index": len(nodes)}))
        return nodes

    def _describe_image(self, blob: bytes, source: str) -> str:
        if self.vlm is None:
            from .pdf import _default_vlm
            self.vlm = _default_vlm()
        try:
            png = to_png_bytes(blob)
            obj = self.vlm.chat_image(IMAGE_PROMPT, png, "docx_img")
            desc = (obj.get("description") or "").strip()
            text_in = (obj.get("text_in_image") or "").strip()
            parts = [desc] + ([text_in] if text_in else [])
            return "；".join(p for p in parts if p) or "[图片无法解析]"
        except Exception as e:
            logger.warning("docx image vlm failed: %s: %s", source, e)
            return "[图片解析失败]"
