"""
页级分析器：决定每页走哪条解析通道。

判定规则（成本递增）：
- 通道A 文本直提：有文本层且无表格特征
- 通道B 直提+VLM表格：有文本层且有表格特征
- 通道C VLM整页：无文本层（扫描件）
"""
import logging
from dataclasses import dataclass, field

import pymupdf

logger = logging.getLogger("rag.page_analyzer")

# 判定阈值
SCANNED_MIN_CHARS = 50          # 单页字符数低于此值 → 扫描件
MIN_LINE_LEN = 40               # 计入表格特征的最小线长
TABLE_MIN_H_LINES = 2           # 构成表格的最少横线数
TABLE_MIN_V_LINES = 2


@dataclass
class TableRegion:
    rect: pymupdf.Rect
    confidence: float = 1.0


@dataclass
class PageAnalysis:
    page_no: int                    # 0-based
    channel: str                    # A / B / C
    char_count: int
    table_regions: list[TableRegion] = field(default_factory=list)
    image_bboxes: list[pymupdf.Rect] = field(default_factory=list)


def _text_char_count(page: pymupdf.Page) -> int:
    return len(page.get_text("text").strip())


def _table_regions_from_drawings(page: pymupdf.Page) -> list[TableRegion]:
    """从矢量线条聚类出表格区域（横线+竖线交集包围盒）。"""
    h_lines, v_lines = [], []
    for d in page.get_drawings():
        for item in d.get("items", []):
            kind = item[0]
            if kind == "l":  # 直线
                p1, p2 = item[1], item[2]
                dx, dy = abs(p2.x - p1.x), abs(p2.y - p1.y)
                if dy <= 2 and dx >= MIN_LINE_LEN:
                    h_lines.append(pymupdf.Rect(p1, p2))
                elif dx <= 2 and dy >= MIN_LINE_LEN:
                    v_lines.append(pymupdf.Rect(p1, p2))
            elif kind == "re":  # 矩形（单元格边框常用）
                r = item[1]
                if isinstance(r, pymupdf.Rect) and r.width >= MIN_LINE_LEN and r.height >= 8:
                    h_lines.append(pymupdf.Rect(r.x0, r.y0, r.x1, r.y0))
                    h_lines.append(pymupdf.Rect(r.x0, r.y1, r.x1, r.y1))
                    v_lines.append(pymupdf.Rect(r.x0, r.y0, r.x0, r.y1))
                    v_lines.append(pymupdf.Rect(r.x1, r.y0, r.x1, r.y1))
    if len(h_lines) < TABLE_MIN_H_LINES or len(v_lines) < TABLE_MIN_V_LINES:
        return []
    # 所有线条合并为一个包围盒（单页多表场景少，先按整页聚合；够用且可解释）
    all_lines = h_lines + v_lines
    x0 = min(r.x0 for r in all_lines)
    y0 = min(r.y0 for r in all_lines)
    x1 = max(r.x1 for r in all_lines)
    y1 = max(r.y1 for r in all_lines)
    rect = pymupdf.Rect(x0, y0, x1, y1)
    # 过滤过小的误检区域
    if rect.width < MIN_LINE_LEN * 2 or rect.height < 20:
        return []
    return [TableRegion(rect=rect)]


def _large_image_bboxes(page: pymupdf.Page, min_area_ratio: float = 0.15,
                        max_count: int = 4) -> list[pymupdf.Rect]:
    """页内大图（占页面积 > 阈值），供配图解析。"""
    page_area = page.rect.width * page.rect.height
    boxes = []
    for img in page.get_images(full=True):
        try:
            rects = page.get_image_rects(img[0])
        except Exception:
            continue
        for r in rects:
            if r.width * r.height >= page_area * min_area_ratio:
                boxes.append(r)
    return boxes[:max_count]


def analyze_page(page: pymupdf.Page, page_no: int,
                 enable_figure_parse: bool = True) -> PageAnalysis:
    chars = _text_char_count(page)

    if chars < SCANNED_MIN_CHARS:
        return PageAnalysis(page_no=page_no, channel="C", char_count=chars)

    tables = _table_regions_from_drawings(page)
    images = _large_image_bboxes(page) if enable_figure_parse else []
    channel = "B" if tables else "A"
    return PageAnalysis(page_no=page_no, channel=channel, char_count=chars,
                        table_regions=tables, image_bboxes=images)
