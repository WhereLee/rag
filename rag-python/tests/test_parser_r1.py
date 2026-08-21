"""R1 单元测试：中间格式 / txt/md 解析 / 清洗基础版。"""
from pathlib import Path

import pytest

from ingest.clean.cleaner import clean_node, clean_nodes
from ingest.parser.base import DocumentNode
from ingest.parser.txt_md import TxtMdParser, decode_text, split_paragraphs


# ---------- 中间格式 ----------

class TestDocumentNode:
    def test_invalid_type_rejected(self):
        with pytest.raises(ValueError):
            DocumentNode("unknown", "x")

    def test_to_text(self):
        n = DocumentNode("paragraph", "hello")
        assert n.to_text() == "hello"


# ---------- 编码检测 ----------

class TestDecode:
    def test_utf8(self):
        assert decode_text("你好".encode("utf-8"), "a.txt") == "你好"

    def test_utf8_with_bom(self):
        raw = b"\xef\xbb\xbf" + "你好".encode("utf-8")
        assert decode_text(raw, "a.txt") == "你好"

    def test_gbk_fallback(self):
        assert decode_text("中文GBK".encode("gbk"), "a.txt") == "中文GBK"

    def test_invalid_encoding_raises(self):
        with pytest.raises(Exception) as e:
            decode_text(b"\xff\xfe\x00\x81\x82", "a.txt")
        assert "编码" in str(e.value)


# ---------- txt ----------

class TestTxt:
    def test_split_paragraphs(self):
        assert split_paragraphs("a\n\nb\n\n\nc") == ["a", "b", "c"]

    def test_parse_paragraphs(self, tmp_path: Path):
        p = tmp_path / "a.txt"
        p.write_text("第一段。\n第二行。\n\n第二段。", encoding="utf-8")
        nodes = TxtMdParser().parse(p)
        assert [n.type for n in nodes] == ["paragraph", "paragraph"]
        assert nodes[0].text == "第一段。\n第二行。"

    def test_empty_file(self, tmp_path: Path):
        p = tmp_path / "empty.txt"
        p.write_text("", encoding="utf-8")
        assert TxtMdParser().parse(p) == []


# ---------- md ----------

class TestMarkdown:
    def _parse(self, content: str, tmp_path: Path):
        p = tmp_path / "a.md"
        p.write_text(content, encoding="utf-8")
        return TxtMdParser().parse(p)

    def test_headings_level(self, tmp_path: Path):
        nodes = self._parse("# 一级\n## 二级\n### 三级", tmp_path)
        assert [(n.type, n.meta.get("level")) for n in nodes] == [
            ("heading", 1), ("heading", 2), ("heading", 3)]

    def test_list_and_paragraph(self, tmp_path: Path):
        nodes = self._parse("- 项一\n- 项二\n\n普通段落", tmp_path)
        assert [n.type for n in nodes] == ["list", "list", "paragraph"]

    def test_code_block_preserved(self, tmp_path: Path):
        nodes = self._parse("```python\nprint(1)\n  indent\n```", tmp_path)
        assert nodes[0].type == "code"
        assert "print(1)" in nodes[0].text

    def test_table(self, tmp_path: Path):
        md = "| 名称 | 数量 |\n| --- | --- |\n| A | 1 |\n| B | 2 |"
        nodes = self._parse(md, tmp_path)
        assert nodes[0].type == "table"
        assert "| A | 1 |" in nodes[0].text

    def test_mixed_doc_order(self, tmp_path: Path):
        md = "# 标题\n\n正文。\n\n- 列表\n\n```\ncode\n```"
        nodes = self._parse(md, tmp_path)
        assert [n.type for n in nodes] == ["heading", "paragraph", "list", "code"]


# ---------- 清洗 ----------

class TestCleaner:
    def _clean(self, text: str, node_type: str = "paragraph") -> DocumentNode:
        return clean_node(DocumentNode(node_type, text))

    def test_crlf_unified(self):
        n = self._clean("a\r\nb\rc")
        assert n.text == "a\nb\nc"
        assert "nl" in n.meta["cleaned_flags"]

    def test_control_chars_removed(self):
        n = self._clean("a\x00b\x1fc")
        assert n.text == "abc"
        assert "ctrl" in n.meta["cleaned_flags"]

    def test_zero_width_removed(self):
        n = self._clean("a\u200bb\ufeffc")
        assert n.text == "abc"
        assert "zw" in n.meta["cleaned_flags"]

    def test_special_spaces_normalized(self):
        n = self._clean("a\u3000b\u00a0c")
        assert n.text == "a b c"
        assert "space" in n.meta["cleaned_flags"]

    def test_multi_spaces_compressed(self):
        n = self._clean("a   b    c")
        assert n.text == "a b c"

    def test_multi_blank_compressed(self):
        n = self._clean("a\n\n\n\nb")
        assert n.text == "a\n\nb"
        assert "blank" in n.meta["cleaned_flags"]

    def test_code_preserves_spaces(self):
        n = self._clean("    indent  keep  \n  more", "code")
        assert "    indent  keep" in n.text
        assert "mspace" not in n.meta.get("cleaned_flags", [])

    def test_table_preserves_spaces(self):
        n = self._clean("| a  | b  |\n| 1  | 2  |", "table")
        assert "| a  | b  |" in n.text

    def test_clean_nodes_returns_list(self):
        nodes = clean_nodes([DocumentNode("paragraph", "a\n\n\nb")])
        assert nodes[0].text == "a\n\nb"
