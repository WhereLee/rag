"""
Markdown / 图片文件解析：统一产出页级中间表示。

- Markdown：按标题切 section，每个 section 视为一个"页"
- 图片：VLM 语义描述 + 文字抽取
"""
import logging
import re
from pathlib import Path

from ingest import vlm

logger = logging.getLogger("rag.doc_parser")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def parse_docx(path: Path) -> tuple[list[dict], dict]:
    """Word 文档：段落 + 表格 → 页级中间表示（每 20 个元素视为一"页"，贴近 PDF 粒度）。"""
    import docx as docx_lib
    from docx.table import Table as _Tbl
    from docx.text.paragraph import Paragraph as _Para

    doc = docx_lib.Document(str(path))
    pages: list[dict] = []
    page_no = 0
    page = {"page_no": page_no, "channel": "DOCX", "text": "", "tables": [], "images": []}

    def flush():
        nonlocal page_no, page
        if page["text"].strip() or page["tables"]:
            pages.append(page.copy())
            page_no += 1
        page = {"page_no": page_no, "channel": "DOCX", "text": "", "tables": [], "images": []}

    # 按文档流顺序遍历 body 元素（段落与表格交错，保持原文顺序）
    count = 0
    for child in doc.element.body.iterchildren():
        if child.tag.endswith("}p"):
            para = _Para(child, doc)
            text = para.text.strip()
            if text:
                page["text"] += text + "\n"
        elif child.tag.endswith("}tbl"):
            tbl = _Tbl(child, doc)
            rows = []
            for row in tbl.rows:
                cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                rows.append("| " + " | ".join(cells) + " |")
            if rows:
                header = rows[0]
                sep = "| " + " | ".join(["---"] * len(tbl.rows[0].cells)) + " |"
                page["tables"].append("\n".join([header, sep] + rows[1:]))
        count += 1
        if count % 20 == 0:
            flush()
    flush()
    stats = {"page_count": len(pages), "channels": {"DOCX": len(pages)},
             "vlm_calls": 0, "figures_parsed": 0, "failed_pages": []}
    return pages, stats


def parse_markdown(path: Path) -> tuple[list[dict], dict]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    # 去掉 markdown 图片/链接语法中的 URL，保留文字
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    # 按一/二级标题切 section
    parts = re.split(r"(?=^#{1,2}\s)", text, flags=re.M)
    pages = []
    for i, part in enumerate(parts):
        part = part.strip()
        if not part:
            continue
        pages.append({"page_no": i, "channel": "MD",
                      "text": part, "tables": [], "images": []})
    stats = {"page_count": len(pages), "channels": {"MD": len(pages)},
             "vlm_calls": 0, "figures_parsed": 0, "failed_pages": []}
    return pages, stats


def parse_image_file(path: Path) -> tuple[list[dict], dict]:
    png_bytes = path.read_bytes()
    info = vlm.parse_image(png_bytes)
    page = {"page_no": 0, "channel": "IMG", "text": "",
            "tables": [], "images": [info]}
    stats = {"page_count": 1, "channels": {"IMG": 1},
             "vlm_calls": 1, "figures_parsed": 1, "failed_pages": []}
    return [page], stats
