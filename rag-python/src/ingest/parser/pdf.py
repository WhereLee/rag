"""PDF 解析器（文本型）：PyMuPDF 块分离 → 图片块 VLM → 坐标拼回 → 页眉页脚/断行清洗。

- 文本型判定：页均提取字符数 < 阈值（默认 20）→ 视为扫描型（本阶段抛 ParseError，R4 接入 VLM 整页转录）
- 块分离：page.get_text("dict") 的 blocks（type 0=文本块 / 1=图片块），各带 bbox
- 图片块：裁剪渲染 PNG → VLM（MiMo）→ JSON 描述（description + text_in_image）
- 拼回：按 (y0, x0) 排序还原阅读顺序
- 清洗：跨页高频重复行（页眉页脚/页码）剔除；行尾非句末标点的强制断行合并
"""
from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path
from typing import List, Optional

import pymupdf

from .base import DocumentNode, ParseError, Parser
from .vlm import VLMClient

logger = logging.getLogger("rag.pdf")

DEFAULT_PAGE_TEXT_THRESHOLD = 20  # 页均字符低于此 → 扫描型
DEFAULT_MAX_PAGES = 500

IMAGE_PROMPT = """你是文档解析器。图片是文档中的配图（架构图/图表/示意图等）。请输出：
1. description：用 2-4 句话描述图片表达的内容与结论（含图中关键数字/标签）
2. text_in_image：图中出现的所有文字（标签、坐标、标题），按出现顺序用换行分隔；没有则为空串
输出 JSON：{"description": "...", "text_in_image": "..."}"""

SCAN_PROMPT = """你是文档转录器。图片是扫描文档的一页。请按阅读顺序输出页面上全部文字，
保留表格结构（用 Markdown 表格）。只输出内容本身，不要解释，不要输出 JSON。"""

# 句末标点：行尾命中则不合并下行
_SENT_END = set("。！？；：”’）】》")


def _default_vlm() -> VLMClient:
    """默认客户端：从项目配置读取（import config 会加载 .env）。"""
    from config import LLM_MAX_RETRIES, LLM_TIMEOUT, MIMO_API_KEY, MIMO_BASE_URL, MIMO_MODEL, PARSED_DIR
    return VLMClient(api_key=MIMO_API_KEY, base_url=MIMO_BASE_URL, model=MIMO_MODEL,
                     timeout=LLM_TIMEOUT, max_retries=LLM_MAX_RETRIES,
                     cache_dir=PARSED_DIR / "vlm_cache")


def _is_sentence_end(line: str) -> bool:
    return bool(line) and line[-1] in _SENT_END


def remove_headers_footers(nodes: List[DocumentNode], page_count: int,
                           page_h: Optional[float] = None) -> List[DocumentNode]:
    """跨页高频重复行 → 剔除页眉/页脚/页码。

    防误删策略（验收脚本抓出的问题）：
    - 只处理单行节点（页眉/页脚/页码几乎都是独立短行；正文段落多为多行，保守保留）
    - 有 bbox 时要求行位于页面顶部/底部区域（顶部 12% / 底部 12%），避免误删页面中部重复正文
    - 无 bbox（docx 页眉段落等）时整节点即重复行则剔除
    - page_h 由调用方传入真实页高；缺失时从 bbox 推断（内容不满页时推断值偏小，仅作退化路径）
    """
    if not nodes:
        return nodes
    if page_h is None:
        page_h = max((n.meta["bbox"][3] for n in nodes if n.meta.get("bbox")), default=0.0)
    if page_h > 0:
        # 顶部/底部 7% 区域（A4 ≈ 前 59pt）：页眉/页码都在此，正文第一行（y≥61）不受误伤
        top_limit = page_h * 0.07
        bottom_limit = page_h * 0.93
    else:
        top_limit = bottom_limit = None  # 无坐标信息 → 跳过区域判断（docx 页眉等无 bbox 场景）

    counter: Counter = Counter()
    for n in nodes:
        counter.update(ln.strip() for ln in n.text.split("\n") if ln.strip())
    threshold = max(2, page_count * 0.5)
    noisy = {ln for ln, c in counter.items() if c >= threshold}
    if not noisy:
        return nodes

    out: List[DocumentNode] = []
    for n in nodes:
        ls = [ln.strip() for ln in n.text.split("\n") if ln.strip()]
        if len(ls) != 1 or ls[0] not in noisy:
            out.append(n)
            continue
        bbox = n.meta.get("bbox")
        if bbox and top_limit is not None:
            y0, y1 = bbox[1], bbox[3]
            if not (y0 < top_limit or y1 > bottom_limit):
                out.append(n)  # 重复行但不在页眉页脚区域 → 每页相同的正文，保留
                continue
        # 命中页眉/页脚/页码：剔除该节点
    return out


def _join_space(a: str, b: str) -> str:
    """拼接补位：ASCII 字母衔接处加空格（英文断行），中文直接拼接。"""
    if a and b and a[-1].isascii() and a[-1].isalpha() and b[0].isascii() and b[0].isalpha():
        return " " + b
    return b


def merge_broken_lines(nodes: List[DocumentNode]) -> List[DocumentNode]:
    """PDF 强制换行合并（仅 paragraph 节点）：

    1. 节点内：行尾非句末标点且非空 → 与下行拼接
    2. 跨节点：与后续同页 paragraph 拼接（PyMuPDF 部分文档每行一个 block）
    """
    out: List[DocumentNode] = []
    i = 0
    while i < len(nodes):
        n = nodes[i]
        if n.type != "paragraph":
            out.append(n)
            i += 1
            continue
        changed = False
        lines = n.text.split("\n")
        if len(lines) > 1:
            merged: List[str] = []
            for line in lines:
                s = line.strip()
                if merged and s and not _is_sentence_end(merged[-1]):
                    merged[-1] += _join_space(merged[-1], s)
                    changed = True
                else:
                    merged.append(s)
            n.text = "\n".join(merged)
        while i + 1 < len(nodes):
            nxt = nodes[i + 1]
            if nxt.type != "paragraph" or nxt.meta.get("page") != n.meta.get("page"):
                break
            cur = n.text.rstrip()
            if not cur or _is_sentence_end(cur):
                break
            nxt_text = nxt.text.strip()
            if not nxt_text:
                break
            if nxt.text.startswith((" ", "\t", "　")):
                break  # 首行缩进（两空格/全角空格）→ 新段落，不合并
            n.text = cur + _join_space(cur, nxt_text)
            changed = True
            i += 1
        if changed:
            flags = list(n.meta.get("cleaned_flags") or [])
            flags.append("joinline")
            n.meta["cleaned_flags"] = flags
        out.append(n)
        i += 1
    return out


class PdfParser(Parser):
    """文本型 PDF 解析：块分离 + 图片 VLM + 坐标拼回。"""

    def __init__(self, vlm: Optional[VLMClient] = None,
                 page_text_threshold: int = DEFAULT_PAGE_TEXT_THRESHOLD,
                 max_pages: int = DEFAULT_MAX_PAGES) -> None:
        self.vlm = vlm
        self.page_text_threshold = page_text_threshold
        self.max_pages = max_pages

    def parse(self, path: Path) -> List[DocumentNode]:
        path = Path(path)
        doc = pymupdf.open(path)
        try:
            if doc.page_count > self.max_pages:
                raise ParseError(f"{path.name}: 页数超限（{doc.page_count} > {self.max_pages}）")
            total_chars = 0
            total_images = 0
            for page in doc:
                total_chars += len(page.get_text())
                total_images += sum(1 for b in page.get_text("dict")["blocks"] if b["type"] == 1)
            if total_chars == 0 and total_images == 0:
                return []  # 空文档（无文字无图片）：无内容可解析，R5 产物校验会拒绝空产物
            avg_chars = total_chars // max(1, doc.page_count)
            # 扫描型：页均字符低于阈值 且（页上有图片块 或 完全无文字层）——纯文本短页不误判
            if avg_chars < self.page_text_threshold and (total_images > 0 or total_chars == 0):
                if self.vlm is None:
                    raise ParseError(f"{path.name}: 扫描型 PDF（页均字符 {avg_chars}），未配置 VLM 无法转录")
                return self._parse_scanned(doc, path.name)  # R4：VLM 整页转录
            nodes: List[DocumentNode] = []
            for pno, page in enumerate(doc, 1):
                nodes.extend(self._parse_page(page, pno, path.name))
            nodes = remove_headers_footers(nodes, doc.page_count, doc[0].rect.height)
            return merge_broken_lines(nodes)
        finally:
            doc.close()

    # ---- 扫描型：整页渲染 → VLM 转录（每页一个节点，失败占位不中断） ----
    def _parse_scanned(self, doc: pymupdf.Document, source: str) -> List[DocumentNode]:
        if self.vlm is None:
            self.vlm = _default_vlm()
        nodes: List[DocumentNode] = []
        for pno, page in enumerate(doc, 1):
            try:
                pix = page.get_pixmap(dpi=150)
                text = self.vlm.chat_text(SCAN_PROMPT, pix.tobytes("png"), f"scan_p{pno}").strip()
                if not text:
                    text = "[该页转录为空]"
            except Exception as e:
                logger.warning("scanned page transcribe failed: %s p%d: %s", source, pno, e)
                text = "[该页解析失败]"
            nodes.append(DocumentNode("paragraph", text,
                                      {"source": source, "page": pno, "index": len(nodes)}))
        return nodes

    # ---- 单页：块提取 + 排序拼回 ----
    def _parse_page(self, page: pymupdf.Page, pno: int, source: str) -> List[DocumentNode]:
        items: List[tuple] = []
        index = 0
        for b in page.get_text("dict")["blocks"]:
            bbox = tuple(round(x, 1) for x in b["bbox"])
            if b["type"] == 0:
                text = self._block_text(b)
                if text.strip():
                    items.append((bbox, DocumentNode("paragraph", text.strip(),
                                                      {"source": source, "page": pno,
                                                       "bbox": bbox, "index": index})))
                    index += 1
            elif b["type"] == 1:
                desc = self._describe_image(page, bbox, source, pno)
                items.append((bbox, DocumentNode("image", desc,
                                                 {"source": source, "page": pno,
                                                  "bbox": bbox, "index": index})))
                index += 1
        # 按 (y0, x0) 排序还原阅读顺序
        items.sort(key=lambda t: (t[0][1], t[0][0]))
        return [n for _, n in items]

    @staticmethod
    def _block_text(block: dict) -> str:
        lines = []
        for line in block.get("lines", []):
            lines.append("".join(s["text"] for s in line.get("spans", [])))
        return "\n".join(lines)

    # ---- 图片块 → VLM ----
    def _describe_image(self, page: pymupdf.Page, bbox: tuple, source: str, pno: int) -> str:
        if self.vlm is None:
            self.vlm = _default_vlm()
        try:
            # 2x 渲染（144dpi）：72dpi 对小图块的文字识别质量不够
            pix = page.get_pixmap(clip=pymupdf.Rect(bbox), matrix=pymupdf.Matrix(2, 2))
            png = pix.tobytes("png")
            obj = self.vlm.chat_image(IMAGE_PROMPT, png, "img")
            desc = (obj.get("description") or "").strip()
            text_in = (obj.get("text_in_image") or "").strip()
            parts = [desc]
            if text_in:
                parts.append(text_in)
            result = "；".join(parts)
            return result if result.strip("；") else "[图片无法解析]"
        except Exception as e:
            logger.warning("image block vlm failed: %s p%d: %s", source, pno, e)
            return "[图片解析失败]"
