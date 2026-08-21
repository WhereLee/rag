"""
解析块协议（第二轮修复核心数据结构）。

统一"页内块"的表示：文本块 / 表格块 / 图片块（带坐标、类型、顺序、状态），
以及页级产物 PageResult 与旧结构（page dict）的相互转换。

设计要点（方案文档 §4.1 + 审查修订 R6）：
- 块是解析与渲染之间的"源真相"：Markdown 是渲染视图，块数据才是真相
- 页码 + order 是排序锚点，保证整篇文档的顺序稳定
- 每块独立状态，坏块隔离不拖死同页好块（分级容错的基础）
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# 块类型
class BlockType(str, Enum):
    TEXT = "text"       # 直提文本（零成本）
    TABLE = "table"     # VLM 结构化 markdown 表格
    IMAGE = "image"     # VLM 语义描述

# 块状态（分级容错状态机）
# ok / failed(render) / failed(validate) / failed(vlm) / unfixable
BLOCK_OK = "ok"
BLOCK_FAILED = "failed"
BLOCK_UNFIXABLE = "unfixable"


def jsonable(obj: Any) -> Any:
    """dataclass/Enum/enum 转 JSON 可序列化对象。"""
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, Enum):
        return obj.value
    if hasattr(obj, "__dataclass_fields__"):
        return {k: jsonable(v) for k, v in vars(obj).items()}
    return obj


@dataclass
class Block:
    """单个块。"""
    page_no: int               # 0-based 页码
    order: int                 # 页内阅读顺序（0 起，渲染/切块依赖）
    type: BlockType            # 类型
    bbox: list                 # [x0, y0, x1, y1] 页面坐标
    text: str = ""             # TEXT 内容 / TABLE 的 markdown / IMAGE 的描述
    meta: dict = field(default_factory=dict)   # image 的 text_in_image 等
    status: str = BLOCK_OK     # 处理状态
    error: str = ""            # 失败原因（人话化文案）

    def to_dict(self) -> dict:
        d = {"type": self.type.value, "order": self.order,
             "bbox": list(self.bbox), "text": self.text,
             "meta": jsonable(self.meta), "status": self.status,
             "error": self.error}
        return d


@dataclass
class PageResult:
    """页级产物（对外 JSON 结构，方案文档 §4.1）。"""
    page_no: int
    page_status: str      # ok / partial / failed / skipped
    channel: str = ""     # A（文本直提）B（文本+表格VLM）C（整页转录）M（混合=文本+图块VLM）
    blocks: list = field(default_factory=list)   # list[Block]，按 order 排序
    errors: list = field(default_factory=list)   # [{"block_order":N,"kind":...,"detail":...}]

    def to_dict(self) -> dict:
        return {
            "page_no": self.page_no,
            "page_status": self.page_status,
            "channel": self.channel,
            "blocks": [b.to_dict() for b in self.blocks],
            "errors": jsonable(self.errors),
        }

    def sorted_blocks(self) -> list:
        return sorted(self.blocks, key=lambda b: b.order)


# ---------------------------------------------------------------- 兼容层（审查修订 R6）

def to_legacy_page(page_result: PageResult) -> dict:
    """PageResult → 旧 page dict，保持 sync_service/chunker 消费结构不变。

    旧结构：{"page_no", "channel", "text", "tables", "images"}
    转换规则：
      - 文本块 → 拼入 text
      - 表格块 → 表格 markdown 追加到 tables
      - 图片块 → {"description","text_in_image"} 追加到 images
      - 失败块 → 跳过（不入库），错误信息已入 errors 供 step_detail
    """
    text_parts: list[str] = []
    tables: list[str] = []
    images: list[dict] = []
    for b in page_result.sorted_blocks():
        if b.status != BLOCK_OK:
            continue  # 失败块不进入旧结构（内容维护完整性）
        if b.type == BlockType.TEXT:
            text_parts.append(b.text)
        elif b.type == BlockType.TABLE:
            tables.append(b.text)
        elif b.type == BlockType.IMAGE:
            images.append({"description": b.text,
                           "text_in_image": (b.meta or {}).get("text_in_image", "")})
    # 兼容旧字段：stats 里的 figures_parsed 依赖 images 数量
    return {"page_no": page_result.page_no,
            "channel": page_result.channel or "A",
            "text": "\n".join(text_parts),
            "tables": tables,
            "images": images}