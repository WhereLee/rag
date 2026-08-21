"""产物合理性校验：空产物 / 重复率 / 文本过短 / 失败占位统计。

返回问题列表（空列表 = 通过）；pipeline 据此判定 status。
"""
from __future__ import annotations

from collections import Counter
from typing import List

from .parser.base import DocumentNode

# 失败占位前缀（解析器/图片/VLM 降级产物）
_PLACEHOLDER_MARKERS = ("[图片解析失败]", "[图片无法解析]", "[该页解析失败]", "[该页转录为空]")

SUSPICIOUS_MIN_TEXT = 10  # 无表格无图片时，文本低于此长度 → 可疑


def validate_nodes(nodes: List[DocumentNode], source: str = "") -> List[str]:
    """产物校验 → 问题列表。"""
    issues: List[str] = []
    if not nodes:
        return ["空产物"]

    # 1. 重复率：相同文本节点占比（整页重复检测近似）；单节点文档不适用
    if len(nodes) > 1:
        counter = Counter(n.text for n in nodes)
        dup_ratio = max(counter.values()) / len(nodes)
        if dup_ratio > 0.9:
            issues.append(f"重复率过高（{dup_ratio:.0%} 节点内容相同），疑似重复页")

    # 2. 文本过短：无表格无图片时整文 < 10 字符 → 可疑
    has_table = any(n.type == "table" for n in nodes)
    has_image = any(n.type == "image" for n in nodes)
    total_len = sum(len(n.text) for n in nodes)
    if total_len < SUSPICIOUS_MIN_TEXT and not has_table and not has_image:
        issues.append(f"文本过短（{total_len} 字符且无表格/图片）")

    # 3. 失败占位统计（pipeline 用于判定 partial）
    placeholders = [n for n in nodes if any(n.text.startswith(m) for m in _PLACEHOLDER_MARKERS)]
    if placeholders:
        ratio = len(placeholders) / len(nodes)
        issues.append(f"{len(placeholders)}/{len(nodes)} 节点降级为失败占位（{ratio:.0%}）")
    return issues
