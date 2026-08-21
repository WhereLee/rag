"""C1 单元测试：结构感知切块（heading 分组 / 段落合并 / 表格行组 / 超长切分 / 去重）。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ingest.chunker import (OVERLAP_CHARS, TARGET_CHARS, Chunk, chunk_nodes,
                            _is_junk, _split_by_lines, _split_long,
                            _split_table)
from ingest.parser.base import DocumentNode, rows_to_markdown


def P(text, **meta):
    return DocumentNode("paragraph", text, meta)


def H(text, level=1, **meta):
    return DocumentNode("heading", text, {"level": level, **meta})


# ---------- heading 分组 / 标题路径 ----------

class TestHeadingPath:
    def test_nested_levels(self):
        chunks = chunk_nodes([
            H("第一章", 1),
            H("1.1 背景", 2),
            P("正文一"),
            H("1.2 目标", 2),
            P("正文二"),
        ])
        assert [c.heading_path for c in chunks] == ["第一章 > 1.1 背景", "第一章 > 1.2 目标"]

    def test_same_level_replaces(self):
        chunks = chunk_nodes([H("一", 1), P("A"), H("二", 1), P("B")])
        assert [c.heading_path for c in chunks] == ["一", "二"]

    def test_level_up_pops(self):
        chunks = chunk_nodes([
            H("A", 1), H("A.1", 2), H("A.1.1", 3), P("深"),
            H("A.2", 2), P("浅"), H("B", 1), P("顶"),
        ])
        assert [c.heading_path for c in chunks] == ["A > A.1 > A.1.1", "A > A.2", "B"]
        assert [c.content for c in chunks] == ["深", "浅", "顶"]

    def test_no_level_same_rank(self):
        # level 缺失 → 与栈顶同级（替换标题，不压栈）
        chunks = chunk_nodes([H("A", 1), DocumentNode("heading", "B", {}), P("x")])
        assert chunks[0].heading_path == "B"

    def test_no_heading_path_empty(self):
        chunks = chunk_nodes([P("没有标题的正文"), P("继续")])
        assert all(c.heading_path == "" for c in chunks)

    def test_heading_itself_not_chunk(self):
        chunks = chunk_nodes([H("只有标题", 1), H("没有正文", 2)])
        assert chunks == []


# ---------- paragraph 合并 ----------

class TestParagraphMerge:
    def test_short_merge(self):
        chunks = chunk_nodes([P("短一。"), P("短二。"), P("短三。")])
        assert len(chunks) == 1
        assert chunks[0].content == "短一。\n短二。\n短三。"

    def test_long_own_chunk(self):
        text = "长" * 100 + "。"
        chunks = chunk_nodes([P(text), P("短段落")])
        assert len(chunks) == 2
        assert chunks[0].content == text

    def test_merge_cross_boundary(self):
        # 合并累计到 500+ 触发 flush（6 段 ≈ 546 成块，余 1 段成第二块）
        nodes = [P("短" * 90 + "。")] * 7   # 7×91 ≈ 637 > 500
        chunks = chunk_nodes(nodes)
        assert len(chunks) == 2
        assert 500 <= len(chunks[0].content) <= 700
        assert chunks[1].content.endswith("。")

    def test_non_paragraph_breaks_merge(self):
        nodes = [P("短一"), DocumentNode("list", "项一\n项二", {}), P("短二")]
        chunks = chunk_nodes(nodes)
        assert [c.chunk_type for c in chunks] == ["paragraph", "list", "paragraph"]

    def test_adjacent_lists_merge(self):
        # txt_md 每项一个 list 节点 → 相邻 list 合并为一个块
        nodes = [DocumentNode("list", "项一", {}), DocumentNode("list", "项二", {}),
                 DocumentNode("list", "项三", {})]
        chunks = chunk_nodes(nodes)
        assert len(chunks) == 1
        assert chunks[0].content == "项一\n项二\n项三"

    def test_list_split_when_over(self):
        # 相邻 list 合并超过目标大小 → 按列表项边界切分
        nodes = [DocumentNode("list", f"第{i}个列表项内容" * 30, {}) for i in range(6)]
        chunks = chunk_nodes(nodes)
        assert len(chunks) >= 2
        assert all(c.chunk_type == "list" for c in chunks)


# ---------- 超长切分 ----------

class TestSplitLong:
    def test_short_no_split(self):
        assert _split_long("短文本。") == ["短文本。"]

    def test_sentence_split_with_overlap(self):
        text = "".join(f"这是第{i}个完整句子。" for i in range(60))  # ~660 字
        chunks = _split_long(text)
        assert len(chunks) >= 2
        assert all(len(c) <= TARGET_CHARS + 20 for c in chunks)
        # overlap：后块开头包含前块尾部字符
        assert chunks[1].startswith(chunks[0][-OVERLAP_CHARS:])

    def test_single_long_sentence_hard_split(self):
        text = "无" * 1300 + "标点"
        chunks = _split_long(text)
        assert all(len(c) <= TARGET_CHARS for c in chunks)
        assert chunks[1].startswith(chunks[0][-OVERLAP_CHARS:])

    def test_newline_boundary(self):
        text = "第一行内容。\n第二行内容。\n" + "长" * 600
        chunks = _split_long(text)
        assert len(chunks) >= 2

    def test_line_split_single_long_line(self):
        # 单行超长（无换行长串）按字符硬切，不产生空块
        text = "无换行的超长列表项内容" * 100
        chunks = _split_by_lines(text)
        assert all(0 < len(c) <= TARGET_CHARS for c in chunks)
        assert len(chunks) >= 2


# ---------- 表格行组 ----------

class TestSplitTable:
    def test_small_table_whole(self):
        rows = [["列A", "列B"]] + [[f"v{i}", str(i)] for i in range(3)]
        md = rows_to_markdown(rows)
        assert _split_table(md) == [md]

    def test_big_table_groups(self):
        rows = [["列A", "列B"]] + [[f"v{i}", str(i)] for i in range(25)]  # 26 行
        md = rows_to_markdown(rows)
        groups = _split_table(md)
        assert len(groups) == 3   # 10 + 10 + 5
        header = groups[0].splitlines()[0]
        assert all(g.splitlines()[0] == header for g in groups)  # 表头保留
        assert all(len(g.splitlines()) <= 12 for g in groups)    # 表头+分隔+≤10 行

    def test_table_chunks_type(self):
        rows = [["a"]] + [["v"]] * 15
        chunks = chunk_nodes([DocumentNode("table", rows_to_markdown(rows), {})])
        assert len(chunks) == 2
        assert all(c.chunk_type == "table" for c in chunks)


# ---------- list / code ----------

class TestListCode:
    def test_list_split_by_items(self):
        text = "\n".join(f"第{i}个列表项内容" for i in range(100))
        chunks = _split_by_lines(text)
        assert len(chunks) >= 2
        assert all(c.endswith("列表项内容") for c in chunks)

    def test_code_indent_kept(self):
        text = "def f():\n    x = 1\n    return x\n" * 50
        chunks = chunk_nodes([DocumentNode("code", text, {})])
        assert len(chunks) >= 2
        assert all("    x = 1" in c.content for c in chunks)  # 缩进未被 strip

    def test_image_own_chunk(self):
        nodes = [P("图片前的段落。"), DocumentNode("image", "一张系统架构图，包含网关和数据库", {}),
                 P("图片后的段落。")]
        chunks = chunk_nodes(nodes)
        assert [c.chunk_type for c in chunks] == ["paragraph", "image", "paragraph"]
        assert chunks[1].chars > 0


# ---------- 清理：junk / 去重 / 空输入 ----------

class TestClean:
    def test_empty_input(self):
        assert chunk_nodes([]) == []

    def test_junk_removed(self):
        assert _is_junk("")
        assert _is_junk("   ")
        assert _is_junk("……——【】")
        assert not _is_junk("有效内容")

    def test_junk_nodes_skipped(self):
        chunks = chunk_nodes([P("..."), P("　　"), P("有效内容")])
        assert len(chunks) == 1

    def test_duplicate_removed(self):
        dup = "相同内容。" * 20   # 100 字，独立成块（不参与合并）
        other = "其他内容。" * 20
        chunks = chunk_nodes([P(dup), P(other), P(dup)])
        assert len(chunks) == 2
        assert [c.content for c in chunks] == [dup, other]

    def test_page_no_carried(self):
        chunks = chunk_nodes([P("带页码内容", page=3)])
        assert chunks[0].page_no == 3

    def test_merged_page_no_first(self):
        # 合并块页码取首段；中间出现 heading 不影响
        nodes = [P("短一", page=2), H("标题", 1), P("短二", page=4)]
        chunks = chunk_nodes(nodes)
        assert chunks[0].page_no == 2
        assert chunks[1].page_no == 4

    def test_seq_incremental(self):
        # 长段落独立成块（不参与合并），seq 全局递增
        chunks = chunk_nodes([P("一" * 100 + "。"), P("二" * 100 + "。"), P("三" * 100 + "。")])
        assert [c.seq for c in chunks] == [1, 2, 3]

    def test_chunk_chars(self):
        c = chunk_nodes([P("一百二十字" * 20)])[0]   # 100 字 ≥ MIN_OWN_CHUNK
        assert c.chars == len(c.content)
