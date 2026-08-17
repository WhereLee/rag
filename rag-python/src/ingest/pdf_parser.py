"""
PDF 解析：按页路由三通道。

- 通道A：pymupdf 文本直提（零成本）
- 通道B：文本直提 + 表格区域裁剪 → VLM 结构化 markdown
- 通道C：整页渲染 → VLM 转录（扫描件）
- 大图配图 → VLM 语义描述（可选）

产出页级中间表示：[{page_no, channel, text, tables:[md], images:[{description,text}]}]
"""
import logging
from pathlib import Path

import pymupdf

from ingest import vlm
from ingest.page_analyzer import analyze_page, PageAnalysis
from llm.mimo_client import LLMError

logger = logging.getLogger("rag.pdf_parser")

RENDER_DPI = 200


def _render_png(page: pymupdf.Page, clip: pymupdf.Rect | None = None,
                dpi: int = RENDER_DPI) -> bytes:
    pix = page.get_pixmap(dpi=dpi, clip=clip)
    return pix.tobytes("png")


def parse_pdf(path: Path, enable_figure_parse: bool = True) -> tuple[list[dict], dict]:
    """返回 (pages, stats)。stats 记录通道分布与 VLM 调用数。"""
    doc = pymupdf.open(str(path))
    pages: list[dict] = []
    stats = {"page_count": doc.page_count, "channels": {"A": 0, "B": 0, "C": 0},
             "vlm_calls": 0, "figures_parsed": 0, "failed_pages": []}

    for i, page in enumerate(doc):
        analysis: PageAnalysis = analyze_page(page, i, enable_figure_parse)
        page_repr = {"page_no": i, "channel": analysis.channel,
                     "text": "", "tables": [], "images": []}
        try:
            if analysis.channel == "C":
                png = _render_png(page)
                page_repr["text"] = vlm.parse_scanned_page(png)
                stats["vlm_calls"] += 1
            else:
                page_repr["text"] = page.get_text("text").strip()
                # 通道B：表格区域逐个 VLM 结构化
                for region in analysis.table_regions:
                    clip = region.rect + (-2, -2, 2, 2)  # 略放大防切边
                    clip = clip & page.rect
                    png = _render_png(page, clip=clip)
                    md = vlm.parse_table_region(png)
                    stats["vlm_calls"] += 1
                    if md:
                        page_repr["tables"].append(md)
                # 大图配图解析（A/B 通道都可能带图）
                for bbox in analysis.image_bboxes:
                    png = _render_png(page, clip=bbox & page.rect)
                    info = vlm.parse_image(png)
                    stats["vlm_calls"] += 1
                    stats["figures_parsed"] += 1
                    page_repr["images"].append(info)
        except LLMError as e:
            logger.error("page %d vlm parse failed: %s", i, e)
            stats["failed_pages"].append(i)
            if analysis.channel == "C":
                page_repr["channel"] = "C_failed"
        stats["channels"][analysis.channel] = stats["channels"].get(analysis.channel, 0) + 1
        pages.append(page_repr)

    doc.close()
    return pages, stats
