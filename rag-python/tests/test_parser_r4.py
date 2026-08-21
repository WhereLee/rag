"""R4 单元测试：扫描 PDF 整页转录 / 独立图片 VLM / 大图缩放 / 降级占位。"""
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pymupdf

from ingest.parser.base import DocumentNode
from ingest.parser.image import ImageParser
from ingest.parser.pdf import PdfParser
from ingest.parser.vlm import to_png_bytes


class FakeVLM:
    """图片→JSON + 图片→文本 双通道 fake。"""

    def __init__(self, obj=None, text="转录出的整页文字内容", raise_exc=None):
        self.obj = obj or {"description": "架构图描述", "text_in_image": "标签X"}
        self.text = text
        self.raise_exc = raise_exc
        self.json_calls = 0
        self.text_calls = 0

    def chat_image(self, prompt, png_bytes, cache_prefix):
        self.json_calls += 1
        if self.raise_exc:
            raise self.raise_exc
        return self.obj

    def chat_text(self, prompt, png_bytes, cache_prefix):
        self.text_calls += 1
        if self.raise_exc:
            raise self.raise_exc
        return self.text


def make_scanned_pdf(path, pages=3):
    doc = pymupdf.open()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 300, 200))
    pix.clear_with(200)
    for _ in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_image(pymupdf.Rect(72, 100, 372, 300), pixmap=pix)
    doc.save(path)
    doc.close()


def make_png_bytes(w=200, h=100) -> bytes:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h))
    pix.clear_with(180)
    return pix.tobytes("png")


class TestScannedPdf:
    def test_transcribe_per_page(self, tmp_path):
        pdf = tmp_path / "scan.pdf"
        make_scanned_pdf(pdf, pages=3)
        fake = FakeVLM(text="第一页内容")
        nodes = PdfParser(vlm=fake).parse(pdf)
        assert len(nodes) == 3, "每页一个节点"
        assert all(n.type == "paragraph" for n in nodes)
        assert all(n.text == "第一页内容" for n in nodes)
        assert fake.text_calls == 3
        assert [n.meta["page"] for n in nodes] == [1, 2, 3]
        assert all(n.meta["source"] == "scan.pdf" for n in nodes)

    def test_transcribe_failure_placeholder(self, tmp_path):
        pdf = tmp_path / "scan.pdf"
        make_scanned_pdf(pdf, pages=2)
        fake = FakeVLM(raise_exc=TimeoutError("boom"))
        nodes = PdfParser(vlm=fake).parse(pdf)
        assert len(nodes) == 2, "失败不中断"
        assert all(n.text == "[该页解析失败]" for n in nodes)

    def test_empty_transcript_placeholder(self, tmp_path):
        pdf = tmp_path / "scan.pdf"
        make_scanned_pdf(pdf, pages=1)
        fake = FakeVLM(text="   ")
        nodes = PdfParser(vlm=fake).parse(pdf)
        assert nodes[0].text == "[该页转录为空]"


class TestImageParser:
    def test_description(self, tmp_path):
        p = tmp_path / "photo.png"
        p.write_bytes(make_png_bytes())
        fake = FakeVLM()
        nodes = ImageParser(vlm=fake).parse(p)
        assert len(nodes) == 1 and nodes[0].type == "image"
        assert "架构图描述" in nodes[0].text and "标签X" in nodes[0].text
        assert fake.json_calls == 1
        assert nodes[0].meta["source"] == "photo.png"

    def test_failure_placeholder(self, tmp_path):
        p = tmp_path / "photo.png"
        p.write_bytes(make_png_bytes())
        fake = FakeVLM(raise_exc=RuntimeError("boom"))
        nodes = ImageParser(vlm=fake).parse(p)
        assert nodes[0].text == "[图片解析失败]"

    def test_corrupt_image_placeholder(self, tmp_path):
        p = tmp_path / "bad.png"
        p.write_bytes(b"not an image")
        fake = FakeVLM()
        nodes = ImageParser(vlm=fake).parse(p)
        assert nodes[0].text == "[图片解析失败]"
        assert fake.json_calls == 0, "转码失败不应调 VLM"


class TestToPngBytes:
    @staticmethod
    def _pixel_size(png: bytes):
        # 用 Pillow 读真实像素（pymupdf 读图片流会被 DPI 元数据换算）
        from PIL import Image

        return Image.open(io.BytesIO(png)).size

    def test_large_image_scaled(self):
        png = make_png_bytes(3000, 1500)  # 长边 3000 > 1568
        out = to_png_bytes(png)
        w, h = self._pixel_size(out)
        assert max(w, h) <= 1568
        assert abs(w / h - 3000 / 1500) < 0.05, "等比缩放"

    def test_small_image_unchanged(self):
        png = make_png_bytes(200, 100)
        out = to_png_bytes(png)
        assert self._pixel_size(out) == (200, 100)

    def test_custom_max_dim(self):
        png = make_png_bytes(500, 400)
        out = to_png_bytes(png, max_dim=250)
        w, h = self._pixel_size(out)
        assert max(w, h) <= 250
