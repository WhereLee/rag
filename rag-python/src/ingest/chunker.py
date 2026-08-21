"""结构感知切块：DocumentNode → Chunk（检索最小单元）。

设计（对应 C1 计划，参数与调研结论对齐）：
- heading 分组：标题开新组，level 栈维护层级，标题路径注入 heading_path（检索/问答上下文，
  调研实证"标题前缀注入"是检索单点优化）；heading 自身不成块——标题信息完整进入 heading_path，
  避免纯标题块占用检索名额
- paragraph 合并：同组相邻段落合并至目标 500 字符；单段 ≥100 直接成块；超长段落按句边界
  （。！？换行）切分并带 overlap 50（10%）
- table 行组：表头 + 最多 10 数据行一组，组间保留表头（块自包含，避免表格被切碎稀释）
- image 独立成块（VLM 描述即内容）；list 按列表项边界切；code 按行切且保留缩进
- 空块/纯标点剔除；content 完全相同去重（保留首个）
- 空输入/无可检索内容 → []（不抛错，由调用方决定状态；解析成功但 0 块不视为失败）
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .parser.base import DocumentNode

TARGET_CHARS = 500     # 目标块大小（字符；中文 ≈0.6 token/字，落在 200-500 token 推荐区间）
OVERLAP_CHARS = 50     # 超长切分重叠（10%，防边界截断关键信息）
MIN_OWN_CHUNK = 100    # 单段落达到此长度独立成块（小于此参与合并）
TABLE_ROWS = 10        # 表格数据行一组（表头保留）

_SENT_BOUND = re.compile(r"(?<=[。！？\n])")   # 句边界（后视断言保留分隔符）
_PUNCT_ONLY = re.compile(r"[\s\W_]+")          # 非内容字符集（\W 含中文标点，中文是 \w）


@dataclass
class Chunk:
    """检索最小单元。heading_path 为标题路径（"第一章 > 1.1"），检索打分与上下文组装时拼入。"""

    chunk_type: str
    seq: int
    content: str
    chars: int
    heading_path: str = ""
    page_no: Optional[int] = None


def chunk_nodes(nodes: List[DocumentNode]) -> List[Chunk]:
    """结构感知切块。返回 [] 表示无可检索内容（空文档/纯标题文档）。"""
    chunks: List[Chunk] = []
    stack: List[Tuple[int, str]] = []   # (level, title) 栈，维护当前标题路径
    pending: List[Tuple[str, str, Optional[int]]] = []  # (kind, text, page_no)，保留文档顺序
    seq = 0

    def heading_path() -> str:
        return " > ".join(t for _, t in stack)

    def make(t: str, text: str, page_no: Optional[int] = None) -> Chunk:
        nonlocal seq
        seq += 1
        return Chunk(chunk_type=t, seq=seq, content=text, chars=len(text),
                     heading_path=heading_path(), page_no=page_no)

    def flush_pending() -> None:
        """连续同类节点合并输出：短段落合并；相邻 list 合并（txt_md 每项一个节点）。
        按 kind 连续分组，保持文档原始顺序。页码取组内首项。"""
        if not pending:
            return
        for kind, group in itertools.groupby(pending, key=lambda x: x[0]):
            items = list(group)
            joined = "\n".join(t for _, t, _ in items)
            if kind == "paragraph":
                chunks.append(make("paragraph", joined, items[0][2]))
            else:
                for seg in _split_by_lines(joined):
                    chunks.append(make("list", seg, items[0][2]))
        pending.clear()

    for n in nodes:
        text = (n.text or "").strip()
        if not text or _is_junk(text):
            continue
        page = n.meta or {}
        if n.type == "heading":
            flush_pending()
            lv = page.get("level")
            if lv is None:
                # 无层级信息 → 与栈顶同级（替换标题文本）；栈空则视为一级
                if stack:
                    stack[-1] = (stack[-1][0], text)
                else:
                    stack.append((1, text))
            else:
                while stack and lv <= stack[-1][0]:
                    stack.pop()
                stack.append((lv, text))
            continue

        if n.type == "paragraph":
            if len(text) >= MIN_OWN_CHUNK:
                flush_pending()
                for seg in _split_long(text):
                    chunks.append(make("paragraph", seg, page.get("page")))
            else:
                pending.append(("paragraph", text, page.get("page")))
                if sum(len(t) for _, t, _ in pending) >= TARGET_CHARS:
                    flush_pending()
            continue

        # list 可与相邻 list 合并（txt_md 每项一个节点），不打断；groupby 保证与段落分组
        if n.type == "list":
            pending.append(("list", text, page.get("page")))
            if sum(len(t) for _, t, _ in pending) >= TARGET_CHARS:
                flush_pending()
            continue
        # 其余非段落节点打断合并
        flush_pending()
        if n.type == "table":
            for part in _split_table(text):
                chunks.append(make("table", part, page.get("page")))
        elif n.type == "image":
            chunks.append(make("image", text, page.get("page")))
        elif n.type == "code":
            for seg in _split_by_lines(text):
                chunks.append(make("code", seg, page.get("page")))

    flush_pending()
    return _dedupe(chunks)


def _is_junk(text: str) -> bool:
    """空/纯空白/纯标点（页眉残留、分隔符等无信息量内容）。"""
    return not _PUNCT_ONLY.sub("", text).strip()


def _split_long(text: str, target: int = TARGET_CHARS,
                overlap: int = OVERLAP_CHARS) -> List[str]:
    """超长文本按句边界贪心切分；overlap 取上一块尾部字符，防止边界截断语义。"""
    if len(text) <= target:
        return [text]
    parts = [p for p in _SENT_BOUND.split(text) if p]
    chunks: List[str] = []
    cur = ""
    for p in parts:
        if len(p) > target:
            # 单句超长（无标点的长串）→ 按字符硬切
            if cur:
                chunks.append(cur)
            cur = p
            while len(cur) > target:
                chunks.append(cur[:target])
                cur = cur[target - overlap:]
        elif len(cur) + len(p) <= target:
            cur += p
        else:
            if cur:
                chunks.append(cur)
            cur = (cur[-overlap:] if overlap else "") + p
    if cur:
        chunks.append(cur)
    return chunks


def _split_table(text: str, rows_per_group: int = TABLE_ROWS) -> List[str]:
    """Markdown 表格按行组切分：表头行 + 分隔行 + 每组合计 rows_per_group 数据行。
    组间保留表头（每块自包含）；表格本身小（≤表头+10 数据行）则整体成块。"""
    lines = text.splitlines()
    if len(lines) <= rows_per_group + 2:
        return [text]
    header = "\n".join(lines[:2])   # 表头行 + 分隔行
    data = lines[2:]
    return [header + "\n" + "\n".join(data[i:i + rows_per_group])
            for i in range(0, len(data), rows_per_group)]


def _split_by_lines(text: str, target: int = TARGET_CHARS) -> List[str]:
    """list/code 按行边界切分：行是天然语义单元；code 行保留缩进（不 strip）。
    单行超长（无换行长串）按字符硬切，避免空块与超长块。"""
    if len(text) <= target:
        return [text]
    lines = text.splitlines()
    chunks: List[str] = []
    cur = ""
    for ln in lines:
        if len(ln) > target:
            if cur:
                chunks.append(cur)
            cur = ln
            while len(cur) > target:
                chunks.append(cur[:target])
                cur = cur[target:]
        elif len(cur) + len(ln) + 1 <= target:
            cur = cur + "\n" + ln if cur else ln
        else:
            if cur:
                chunks.append(cur)
            cur = ln
    if cur:
        chunks.append(cur)
    return chunks


def _dedupe(chunks: List[Chunk]) -> List[Chunk]:
    """content 完全相同去重（保留首个）。表格行组仅表头重复不触发（数据行不同）。"""
    seen = set()
    out: List[Chunk] = []
    for c in chunks:
        if c.content in seen:
            continue
        seen.add(c.content)
        out.append(c)
    return out
