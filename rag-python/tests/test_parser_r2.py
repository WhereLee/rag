"""R2 单元测试：PDF 文本型块分离 / 图片块 VLM / 页眉页脚 / 断行合并 / 扫描型判定。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pymupdf

from ingest.parser.base import DocumentNode, ParseError
from ingest.parser.pdf import PdfParser, merge_broken_lines, remove_headers_footers
from ingest.parser.vlm import extract_json


class FakeVLM:
    """测试用假 VLM：返回固定 JSON；可配置抛异常。"""

    def __init__(self, obj=None, raise_exc=None):
        self.obj = obj or {"description": "架构图描述", "text_in_image": "模块A\n模块B"}
        self.raise_exc = raise_exc
        self.calls = 0

    def chat_image(self, prompt, png_bytes, cache_prefix):
        self.calls += 1
        if self.raise_exc:
            raise self.raise_exc
        return self.obj


def _ins(page, pos, text, size=11):
    """写文本：优先中文字体，回退默认字体（提取不受影响）。"""
    try:
        page.insert_text(pos, text, fontsize=size, fontname="china-s")
    except Exception:
        page.insert_text(pos, text, fontsize=size)


def make_pdf(path, pages=1, with_image=False, header=None, footer=None, with_text=True):
    """用 PyMuPDF 造 PDF：每页可选页眉/页脚 + 正文（可断行）+ 图片块。"""
    doc = pymupdf.open()
    pix = None
    if with_image:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 100))
        pix.clear_with(240)
    for p in range(pages):
        page = doc.new_page(width=595, height=842)
        y = 40
        if header:
            _ins(page, (72, y), header, size=9)
            y += 20
        if with_image:
            page.insert_image(pymupdf.Rect(72, y, 272, y + 100), pixmap=pix)
            y += 110
        if with_text:
            _ins(page, (72, y), f"第{p + 1}页正文第一段，包含中文内容。")
            _ins(page, (72, y + 20), "第二行断行续接内容")
        if footer:
            _ins(page, (72, 800), footer, size=9)
    doc.save(path)
    doc.close()


# ---------- extract_json 容错 ----------

class TestExtractJson:
    def test_pure_json(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_json(self):
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_surrounded_json(self):
        assert extract_json('好的，结果如下：{"a": 1} 完毕') == {"a": 1}

    def test_invalid(self):
        assert extract_json("不是json内容") is None
        assert extract_json("") is None


# ---------- 页眉页脚 ----------

class TestRemoveHeadersFooters:
    def test_repeated_line_removed(self):
        nodes = [
            DocumentNode("paragraph", "公司内部资料", {"page": 1}),
            DocumentNode("paragraph", "本周完成功能A", {"page": 1}),
            DocumentNode("paragraph", "公司内部资料", {"page": 2}),
            DocumentNode("paragraph", "本周完成功能B", {"page": 2}),
        ]
        out = remove_headers_footers(nodes, 2)
        texts = [n.text for n in out]
        assert "公司内部资料" not in texts
        assert "本周完成功能A" in texts

    def test_multi_line_node_not_touched(self):
        # 页眉与正文在同一节点内（多行）：保守保留，不误删正文
        nodes = [
            DocumentNode("paragraph", "公司内部资料\n本周正文", {"page": 1}),
            DocumentNode("paragraph", "公司内部资料", {"page": 2}),
        ]
        out = remove_headers_footers(nodes, 2)
        assert [n.text for n in out] == ["公司内部资料\n本周正文"]

    def test_bbox_zone_required(self):
        # 重复行在页面中部（非页眉页脚区域）→ 保留（防误删每页相同的正文）
        nodes = [
            DocumentNode("paragraph", "每页相同提示", {"page": 1, "bbox": (72, 300, 400, 320)}),
            DocumentNode("paragraph", "每页相同提示", {"page": 2, "bbox": (72, 300, 400, 320)}),
            DocumentNode("paragraph", "正文A", {"page": 1, "bbox": (72, 600, 500, 620)}),
            DocumentNode("paragraph", "正文B", {"page": 2, "bbox": (72, 600, 500, 620)}),
        ]
        out = remove_headers_footers(nodes, 2, 842)
        assert [n.text for n in out] == ["每页相同提示", "每页相同提示", "正文A", "正文B"]

    def test_bbox_top_zone_removed(self):
        # 重复行在页面顶部区域 → 剔除
        nodes = [
            DocumentNode("paragraph", "页眉固定", {"page": 1, "bbox": (72, 40, 200, 55)}),
            DocumentNode("paragraph", "页眉固定", {"page": 2, "bbox": (72, 40, 200, 55)}),
        ]
        out = remove_headers_footers(nodes, 2, 842)
        assert out == []

    def test_bbox_bottom_zone_removed(self):
        # 重复行在页面底部区域 → 剔除
        nodes = [
            DocumentNode("paragraph", "页码行", {"page": 1, "bbox": (300, 800, 330, 815)}),
            DocumentNode("paragraph", "页码行", {"page": 2, "bbox": (300, 800, 330, 815)}),
        ]
        out = remove_headers_footers(nodes, 2, 842)
        assert out == []

    def test_single_occurrence_kept(self):
        nodes = [DocumentNode("paragraph", "唯一出现行", {"page": 1}),
                 DocumentNode("paragraph", "正文A", {"page": 1}),
                 DocumentNode("paragraph", "正文B", {"page": 2})]
        out = remove_headers_footers(nodes, 2)
        assert [n.text for n in out] == ["唯一出现行", "正文A", "正文B"]

    def test_node_fully_removed(self):
        nodes = [DocumentNode("paragraph", "页眉固定", {"page": 1}),
                 DocumentNode("paragraph", "页眉固定", {"page": 2}),
                 DocumentNode("paragraph", "正文", {"page": 2})]
        out = remove_headers_footers(nodes, 2)
        assert [n.text for n in out] == ["正文"]


# ---------- 断行合并 ----------

class TestMergeBrokenLines:
    def test_merge_no_punct(self):
        nodes = [DocumentNode("paragraph", "第一行没有标点\n续接内容", {"page": 1})]
        merge_broken_lines(nodes)
        assert nodes[0].text == "第一行没有标点续接内容"
        assert "joinline" in nodes[0].meta["cleaned_flags"]

    def test_not_merge_after_sentence_end(self):
        nodes = [DocumentNode("paragraph", "第一行有句号。\n新段内容", {"page": 1})]
        merge_broken_lines(nodes)
        assert nodes[0].text == "第一行有句号。\n新段内容"

    def test_not_touch_code_and_table(self):
        nodes = [
            DocumentNode("code", "def f():\n    return 1", {"page": 1}),
            DocumentNode("table", "| a | b |\n|---|---|", {"page": 1}),
        ]
        merge_broken_lines(nodes)
        assert nodes[0].text == "def f():\n    return 1"
        assert nodes[1].text == "| a | b |\n|---|---|"

    def test_cross_node_merge(self):
        # 独立 block（单行节点）之间的断行合并：吸收链到句末标点终止，新段落独立
        nodes = [
            DocumentNode("paragraph", "第一块无标点", {"page": 1}),
            DocumentNode("paragraph", "续接内容", {"page": 1}),
            DocumentNode("paragraph", "段落收尾行。", {"page": 1}),
            DocumentNode("paragraph", "不应拼接", {"page": 1}),
        ]
        out = merge_broken_lines(nodes)
        assert [n.text for n in out] == ["第一块无标点续接内容段落收尾行。", "不应拼接"]
        assert "joinline" in out[0].meta["cleaned_flags"]

    def test_indent_starts_new_paragraph(self):
        # 后块带首行缩进（前导空格）→ 新段落，不合并
        nodes = [
            DocumentNode("paragraph", "上一行无标点", {"page": 1}),
            DocumentNode("paragraph", "  首行缩进新段落", {"page": 1}),
        ]
        merge_broken_lines(nodes)
        assert [n.text for n in nodes] == ["上一行无标点", "  首行缩进新段落"]

    def test_cross_page_not_merge(self):
        nodes = [
            DocumentNode("paragraph", "页尾无标点", {"page": 1}),
            DocumentNode("paragraph", "下一页内容", {"page": 2}),
        ]
        merge_broken_lines(nodes)
        assert [n.text for n in nodes] == ["页尾无标点", "下一页内容"]

    def test_english_space_inserted(self):
        nodes = [DocumentNode("paragraph", "This is a broken", {"page": 1}),
                 DocumentNode("paragraph", "line", {"page": 1})]
        merge_broken_lines(nodes)
        assert nodes[0].text == "This is a broken line"


# ---------- PdfParser 集成 ----------

class TestPdfParser:
    def test_text_pdf_paragraphs(self, tmp_path):
        pdf = tmp_path / "doc.pdf"
        make_pdf(pdf, pages=2, header="公司内部资料", footer="第X页")
        parser = PdfParser(vlm=FakeVLM())
        nodes = parser.parse(pdf)
        assert nodes, "应有节点"
        assert all(n.meta["page"] in (1, 2) for n in nodes)
        # 页眉 2 页重复 → 被清
        assert all("公司内部资料" not in n.text for n in nodes)

    def test_image_block_vlm_and_order(self, tmp_path):
        pdf = tmp_path / "img.pdf"
        make_pdf(pdf, pages=1, with_image=True)
        fake = FakeVLM()
        parser = PdfParser(vlm=fake, page_text_threshold=1)
        nodes = parser.parse(pdf)
        types = [n.type for n in nodes]
        assert types[0] == "image", "图片块 y 坐标靠上应排前"
        assert "paragraph" in types
        img = nodes[0]
        assert "架构图描述" in img.text and "模块A" in img.text
        assert fake.calls == 1
        assert img.meta["bbox"], "应有坐标元数据"

    def test_scanned_pdf_rejected_without_vlm(self, tmp_path):
        """扫描型且未配置 VLM → ParseError（不静默产出乱码）。"""
        pdf = tmp_path / "scan.pdf"
        make_pdf(pdf, pages=5, with_image=True, with_text=False)
        with pytest.raises(ParseError, match="扫描型"):
            PdfParser().parse(pdf)

    def test_max_pages_rejected(self, tmp_path):
        pdf = tmp_path / "big.pdf"
        make_pdf(pdf, pages=3)
        with pytest.raises(ParseError, match="页数超限"):
            PdfParser(vlm=FakeVLM(), max_pages=2).parse(pdf)

    def test_vlm_failure_placeholder(self, tmp_path):
        pdf = tmp_path / "fail.pdf"
        make_pdf(pdf, pages=1, with_image=True)
        fake = FakeVLM(raise_exc=RuntimeError("boom"))
        nodes = PdfParser(vlm=fake, page_text_threshold=1).parse(pdf)
        img = next(n for n in nodes if n.type == "image")
        assert img.text == "[图片解析失败]"
        assert fake.calls == 1

    def test_vlm_none_defaults_to_real(self, tmp_path):
        """未注入 VLM 时懒加载真实客户端（不实际调用，仅确认可构建）。"""
        pdf = tmp_path / "plain.pdf"
        make_pdf(pdf, pages=1)
        nodes = PdfParser().parse(pdf)
        assert nodes and nodes[0].type == "paragraph"

    def test_empty_page_no_crash(self, tmp_path):
        pdf = tmp_path / "blank.pdf"
        doc = pymupdf.open()
        doc.new_page(width=595, height=842)  # 空页
        doc.save(pdf)
        doc.close()
        nodes = PdfParser(vlm=FakeVLM()).parse(pdf)
        assert nodes == []
