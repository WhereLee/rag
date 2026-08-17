"""
切块器：页级中间表示 → 检索块。

策略：
- 文本块：段落优先，≤500 字整段；>500 字按句子边界二分；相邻块 overlap 60 字
- 上下文注入：每块头部注入《文档标题》p.页码（Contextual Retrieval 思想，零成本）
- 表格块：整表一块；>800 字按行分组拆分并保留表头
- 图片块：描述 + 图中文字合并
"""
import re
from dataclasses import dataclass, field

MAX_CHUNK_CHARS = 500
OVERLAP_CHARS = 60
MAX_TABLE_CHARS = 800

SENT_END = re.compile(r"(?<=[。！？!?；;\n])")


@dataclass
class Chunk:
    chunk_type: str          # text / table / image
    page_no: int
    seq: int
    content: str
    meta: dict = field(default_factory=dict)


def _split_sentences(text: str) -> list[str]:
    return [s for s in SENT_END.split(text) if s.strip()]


def _split_long_text(text: str, limit: int) -> list[str]:
    """>limit 的文本按句子边界分组到 ≤limit。"""
    sentences = _split_sentences(text)
    parts, buf = [], ""
    for s in sentences:
        if len(buf) + len(s) > limit and buf:
            parts.append(buf)
            buf = s
        else:
            buf += s
        # 单句仍超长 → 硬切
        while len(buf) > limit:
            parts.append(buf[:limit])
            buf = buf[limit:]
    if buf:
        parts.append(buf)
    return parts


def _chunk_text_with_overlap(text: str, limit: int) -> list[str]:
    """段落流切块 + overlap。"""
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        # 超长段落先拆
        pieces = _split_long_text(para, limit) if len(para) > limit else [para]
        for piece in pieces:
            if len(buf) + len(piece) + 1 > limit and buf:
                chunks.append(buf)
                # overlap：取上一块尾部 OVERLAP_CHARS 字作为新块开头
                tail = buf[-OVERLAP_CHARS:] if len(buf) > OVERLAP_CHARS else buf
                buf = tail + piece
            else:
                buf = buf + "\n" + piece if buf else piece
        # 单 piece 超限时 _split_long_text 已保证 ≤limit
    if buf:
        chunks.append(buf)
    return chunks


def _split_table(md: str) -> list[str]:
    """超长 markdown 表格按行分组，每组保留表头。"""
    if len(md) <= MAX_TABLE_CHARS:
        return [md]
    lines = md.strip().split("\n")
    # 表头 = 前两行（标题行 + 分隔行）
    header = lines[:2] if len(lines) > 2 and set(lines[1].strip()) <= set("|-: ") else lines[:1]
    body = lines[len(header):]
    parts, cur = [], list(header)
    cur_len = sum(len(x) for x in cur)
    for line in body:
        if cur_len + len(line) > MAX_TABLE_CHARS and len(cur) > len(header):
            parts.append("\n".join(cur))
            cur = list(header)
            cur_len = sum(len(x) for x in cur)
        cur.append(line)
        cur_len += len(line)
    if len(cur) > len(header):
        parts.append("\n".join(cur))
    return parts


def chunk_document(title: str, pages: list[dict]) -> list[Chunk]:
    """页级中间表示 → Chunk 列表（seq 全局递增）。"""
    chunks: list[Chunk] = []
    seq = 0
    for page in pages:
        page_no = page["page_no"]
        ctx = f"《{title}》 p.{page_no + 1}："

        # 1. 文本
        text = (page.get("text") or "").strip()
        if text:
            for piece in _chunk_text_with_overlap(text, MAX_CHUNK_CHARS):
                chunks.append(Chunk("text", page_no, seq, ctx + piece))
                seq += 1

        # 2. 表格（VLM 结构化结果）
        for md in page.get("tables", []):
            for part in _split_table(md):
                chunks.append(Chunk("table", page_no, seq, ctx + "\n" + part,
                                    meta={"source": "vlm_table"}))
                seq += 1

        # 3. 图片
        for img in page.get("images", []):
            desc = img.get("description", "")
            txt = img.get("text_in_image", "")
            content = f"{ctx}[图] {desc}"
            if txt:
                content += f"\n图中文字：{txt[:300]}"
            chunks.append(Chunk("image", page_no, seq, content,
                                meta={"source": "vlm_image"}))
            seq += 1
    return chunks
