"""统一中间格式与解析器接口（最小管线重建）。

DocumentNode 是解析域的标准产物：所有格式的解析器都输出 DocumentNode 列表，
切块层基于节点类型做结构感知切分，meta 携带溯源信息（页码/层级/清洗标记）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List

# 节点类型全集：heading 标题 / paragraph 段落 / table 表格 / image 图片 / list 列表 / code 代码
NODE_TYPES = {"heading", "paragraph", "table", "image", "list", "code"}


@dataclass
class DocumentNode:
    """一个语义单元（块）。text 为清洗后的内容；image 节点 text 为 VLM 描述。"""

    type: str
    text: str
    meta: dict = field(default_factory=dict)
    # meta 常用键：source 来源文件名 / page 页码(1 起) / level 标题层级 / bbox 坐标 / index 序号 / cleaned_flags 清洗标记

    def __post_init__(self) -> None:
        if self.type not in NODE_TYPES:
            raise ValueError(f"非法节点类型: {self.type}")

    def to_text(self) -> str:
        """转纯文本（预览/清洗统计用）。"""
        return self.text


class ParseError(Exception):
    """解析失败（不可恢复，调用方标记文件失败）。"""


class Parser:
    """解析器基类：输入文件路径，输出 DocumentNode 列表。"""

    def parse(self, path: Path) -> List[DocumentNode]:
        raise NotImplementedError


def rows_to_markdown(rows: List[List[str]]) -> str:
    """二维行列表 → Markdown 表格（docx/xlsx/pptx 共用，行宽补齐）。"""
    if not rows:
        return ""
    ncol = max(len(r) for r in rows)
    padded = [r + [""] * (ncol - len(r)) for r in rows]
    lines = ["| " + " | ".join(padded[0]) + " |"]
    lines.append("|" + "|".join(["---"] * ncol) + "|")
    lines.extend("| " + " | ".join(r) + " |" for r in padded[1:])
    return "\n".join(lines)
