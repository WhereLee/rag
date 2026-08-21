# -*- coding: utf-8 -*-
"""R4 正常测试：扫描 PDF 整页转录 / 独立图片 VLM 描述 / 大图缩放。
用法: python scripts/debug/verify_rag_r4_normal.py（需在项目根目录执行）
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "rag-python" / "src"))

import pymupdf

from ingest.parser.image import ImageParser
from ingest.parser.pdf import PdfParser

fail = 0


def check(name, cond, detail=""):
    global fail
    if cond:
        print(f"[OK] {name}")
    else:
        print(f"[FAIL] {name} -> {detail}")
        fail += 1


class FakeVLM:
    def __init__(self):
        self.text_calls = 0
        self.json_calls = 0

    def chat_text(self, prompt, png_bytes, cache_prefix):
        self.text_calls += 1
        return "扫描页内容：关于系统架构的说明文档。表格：\n| 模块 | 状态 |\n|---|---|\n| A | 正常 |"

    def chat_image(self, prompt, png_bytes, cache_prefix):
        self.json_calls += 1
        return {"description": "趋势图：销售额逐月上升", "text_in_image": "2024\n2025"}


tmp = Path(tempfile.mkdtemp(prefix="rag_r4_n_"))
fake = FakeVLM()

# 1. 扫描型 PDF（纯图 3 页）→ 每页一个转录节点
pdf = tmp / "scan.pdf"
doc = pymupdf.open()
pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 300))
pix.clear_with(200)
for _ in range(3):
    page = doc.new_page(width=595, height=842)
    page.insert_image(pymupdf.Rect(72, 100, 472, 400), pixmap=pix)
doc.save(pdf)
doc.close()
nodes = PdfParser(vlm=fake).parse(pdf)
check("扫描页节点数", len(nodes) == 3, nodes)
check("扫描页内容", all("扫描页内容" in n.text for n in nodes))
check("扫描页页码", [n.meta["page"] for n in nodes] == [1, 2, 3])
check("转录调用次数", fake.text_calls == 3, fake.text_calls)

# 2. 独立图片（png/jpg）→ VLM 描述
for name in ("photo.png", "photo.jpg"):
    p = tmp / name
    if name.endswith("png"):
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 300, 200))
        pix.clear_with(160)
        p.write_bytes(pix.tobytes("png"))
    else:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 300, 200))
        pix.clear_with(160)
        p.write_bytes(pix.tobytes("jpeg"))
    nodes = ImageParser(vlm=fake).parse(p)
    check(f"图片 {name} 描述", len(nodes) == 1 and "销售额" in nodes[0].text, nodes)
check("图片调用次数", fake.json_calls == 2, fake.json_calls)

# 3. 大图缩放：3000x1500 PNG → 长边 ≤ 1568（to_png_bytes 内部，间接验证不报错）
big = tmp / "big.png"
pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 3000, 1500))
pix.clear_with(200)
big.write_bytes(pix.tobytes("png"))
nodes = ImageParser(vlm=fake).parse(big)
check("大图解析不报错", len(nodes) == 1 and "销售额" in nodes[0].text, nodes)

print(f"\nR4 正常测试: {'全部通过' if fail == 0 else f'{fail} 项失败'}")
sys.exit(1 if fail else 0)
