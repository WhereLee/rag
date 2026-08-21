"""
Markdown 渲染器（第二轮 S4，方案 §4.5）：块数组 → 页 MD → 整篇 MD。

纯函数无副作用：块 JSON 是源真相，改渲染规则不需要重跑解析。
渲染规则（规则识别模块，用户指定方向）：
- TEXT 块 → 原文段落（保留换行）
- TABLE 块 → 直接嵌入其 markdown 表格
- IMAGE 块 → `![图N：description]` + 图内文字引用块
- 失败块 → 页内注释 `<!-- 第N块识别失败：原因 -->` 不中断文档流
- 页间用水平分隔线 + 页码注释分隔，保证顺序稳定
"""
from __future__ import annotations

from ingest.block_types import Block, BlockType, PageResult, BLOCK_OK

# 页内块顺序调整：图片描述作为旁注（避免打断正文阅读），表格紧随其上下文
_PAGE_SEPARATOR = "\n\n---\n\n<!-- page {page_no} -->\n\n"
_IMAGE_PLACEHOLDER = "![图{seq}：{desc}]"


def _render_block(block: Block, seq: int) -> str:
    """单个块 → MD 片段（纯函数）。失败块统一转注释，不产生脏内容。"""
    if block.status != BLOCK_OK:
        reason = (block.error or block.status or "解析失败").strip()
        return f"<!-- 第{block.order}块({block.type.value})识别失败：{reason} -->\n"
    if block.type == BlockType.TEXT:
        return block.text.rstrip() + "\n"
    if block.type == BlockType.TABLE:
        return block.text.rstrip() + "\n"
    if block.type == BlockType.IMAGE:
        desc = (block.text or "图").strip() or f"图{seq}"
        parts = [_IMAGE_PLACEHOLDER.format(seq=seq, desc=desc)]
        txt = (block.meta or {}).get("text_in_image", "")
        if txt:
            parts.append(f"> 图中文字：{txt[:300]}")
        return "\n".join(parts) + "\n"
    # 兜底（未知类型）
    return f"<!-- 第{block.order}块无法渲染（{block.type.value}） -->\n"


def render_page(page_result: PageResult) -> str:
    """块数组 → 页 MD（块序稳定，失败块转注释不中断流）。"""
    parts: list[str] = []
    seq = 0
    for block in page_result.sorted_blocks():
        seq += 1
        parts.append(_render_block(block, seq))
    return "\n".join(parts).strip() + "\n"


def render_document(pages: list[PageResult | dict]) -> str:
    """页列表 → 整篇 MD（按 page_no 排序，页间分隔）。兼容 dict 或 PageResult。"""
    parts: list[str] = []
    for p in pages:
        pr = p if isinstance(p, PageResult) else _from_legacy(p)
        parts.append(_PAGE_SEPARATOR.format(page_no=pr.page_no) + render_page(pr))
    return "\n".join(parts).strip() + "\n"


def _from_legacy(p: dict) -> PageResult:
    """旧 page dict → PageResult（兼容 render_document 消费旧结构）。"""
    blocks: list[Block] = []
    order = 0
    text = (p.get("text") or "").strip()
    if text:
        blocks.append(Block(page_no=p.get("page_no", 0), order=order,
                            type=BlockType.TEXT, bbox=[0, 0, 0, 0], text=text))
        order += 1
    tables = p.get("tables") or []
    for t in tables:
        blocks.append(Block(page_no=p.get("page_no", 0), order=order,
                            type=BlockType.TABLE, bbox=[0, 0, 0, 0], text=t))
        order += 1
    images = p.get("images") or []
    for img in images:
        blocks.append(Block(page_no=p.get("page_no", 0), order=order,
                            type=BlockType.IMAGE, bbox=[0, 0, 0, 0],
                            text=img.get("description", ""),
                            meta={"text_in_image": img.get("text_in_image", "")}))
        order += 1
    return PageResult(page_no=p.get("page_no", 0), page_status="ok", channel="A",
                      blocks=blocks, errors=[])