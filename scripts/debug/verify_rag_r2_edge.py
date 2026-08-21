# -*- coding: utf-8 -*-
"""R2 边界测试：扫描型判定 / 空页 / 超页数 / VLM 失败降级 / 断行不误伤。
用法: python scripts/debug/verify_rag_r2_edge.py（需在项目根目录执行）
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "rag-python" / "src"))

import pymupdf

from ingest.parser.base import DocumentNode, ParseError
from ingest.parser.pdf import PdfParser, merge_broken_lines

fail = 0


def check(name, cond, detail=""):
    global fail
    if cond:
        print(f"[OK] {name}")
    else:
        print(f"[FAIL] {name} -> {detail}")
        fail += 1


class BoomVLM:
    def chat_image(self, prompt, png_bytes, cache_prefix):
        raise TimeoutError("vlm timeout")


def _ins(page, pos, text, size=11):
    try:
        page.insert_text(pos, text, fontsize=size, fontname="china-s")
    except Exception:
        page.insert_text(pos, text, fontsize=size)


tmp = Path(tempfile.mkdtemp(prefix="rag_r2_e_"))

# 1. 扫描型 PDF：纯图无字 → ParseError 且提示"扫描型"
pdf = tmp / "scan.pdf"
doc = pymupdf.open()
pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 300, 200))
pix.clear_with(200)
for _ in range(3):
    page = doc.new_page(width=595, height=842)
    page.insert_image(pymupdf.Rect(72, 100, 372, 300), pixmap=pix)
doc.save(pdf)
doc.close()
try:
    PdfParser().parse(pdf)
    check("扫描型拒绝", False, "未抛错")
except ParseError as e:
    check("扫描型拒绝", "扫描型" in str(e), e)

# 2. 空页 PDF → 空节点列表（不抛错）
pdf = tmp / "blank.pdf"
doc = pymupdf.open()
doc.new_page(width=595, height=842)
doc.save(pdf)
doc.close()
nodes = PdfParser().parse(pdf)
check("空页 PDF 空产物", nodes == [], nodes)

# 3. 超页数 → ParseError
pdf = tmp / "big.pdf"
doc = pymupdf.open()
for _ in range(3):
    page = doc.new_page(width=595, height=842)
    _ins(page, (72, 72), "有文字的页面。")
doc.save(pdf)
doc.close()
try:
    PdfParser(max_pages=2).parse(pdf)
    check("超页数拒绝", False, "未抛错")
except ParseError as e:
    check("超页数拒绝", "页数超限" in str(e), e)

# 4. VLM 超时降级 → 图片块占位，不中断整文件
pdf = tmp / "vlmfail.pdf"
doc = pymupdf.open()
page = doc.new_page(width=595, height=842)
page.insert_image(pymupdf.Rect(72, 80, 272, 180), pixmap=pix)
_ins(page, (72, 220), "正文段落第一行有足够多的文字内容说明描述。")
doc.save(pdf)
doc.close()
nodes = PdfParser(vlm=BoomVLM()).parse(pdf)
types = [n.type for n in nodes]
check("VLM 失败不中断", "image" in types and "paragraph" in types, types)
img = next(n for n in nodes if n.type == "image")
check("失败占位", img.text == "[图片解析失败]", img.text)

# 5. 断行合并不误伤：句末标点不合并；code/table 节点不动
nodes = [
    DocumentNode("paragraph", "第一句以句号结束。\n第二句是新的内容", {"page": 1}),
    DocumentNode("code", "def f():\n    return 1", {"page": 1}),
    DocumentNode("table", "| a | b |\n|---|---|", {"page": 1}),
]
merge_broken_lines(nodes)
check("句号后不合并", nodes[0].text == "第一句以句号结束。\n第二句是新的内容", nodes[0].text)
check("code 不被合并", nodes[1].text == "def f():\n    return 1", nodes[1].text)
check("table 不被合并", nodes[2].text == "| a | b |\n|---|---|", nodes[2].text)
# 无标点结尾的断行 → 合并
nodes = [DocumentNode("paragraph", "无标点结尾的行\n续接文字", {"page": 1})]
merge_broken_lines(nodes)
check("无标点断行合并", nodes[0].text == "无标点结尾的行续接文字", nodes[0].text)

# 6. 损坏 PDF → 异常（PyMuPDF 抛错，不崩溃成空产物）
pdf = tmp / "corrupt.pdf"
pdf.write_bytes(b"%PDF-1.4\n%%EOF garbage not really a pdf")
try:
    PdfParser().parse(pdf)
    check("损坏 PDF 抛错", False, "未抛错")
except Exception as e:
    check("损坏 PDF 抛错", True, str(e)[:60])

print(f"\nR2 边界测试: {'全部通过' if fail == 0 else f'{fail} 项失败'}")
sys.exit(1 if fail else 0)
