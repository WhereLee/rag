"""清洗管线（分级规则 + cleaning_flags 可追溯）。

规则按节点类型差异化执行：
- code / table：只做换行符统一 + 控制字符剔除（不动空格/缩进——代码缩进与表格列对齐有语义）
- 其余类型：全量规则（空白规范化 + 控制字符 + 全角/不间断空格归一 + 连续空行压缩）
每处修改记录 flag，便于排查"为什么这个 chunk 是空的"。
"""
from __future__ import annotations

import re

from ..parser.base import DocumentNode

# 控制字符（保留 \n \t \r 前的控制字符剔除；\r 随后在换行统一中处理）
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
# 零宽字符：ZWSP/ZWNJ/ZWJ/BOM
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\ufeff]")
# 特殊空白：全角空格 / 不间断空格 / 窄不换行空格 / 表意空格
_SPECIAL_SPACE_RE = re.compile(r"[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]")
# 连续普通空格（≥2）压缩为 1
_MULTI_SPACE_RE = re.compile(r" {2,}")
# 连续空行（≥3）压缩为 2
_MULTI_BLANK_RE = re.compile(r"\n{3,}")

# 逐行缩进/表格保留类型：不做空格类压缩
_PRESERVE_SPACE_TYPES = {"code", "table"}


def clean_node(node: DocumentNode) -> DocumentNode:
    """按节点类型清洗 text，清洗标记写入 meta.cleaned_flags。"""
    text = node.text
    flags: list[str] = []

    # 1. 换行符统一（所有类型）
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        flags.append("nl")

    # 2. 控制字符 + 零宽剔除（所有类型）
    new = _CTRL_RE.sub("", text)
    if new != text:
        text = new
        flags.append("ctrl")
    new = _ZERO_WIDTH_RE.sub("", text)
    if new != text:
        text = new
        flags.append("zw")

    # 3. 空格类规则（code/table 保留缩进与对齐）
    if node.type not in _PRESERVE_SPACE_TYPES:
        new = _SPECIAL_SPACE_RE.sub(" ", text)
        if new != text:
            text = new
            flags.append("space")
        new = _MULTI_SPACE_RE.sub(" ", text)
        if new != text:
            text = new
            flags.append("mspace")

    # 4. 连续空行压缩（所有类型，段落结构保留）
    new = _MULTI_BLANK_RE.sub("\n\n", text)
    if new != text:
        text = new
        flags.append("blank")

    # 5. 整体 strip（code 跳过：首行缩进有语义）
    if node.type != "code":
        new = text.strip()
        if new != text:
            text = new
            flags.append("strip")

    if flags:
        node.text = text
        node.meta["cleaned_flags"] = flags
    return node


def clean_nodes(nodes: list[DocumentNode]) -> list[DocumentNode]:
    """遍历清洗整棵节点列表。"""
    return [clean_node(n) for n in nodes]
