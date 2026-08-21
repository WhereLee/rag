"""
页级分析器（第二轮重构）：从"字符数二元判定"升级为"页内块化"。

职责（方案文档 §4.2）：
- 页级预判（R3）：无文本 → 整页转录通道 C；纯空白 → skipped
- 有文本页 → 块化：文本块 / 表格块 / 图片块（带坐标）→ 冲突裁决 → 阅读顺序排序
- 输出 PageResult（块级产物，供 pdf_parser 做块级 VLM 分派）

与旧实现的关系：保留 TableRegion/PageAnalysis 类型兼容，新增 extract_page_blocks 主入口。
成本：纯坐标+线条判定，零 VLM 开销（VLM 只在块级分派时按需触发）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pymupdf

from ingest.block_types import Block, BlockType, PageResult, BLOCK_OK

logger = logging.getLogger("rag.page_analyzer")

# 判定阈值
SCANNED_MIN_CHARS = 50          # 页字符数低于此值 → 扫描页（整页转录）
MIN_LINE_LEN = 40               # 计入表格特征的最小线长
TABLE_MIN_H_LINES = 2           # 构成表格的最少横线数
TABLE_MIN_V_LINES = 2
LINE_CLUSTER_GAP = 12           # 行间距小于此值归入同表格簇（多区域聚类）
MIN_IMAGE_AREA_RATIO = 0.02     # 图片块最小面积占比（低于此值忽略，防噪声）

# 兼容旧调用（pdf_parser 已改用新接口，保留类型定义）
@dataclass
class TableRegion:
    rect: pymupdf.Rect
    confidence: float = 1.0


@dataclass
class PageAnalysis:
    page_no: int
    channel: str                    # A / B / C 兼容旧语义
    char_count: int
    table_regions: list = field(default_factory=list)
    image_bboxes: list = field(default_factory=list)


def _text_char_count(page: pymupdf.Page) -> int:
    return len(page.get_text("text").strip())


def _table_regions_from_drawings(page: pymupdf.Page) -> list[TableRegion]:
    """从矢量线条多区域聚类出表格区域（R3：多表页分离为多个区域，不再整页一个包围盒）。

    算法：提取横/竖线 → 按 y 坐标贪心聚簇（行间距 < LINE_CLUSTER_GAP 合并）→ 每簇一个包围盒。
    """
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

    def cluster(lines: list) -> list[list]:
        """按 y 中心聚簇：先按 y0 排序，间距小于容差合并。"""
        lines = sorted(lines, key=lambda r: r.y0)
        clusters: list[list] = []
        for ln in lines:
            placed = False
            for c in clusters:
                c_top = min(x.y0 for x in c)
                c_bot = max(x.y1 for x in c)
                if (ln.y0 - c_bot) < LINE_CLUSTER_GAP or (c_top - ln.y1) < LINE_CLUSTER_GAP:
                    c.append(ln)
                    placed = True
                    break
            if not placed:
                clusters.append([ln])
        return clusters

    h_clusters = cluster(h_lines)
    v_clusters = cluster(v_lines)
    # 横线簇与竖线簇按空间交集配对合并成表格区
    regions: list[TableRegion] = []
    for hc in h_clusters:
        hc_rect = pymupdf.Rect(
            min(r.x0 for r in hc), min(r.y0 for r in hc),
            max(r.x1 for r in hc), max(r.y1 for r in hc))
        for vc in v_clusters:
            vc_rect = pymupdf.Rect(
                min(r.x0 for r in vc), min(r.y0 for r in vc),
                max(r.x1 for r in vc), max(r.y1 for r in vc))
            # 横竖簇存在交集（或近似相交）才合并为表格区域
            if hc_rect.intersects(vc_rect):
                rect = pymupdf.Rect(
                    min(hc_rect.x0, vc_rect.x0), min(hc_rect.y0, vc_rect.y0),
                    max(hc_rect.x1, vc_rect.x1), max(hc_rect.y1, vc_rect.y1))
                if rect.width >= MIN_LINE_LEN * 2 and rect.height >= 20:
                    regions.append(TableRegion(rect=rect))
    return regions


def _image_blocks(page: pymupdf.Page, page_area: float,
                  min_area_ratio: float = MIN_IMAGE_AREA_RATIO,
                  max_count: int = 8) -> list[dict]:
    """页内图片块（带 bbox），异常显式记录并返回。

    合法性校验（Step3 补强）：图片显示矩形若明显越界（任一维超过页面2倍）
    判定为损坏的坐标元数据（如超长异常的 band 矩形），丢弃并记录，避免裁剪非法区域触发渲染错误。
    """
    pw, ph = page.rect.width, page.rect.height
    boxes = []
    for img in page.get_images(full=True):
        try:
            rects = page.get_image_rects(img[0])
        except Exception as e:
            logger.warning("image rects failed: %s", e)
            continue
        for r in rects:
            w, h = r.width, r.height
            if w <= 0 or h <= 0 or r.x0 < -1 or r.y0 < -1:
                logger.debug("skip image rect: 非正面积或负坐标 %s", r)
                continue
            if w > pw * 2 or h > ph * 2:
                # 坐标元数据损坏（如 band 超高异常矩形）：丢弃并记录，不触发裁剪渲染
                logger.warning("image rect 越界异常(%s x %s 页面 %s x %s), 丢弃该图", w, h, pw, ph)
                continue
            if w * h >= page_area * min_area_ratio:
                boxes.append({"bbox": [r.x0, r.y0, r.x1, r.y1],
                             "x0": r.x0, "y0": r.y0, "x1": r.x1, "y1": r.y1})
    boxes.sort(key=lambda b: (b["y0"], b["x0"]))  # 稳定顺序
    return boxes[:max_count]


def _resolve_blocks(page: pymupdf.Page, page_no: int) -> PageResult:
    """块化主流程：采集三类候选 → 冲突裁决 → 阅读顺序 → PageResult（通道 由块构成决定）。"""
    page_area = page.rect.width * page.rect.height
    candidates: list = []

    # 1) 文本候选：get_text("blocks") 原生坐标块
    for block in page.get_text("blocks", sort=False):
        x0, y0, x1, y1, text, bno, btype = block[:7]
        text = (text or "").strip()
        if not text:
            continue
        candidates.append({"kind": "text", "bbox": [x0, y0, x1, y1],
                           "x0": x0, "y0": y0, "x1": x1, "y1": y1, "text": text})

    # 2) 表格候选：多区域聚类
    for reg in _table_regions_from_drawings(page):
        r = reg.rect
        candidates.append({"kind": "table", "bbox": [r.x0, r.y0, r.x1, r.y1],
                          "x0": r.x0, "y0": r.y0, "x1": r.x1, "y1": r.y1, "text": ""})

    # 3) 图片候选
    for img in _image_blocks(page, page_area):
        candidates.append({"kind": "image", "bbox": img["bbox"],
                           "x0": img["x0"], "y0": img["y0"],
                           "x1": img["x1"], "y1": img["y1"], "text": ""})

    if not candidates:
        return PageResult(page_no=page_no, page_status="skipped", channel="",
                          blocks=[], errors=[])

    # 冲突裁决（方案 §4.2 STEP2）：
    #   a. 图片 ⊃ 文本（文本完全在图中）→ 文本并入图片块（截图内文字）
    #   b. 表格 ⊃ 文本 → 表格整体吞并内部文本
    #   c. 图注文本（与图相邻不重叠）→ 保留为独立文本块
    def area(b):
        return max(b["x1"] - b["x0"], 0) * max(b["y1"] - b["y0"], 0)

    def contains(outer, inner) -> bool:
        return (outer["x0"] - 2 <= inner["x0"] and outer["y0"] - 2 <= inner["y0"]
                and outer["x1"] + 2 >= inner["x1"] and outer["y1"] + 2 >= inner["y1"])

    resolved: list = []
    # 表格优先吞并内部文本，其次图片吞并内部文本
    for c in sorted(candidates, key=lambda c: 0 if c["kind"] == "table" else (1 if c["kind"] == "image" else 2)):
        merged = False
        for r in resolved:
            if c["kind"] == "text" and r["kind"] in ("table", "image") and contains(r, c):
                # 文本被更高优先级块吞并
                if r["kind"] == "image":
                    r.setdefault("inner_texts", []).append(c["text"])
                merged = True
                break
            if c["kind"] in ("table", "image") and r["kind"] == "text" and contains(c, r):
                r["kind"] = c["kind"]  # 覆盖文本块为图/表所在区域
                r["bbox"] = c["bbox"] if area(c) > area(r) else r["bbox"]
                merged = True
                break
        if not merged:
            resolved.append(dict(c))

    # 若一张图包含多文本，合并文本内容
    for r in resolved:
        if r["kind"] == "image" and r.get("inner_texts"):
            r["text"] = " ".join(r["inner_texts"])

    # 阅读顺序（单栏优先；双栏留 TODO：需先做栏检测）
    def read_key(c):
        row = round(c["y0"] / 30)  # 30pt 行高容差分带
        return (row, c["x0"])
    resolved.sort(key=read_key)

    # 组装 Block（status 由分派阶段赋值；此处标记 ok 待处理）
    blocks: list[Block] = []
    errors: list[dict] = []
    for i, c in enumerate(resolved):
        btype = {"text": BlockType.TEXT, "table": BlockType.TABLE,
                 "image": BlockType.IMAGE}[c["kind"]]
        blocks.append(Block(page_no=page_no, order=i, type=btype,
                           bbox=list(c["bbox"]), text=c.get("text", ""),
                           status=BLOCK_OK, meta={}))

    has_table = any(b.type == BlockType.TABLE for b in blocks)
    has_image = any(b.type == BlockType.IMAGE for b in blocks)
    has_text = any(b.type == BlockType.TEXT for b in blocks)
    channel = "B" if has_table else ("A" if has_text else "C")
    if has_image and has_text:
        channel = "M"  # 混合页（文本+图块并行分派，方案核心场景）
    page_status = "ok" if not errors else "partial"
    return PageResult(page_no=page_no, page_status=page_status, channel=channel,
                      blocks=blocks, errors=errors)


def analyze_page(page: pymupdf.Page, page_no: int,
                 enable_figure_parse: bool = True) -> PageAnalysis:
    """兼容旧接口（历史调用方，如无外部使用可删）——内部转为便捷包装。"""
    chars = _text_char_count(page)
    result = extract_page_blocks(page, page_no, enable_figure_parse)
    return PageAnalysis(
        page_no=page_no, channel=result.channel or "C", char_count=chars,
        table_regions=[TableRegion(rect=pymupdf.Rect(*b.bbox)) for b in result.blocks
                       if b.type == BlockType.TABLE],
        image_bboxes=[pymupdf.Rect(*b.bbox) for b in result.blocks if b.type == BlockType.IMAGE])


def extract_page_blocks(page: pymupdf.Page, page_no: int,
                        enable_figure_parse: bool = True) -> PageResult:
    """对外主入口：页内块化（方案 §4.2）。

    预判（R3）：字符数 < SCANNED_MIN_CHARS → 整页扫描通道 C（整页图片块待转录）或跳过。
    result.channel: A（纯文本）B（含表格）M（文本+图片）C（整页转录）空（blank）
    """
    chars = _text_char_count(page)
    if chars < SCANNED_MIN_CHARS:
        # 无文本层：整页作为垂直转录候选（扫描/纯图页）
        imgs = _image_blocks(page, page.rect.width * page.rect.height)
        if not imgs:
            return PageResult(page_no=page_no, page_status="skipped", channel="C",
                              blocks=[], errors=[])
        # 整页包围盒作为单个放大图块（通道 C 语义保留：交由 pdf_parser 整页渲染转录）
        pr = pymupdf.Rect(page.rect)
        return PageResult(page_no=page_no, page_status="ok", channel="C",
                          blocks=[Block(page_no=page_no, order=0, type=BlockType.IMAGE,
                                        bbox=[pr.x0, pr.y0, pr.x1, pr.y1], status=BLOCK_OK)],
                          errors=[])
    # 有文本层：块化
    return _resolve_blocks(page, page_no)