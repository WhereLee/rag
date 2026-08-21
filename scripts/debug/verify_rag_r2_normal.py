# -*- coding: utf-8 -*-
"""R2 正常测试：文本型 PDF 块分离 / 图片块 VLM / 页眉页脚 / 断行合并 端到端。
用法: python scripts/debug/verify_rag_r2_normal.py（需在项目根目录执行）
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "rag-python" / "src"))

import pymupdf

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
        self.calls = 0

    def chat_image(self, prompt, png_bytes, cache_prefix):
        self.calls += 1
        return {"description": "架构示意图：服务A调用服务B", "text_in_image": "ServiceA\nServiceB"}


def _ins(page, pos, text, size=11):
    try:
        page.insert_text(pos, text, fontsize=size, fontname="china-s")
    except Exception:
        page.insert_text(pos, text, fontsize=size)


tmp = Path(tempfile.mkdtemp(prefix="rag_r2_n_"))

# 1. 图文混排 3 页：页眉"公司内部资料"重复 + 页脚"第X页" + 第2页插图 + 正文断行
pdf = tmp / "mixed.pdf"
doc = pymupdf.open()
pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 100))
pix.clear_with(220)
for p in range(3):
    page = doc.new_page(width=595, height=842)
    _ins(page, (72, 40), "公司内部资料", size=9)
    y = 60
    if p == 1:
        page.insert_image(pymupdf.Rect(72, y, 272, y + 100), pixmap=pix)
        y += 110
    _ins(page, (72, y), f"第{p + 1}页正文第一段，描述系统总体架构设计")
    _ins(page, (72, y + 20), "第二行是断行的续接文字内容")
    _ins(page, (72, 800), f"第 {p + 1} 页", size=9)
doc.save(pdf)
doc.close()

fake = FakeVLM()
nodes = PdfParser(vlm=fake).parse(pdf)

# 页码 meta 完整
check("页码 meta 完整", all(n.meta.get("page") in (1, 2, 3) for n in nodes), nodes)
# 页眉被清（3 页重复 ≥ 1.5 阈值）
check("页眉剔除", all("公司内部资料" not in n.text for n in nodes))
# 页脚保留（每页页码不同，仅出现 1 次，不误删）
all_text = "\n".join(n.text for n in nodes)
check("页码不误删", "第 2 页" in all_text)
# 第2页有图片节点且描述入库
img_nodes = [n for n in nodes if n.type == "image"]
check("图片块 VLM 描述", len(img_nodes) == 1 and "ServiceA" in img_nodes[0].text, img_nodes)
check("VLM 调用次数", fake.calls == 1, fake.calls)
# 图片节点带 bbox
check("图片带坐标", bool(img_nodes[0].meta.get("bbox")))
# 节点顺序：第2页图片在正文前（y 坐标靠上）
p2_nodes = [n for n in nodes if n.meta["page"] == 2]
check("块顺序", p2_nodes[0].type == "image", [n.type for n in p2_nodes])
# 断行合并生效：跨行文字被拼接（正文两行之间无标点结尾）
check("断行合并", any("架构设计第二行" in n.text for n in nodes),
      [n.text for n in nodes if n.type == "paragraph"])
# 节点带 source
check("来源标记", all(n.meta.get("source") == "mixed.pdf" for n in nodes))

# 2. 纯文本 PDF（无图片无页眉页脚）
pdf2 = tmp / "plain.pdf"
doc = pymupdf.open()
for p in range(2):
    page = doc.new_page(width=595, height=842)
    _ins(page, (72, 72), f"纯文本第{p + 1}页。")
doc.save(pdf2)
doc.close()
nodes2 = PdfParser(vlm=fake).parse(pdf2)
check("纯文本 PDF 节点", len(nodes2) == 2 and all(n.type == "paragraph" for n in nodes2), nodes2)
check("无图片不调 VLM", fake.calls == 1, fake.calls)  # 仍是 1（仅 mixed.pdf 调过）

print(f"\nR2 正常测试: {'全部通过' if fail == 0 else f'{fail} 项失败'}")
sys.exit(1 if fail else 0)
