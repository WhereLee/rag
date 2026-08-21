"""txt / md 解析器。

- 编码检测：UTF-8（含 BOM）优先，解码异常回退 GBK——中文环境两大现实来源
- md 保留结构：标题层级/列表/代码块/表格 → 对应节点类型，供切块层结构感知切分
- txt 按空行分段 → 多个 paragraph 节点
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List

from .base import DocumentNode, ParseError, Parser

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+\.)\s+(.*)$")
_FENCE_RE = re.compile(r"^```|^~~~")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")


def decode_text(raw: bytes, source: str) -> str:
    """编码检测：UTF-8（utf-8-sig 兼容 BOM）→ GBK 回退；都失败抛 ParseError。"""
    for enc in ("utf-8-sig", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ParseError(f"{source}: 编码无法识别（非 UTF-8/GBK）")


def split_paragraphs(text: str) -> List[str]:
    """按空行切段，strip 每段，丢弃空段。"""
    return [seg.strip() for seg in text.split("\n\n") if seg.strip()]


class TxtMdParser(Parser):
    """txt/md 统一解析：md 按标记分节点，txt 按空行分段。"""

    def parse(self, path: Path) -> List[DocumentNode]:
        path = Path(path)
        raw = path.read_bytes()
        text = decode_text(raw, path.name)
        # 换行统一（Windows/旧 Mac 文件带 \r\n、\r）：分段/正则都按 \n 处理的前提
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        source = path.name

        if path.suffix.lower() == ".md":
            nodes = self._parse_markdown(text, source)
        else:
            nodes = [
                DocumentNode("paragraph", seg, {"source": source, "index": i})
                for i, seg in enumerate(split_paragraphs(text))
            ]
        return nodes

    def _parse_markdown(self, text: str, source: str) -> List[DocumentNode]:
        nodes: List[DocumentNode] = []
        lines = text.split("\n")
        i = 0
        n = len(lines)
        index = 0
        while i < n:
            line = lines[i]
            stripped = line.strip()

            # 代码块（围栏）
            if _FENCE_RE.match(stripped):
                buf = [line]
                i += 1
                while i < n and not _FENCE_RE.match(lines[i].strip()):
                    buf.append(lines[i])
                    i += 1
                if i < n:  # 闭合围栏
                    buf.append(lines[i])
                    i += 1
                nodes.append(DocumentNode("code", "\n".join(buf),
                                          {"source": source, "index": index}))
                index += 1
                continue

            # 标题
            m = _HEADING_RE.match(stripped)
            if m:
                nodes.append(DocumentNode("heading", m.group(2).strip(),
                                          {"source": source, "index": index,
                                           "level": len(m.group(1))}))
                index += 1
                i += 1
                continue

            # 表格：行以 | 开头，且其后（或本身）存在分隔行 |---|
            if _TABLE_ROW_RE.match(stripped) and self._looks_like_table(lines, i):
                buf = [line]
                i += 1
                while i < n and _TABLE_ROW_RE.match(lines[i].strip()):
                    buf.append(lines[i])
                    i += 1
                nodes.append(DocumentNode("table", "\n".join(buf),
                                          {"source": source, "index": index}))
                index += 1
                continue

            # 列表
            if _LIST_RE.match(line):
                nodes.append(DocumentNode("list", stripped,
                                          {"source": source, "index": index}))
                index += 1
                i += 1
                continue

            # 空行跳过
            if not stripped:
                i += 1
                continue

            # 普通段落
            nodes.append(DocumentNode("paragraph", stripped,
                                      {"source": source, "index": index}))
            index += 1
            i += 1

        return nodes

    @staticmethod
    def _looks_like_table(lines: List[str], i: int) -> bool:
        """当前行是表头时：下一行是 |---| 分隔行，或当前行本身是分隔行。"""
        if _TABLE_SEP_RE.match(lines[i].strip()):
            return True
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        return "|" in nxt and _TABLE_SEP_RE.match(nxt) and "-" in nxt
