# -*- coding: utf-8 -*-
"""R4 边界测试：转录失败占位 / 转录空占位 / 损坏图片 / 超长图片 / 未配置 VLM。
用法: python scripts/debug/verify_rag_r4_edge.py（需在项目根目录执行）
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "rag-python" / "src"))

import pymupdf

from ingest.parser.base import ParseError
from ingest.parser.image import ImageParser
from ingest.parser.pdf import PdfParser
from ingest.parser.vlm import to_png_bytes

fail = 0


def check(name, cond, detail=""):
    global fail
    if cond:
        print(f"[OK] {name}")
    else:
        print(f"[FAIL] {name} -> {detail}")
        fail += 1


class BoomVLM:
    def chat_text(self, prompt, png_bytes, cache_prefix):
        raise TimeoutError("vlm timeout")

    def chat_image(self, prompt, png_bytes, cache_prefix):
        raise TimeoutError("vlm timeout")


class EmptyVLM:
    def chat_text(self, prompt, png_bytes, cache_prefix):
        return "   "

    def chat_image(self, prompt, png_bytes, cache_prefix):
        return {}


tmp = Path(tempfile.mkdtemp(prefix="rag_r4_e_"))


def make_scanned(pdf, pages=2):
    doc = pymupdf.open()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 300, 200))
    pix.clear_with(200)
    for _ in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_image(pymupdf.Rect(72, 100, 372, 300), pixmap=pix)
    doc.save(pdf)
    doc.close()


# 1. VLM 全失败 → 每页占位不中断
pdf = tmp / "scan.pdf"
make_scanned(pdf)
nodes = PdfParser(vlm=BoomVLM()).parse(pdf)
check("转录失败不中断", len(nodes) == 2, nodes)
check("失败占位文本", all(n.text == "[该页解析失败]" for n in nodes))

# 2. 转录空输出 → 空占位
pdf = tmp / "scan2.pdf"
make_scanned(pdf, pages=1)
nodes = PdfParser(vlm=EmptyVLM()).parse(pdf)
check("转录空占位", nodes[0].text == "[该页转录为空]", nodes[0].text)

# 3. 扫描型但未配置 VLM → ParseError
pdf = tmp / "scan3.pdf"
make_scanned(pdf, pages=1)
try:
    PdfParser().parse(pdf)
    check("无 VLM 抛错", False, "未抛错")
except ParseError as e:
    check("无 VLM 抛错", "扫描型" in str(e), e)

# 4. 独立图片 VLM 失败 → 占位
p = tmp / "img.png"
pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 100, 80))
pix.clear_with(200)
p.write_bytes(pix.tobytes("png"))
nodes = ImageParser(vlm=BoomVLM()).parse(p)
check("图片失败占位", nodes[0].text == "[图片解析失败]", nodes[0].text)

# 5. 损坏图片 → 占位不抛
p = tmp / "bad.png"
p.write_bytes(b"\x89PNG not really")
nodes = ImageParser(vlm=BoomVLM()).parse(p)
check("损坏图片占位", nodes[0].text == "[图片解析失败]", nodes[0].text)

# 6. 超长图片缩放边界：单边极大（10000x50）不崩
p = tmp / "strip.png"
pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 10000, 50))
pix.clear_with(200)
p.write_bytes(pix.tobytes("png"))
out = to_png_bytes(p.read_bytes())
from PIL import Image
import io
w, h = Image.open(io.BytesIO(out)).size
check("超长图缩放", max(w, h) <= 1568 and w >= h, (w, h))

# 7. VLM 空 JSON（description/text 都缺）→ "图片无法解析"占位
p = tmp / "emptyobj.png"
pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 50, 50))
pix.clear_with(200)
p.write_bytes(pix.tobytes("png"))
nodes = ImageParser(vlm=EmptyVLM()).parse(p)
check("空 JSON 占位", nodes[0].text == "[图片无法解析]", nodes[0].text)

print(f"\nR4 边界测试: {'全部通过' if fail == 0 else f'{fail} 项失败'}")
sys.exit(1 if fail else 0)
