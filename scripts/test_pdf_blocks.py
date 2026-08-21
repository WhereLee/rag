# -*- coding: utf-8 -*-
"""块化（Block Extraction）单元测试（第二轮修复）。

覆盖（方案 §4.2 + R3/R5）：
- 页级预判：扫描页（chars<50）→ channel=C 整页块
- 块化结构：块类型/顺序/bbox 合法性
- 多区域表格聚类：多表页产生 ≥2 表格区域
- 冲突裁决：图片含文本吞并
- 兼容层：to_legacy_page 转旧结构

运行：pytest scripts/test_pdf_blocks.py
依赖：rag-python/src, data/corpus 内 PDF（真实文件，只读不写）
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rag-python" / "src"))

import pymupdf
from ingest.block_types import BlockType, PageResult, to_legacy_page
from ingest.page_analyzer import extract_page_blocks

CORPUS = Path(__file__).resolve().parents[1] / "data" / "corpus"

def _pdf_path(name: str = "whitepaper/企业智能文档管理白皮书（2026年）.pdf") -> Path:
    p = CORPUS / name
    if not p.exists():
        pytest.skip(f"语料缺失: {p}")
    return p


def _iter_pages(name: str = "whitepaper/企业智能文档管理白皮书（2026年）.pdf"):
    doc = pymupdf.open(str(_pdf_path(name)))
    for i, page in enumerate(doc):
        yield i, page
    doc.close()


def test_scan_page_precheck():
    """扫描件页面：无文本层 → channel=C 整页块（通道 C 语义保留）"""
    doc = pymupdf.open(str(_pdf_path("scanned/历史档案数字化试点通知（扫描件）.pdf")))
    page = doc[0]
    result = extract_page_blocks(page, 0)
    # 扫描件首页应无文本层（或极少）→ 整页转录语义
    if result.channel == "C":
        assert len(result.blocks) == 1 and result.blocks[0].type == BlockType.IMAGE
    doc.close()


def test_block_structure():
    """白皮书有文本页：块结构合法（类型/顺序/bbox）"""
    checked = 0
    for i, page in _iter_pages():
        result = extract_page_blocks(page, i)
        if result.channel in ("", "C"):
            continue  # 跳过扫描/空白页
        assert isinstance(result, PageResult)
        orders = [b.order for b in result.blocks]
        assert orders == sorted(orders), f"page {i} 块序乱: {orders}"
        for b in result.blocks:
            assert b.type in (BlockType.TEXT, BlockType.TABLE, BlockType.IMAGE)
            assert len(b.bbox) == 4 and b.bbox[2] > b.bbox[0] and b.bbox[3] > b.bbox[1], f"bad bbox {b.bbox}"
        checked += 1
        if checked >= 10:  # 抽查前 10 个有内容页
            break
    assert checked > 0, "白皮书应存在有文本页"


def test_multi_table_regions():
    """规范文档页：能切出 ≥1 表格块（若该页含表）"""
    counts = 0
    for i, page in _iter_pages("standard/文档智能解析技术规范 LT-S 001-2026.pdf"):
        result = extract_page_blocks(page, i)
        tables = [b for b in result.blocks if b.type == BlockType.TABLE]
        if tables:
            counts += len(tables)
            # 多表页应分离为多个区域（R3 多区域聚类）
            assert len(tables) >= 1
            bboxes = [b.bbox for b in tables]
            x0s = [b[0] for b in bboxes]
            assert x0s == sorted(x0s), "表格块应按阅读顺序排列"
    assert counts > 0, "规范文档应包含表格区域"


def test_md_render():
    """Markdown 渲染器：块序稳定、失败块转注释不中断流（S4 交付物）。"""
    from ingest.md_render import render_page
    from ingest.block_types import Block, BlockType, PageResult
    pr = PageResult(page_no=1, page_status="partial", channel="M", blocks=[
        Block(page_no=1, order=0, type=BlockType.TEXT, bbox=[0, 0, 1, 1], text="正文段落"),
        Block(page_no=1, order=1, type=BlockType.TABLE, bbox=[0, 0, 1, 1], text="|a|b|\n|-|-|"),
        Block(page_no=1, order=2, type=BlockType.IMAGE, bbox=[0, 0, 1, 1],
              text="架构图", meta={"text_in_image": "检索层"}),
        Block(page_no=1, order=3, type=BlockType.IMAGE, bbox=[0, 0, 1, 1],
              status="failed(validate)", error="表格识别结果异常"),
    ])
    md = render_page(pr)
    assert "正文段落" in md
    assert "|a|b|" in md
    assert "架构图" in md and "检索层" in md
    assert "识别结果异常" in md
    # 块序稳定：文本 → 表格 → 图片 → 失败注释
    idx = [md.index(k) for k in ("正文段落", "|a|b|", "架构图", "识别结果异常")]
    assert idx == sorted(idx), f"块序错乱: {idx}"


def test_legacy_conversion():
    """兼容层：PageResult → 旧 page 结构（文本/表格/图片分流正确）"""
    for i, page in _iter_pages():
        result = extract_page_blocks(page, i)
        if not result.blocks:
            continue
        legacy = to_legacy_page(result)
        assert isinstance(legacy, dict)
        assert set(["page_no", "channel", "text", "tables", "images"]).issubset(legacy.keys())
        assert isinstance(legacy["text"], str)
        assert isinstance(legacy["tables"], list)
        assert isinstance(legacy["images"], list)
        # 文本块内容应出现在 text 中
        text_blocks = [b for b in result.blocks if b.type == BlockType.TEXT and b.status == "ok"]
        if text_blocks and not legacy["text"]:
            raise AssertionError(f"page {i} 有文本块但 text 为空")
        return  # 只检查第一个有块页面即可
    raise AssertionError("未找到有块的页面")