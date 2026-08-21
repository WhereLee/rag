"""
PDF 解析（第二轮重构）：页级预判 → 块化 → 块级 VLM 分派 → 旧结构兼容输出。

流程（方案文档 §3 + 审查修订 R3）：
1. 页级预判：chars<50 → 整页转录 C；无内容 → skipped
2. 有文本页：块化（text/table/image 块）→ 块级分派
   - 文本块 直达（零成本）   - 表格块 裁剪 bbox → VLM 结构化   - 图片块 裁剪 bbox → VLM 语义描述
3. 块级容错：坏块 failed 记录（render/validate/vlm/unfixable），同页好块照常 —— 坏块不拖死整篇
4. 旧 page dict 兼容输出（同步路径 ingest_file 零改动）= to_legacy_page()

并行（Step 2）将替换"块级分派"为线程池执行（worker 内）；本文件保持纯串行正确性优先。
"""
from __future__ import annotations

import logging
from pathlib import Path

import pymupdf

from ingest import vlm
from ingest.block_types import PageResult, Block, BlockType, to_legacy_page
from ingest.page_analyzer import extract_page_blocks
from llm.mimo_client import LLMError

logger = logging.getLogger("rag.pdf_parser")

RENDER_DPI = 200
TABLE_PAD = 2           # 表格裁剪外扩像素（防切边）


def _render_png(page: pymupdf.Page, clip: pymupdf.Rect | None = None,
                dpi: int = RENDER_DPI) -> bytes:
    pix = page.get_pixmap(dpi=dpi, clip=clip)
    return pix.tobytes("png")


def _crop_bbox(page: pymupdf.Page, bbox) -> pymupdf.Rect:
    """按 bbox 坐标裁剪（含外扩与页面边界收敛）。"""
    rect = bbox if isinstance(bbox, pymupdf.Rect) else pymupdf.Rect(*bbox)
    rect = rect + (-TABLE_PAD, -TABLE_PAD, TABLE_PAD, TABLE_PAD)
    return rect & page.rect


def _process_block(page: pymupdf.Page, block: Block, stats: dict) -> None:
    """块级分派（单页内串行，多页并行）。块失败记录到 block.status/error 与 stats。"""
    bbox = _crop_bbox(page, block.bbox)
    try:
        if block.type == BlockType.TEXT:
            return  # 文本块内容已在块化时取得，无需处理
        if block.type == BlockType.TABLE:
            png = _render_png(page, clip=bbox)
            try:
                md, meta = vlm.parse_table_region(png)
            except vlm.VLMValidationError as ve:
                _fail_block(block, ve, stats, "table", "validate")
                return
            stats["vlm_calls"] += 1
            stats["vlm_details"].append({"page": block.page_no, "block": block.order,
                                         "type": "table", **meta})
            if md:
                block.text = md
            else:
                _fail_block(block, ValueError("表格区域未检出表格内容（no_table）"), stats,
                            "table", "validate")
            return
        if block.type == BlockType.IMAGE:
            png = _render_png(page, clip=bbox)
            try:
                info, meta = vlm.parse_image(png)
            except vlm.VLMValidationError as ve:
                _fail_block(block, ve, stats, "image", "validate")
                return
            stats["vlm_calls"] += 1
            stats["figures_parsed"] += 1
            stats["vlm_details"].append({"page": block.page_no, "block": block.order,
                                         "type": "image", **meta})
            block.text = info["description"]
            block.meta = {"text_in_image": info["text_in_image"]}
    except LLMError as e:
        _fail_block(block, e, stats, block.type.value, "vlm")
    except pymupdf.mupdf.FzErrorArgument as e:   # 文件缺陷（如 code=4 bandwriter）不重试
        _fail_block(block, e, stats, block.type.value, "unfixable")
    except Exception as e:   # 其余渲染/意外异常兜底
        _fail_block(block, e, stats, block.type.value, "render")

def _fail_block(block: Block, exc: Exception, stats: dict, kind_tag: str, hint: str) -> None:
    """统一失败记录：分类 + 人话文案 + 统计（第三轮：补充块级错误结构化数据供 issue 生成）。"""
    from ingest.errors import classify_exception
    info = classify_exception(exc)
    block.error = info["message"]
    if info["kind"] == "unfixable_file":
        block.status = "unfixable"
        stats["unfixable"] += 1
    elif info["kind"] in ("retriable", "unclassified"):
        block.status = f"failed({hint})"
    else:  # discard_block 等
        block.status = f"failed({hint})"
    stats["blocks_failed"] += 1
    stats["page_errors"].setdefault(block.page_no, []).append(
        f"{kind_tag}[{block.order}]: {info['detail']}")
    # 第三轮：结构化块级失败清单（供 issue_items 落库）
    stats.setdefault("block_failures", []).append({
        "page": block.page_no, "block": block.order, "type": block.type.value,
        "kind": info["kind"], "reason": info["message"], "bbox": list(block.bbox)})
    # 同步记录失败块 bbox 映射（供重试时重渲染原图区域；key 用字符串避免 JSON 序列化报错）
    stats.setdefault("failed_bboxes", {})[f"{block.page_no}:{block.order}"] = list(block.bbox)


def parse_pdf(path: Path, enable_figure_parse: bool = True,
              progress_cb=None) -> tuple[list[dict], dict]:
    """返回 (pages, stats)。签名不变（兼容 sync_service/worker/脚本调用）。

    第二轮 Step2（页级并发）：每页一个 worker（不同 page 对象，PyMuPDF 线程安全），
    并发页数受信号量闸门（同时限制 VLM 并发峰值，页内块串行不叠加），
    结果按 page_no 重排输出。stats 跨线程聚合用锁合并。
    progress_cb(done, total) 按完成页数递增（单调不后退）。

    stats 新增：blocks_total / blocks_failed / pages_failed / unfixable / vlm_details / page_errors
    """
    import os
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading as _threading

    doc = pymupdf.open(str(path))
    total = doc.page_count
    stats_lock = _threading.Lock()
    stats = {"page_count": total, "channels": {"A": 0, "B": 0, "C": 0, "M": 0},
             "vlm_calls": 0, "figures_parsed": 0, "failed_pages": [],
             "blocks_total": 0, "blocks_failed": 0, "unfixable": 0,
             "vlm_details": [], "page_errors": {}}

    def merge_stats(local: dict) -> None:
        with stats_lock:
            for k in ("vlm_calls", "figures_parsed", "blocks_total", "blocks_failed", "unfixable"):
                stats[k] += local.get(k, 0)
            stats["failed_pages"].extend(local.get("failed_pages", []))
            stats["vlm_details"].extend(local.get("vlm_details", []))
            for ch, n in (local.get("channels") or {}).items():
                stats["channels"][ch] = stats["channels"].get(ch, 0) + n
            for pg, err in (local.get("page_errors") or {}).items():
                stats["page_errors"].setdefault(pg, []).extend(err) if isinstance(err, list) else stats["page_errors"].setdefault(pg, err)
            # 第三轮：合并块级失败清单与 bbox 映射
            stats.setdefault("block_failures", []).extend(local.get("block_failures", []))
            stats.setdefault("failed_bboxes", {}).update(local.get("failed_bboxes", {}))

    def process_page(i: int, page) -> tuple[int, dict]:
        """单页处理：块化 + 块级分派（页内串行，多页并行）→ 转旧结构。返回 (page_no, legacy)。"""
        local = {"vlm_calls": 0, "figures_parsed": 0, "blocks_total": 0,
                 "blocks_failed": 0, "unfixable": 0, "failed_pages": [],
                 "vlm_details": [], "page_errors": {}, "channels": {}}
        try:
            page_result: PageResult = extract_page_blocks(page, i, enable_figure_parse)
            if page_result.channel == "C" and len(page_result.blocks) == 1 \
                    and page_result.blocks[0].type == BlockType.IMAGE:
                # 整页转录（通道 C 语义：扫描/纯图页）
                png = _render_png(page)
                try:
                    text, meta = vlm.parse_scanned_page(png)
                except (vlm.VLMValidationError, LLMError) as e:
                    page_result.page_status = "failed"
                    local["failed_pages"].append(i)
                    local["page_errors"][i] = f"scan: {e}"
                    local["vlm_calls"] += 1
                else:
                    local["vlm_calls"] += 1
                    local["vlm_details"].append({"page": i, "type": "scan", **meta})
                    page_result.blocks[0].text = text
            elif page_result.page_status != "skipped":
                ok_count = 0
                for block in page_result.sorted_blocks():
                    _process_block(page, block, local)
                    if block.status == "ok":
                        ok_count += 1
                if ok_count == 0:
                    page_result.page_status = "failed"
                    local["failed_pages"].append(i)
                elif ok_count < len(page_result.blocks):
                    page_result.page_status = "partial"
                else:
                    page_result.page_status = "ok"
            local["blocks_total"] = len(page_result.blocks)
            local["channels"][page_result.channel or "A"] = 1
            merge_stats(local)
            return i, to_legacy_page(page_result)
        except (LLMError, vlm.VLMValidationError, pymupdf.mupdf.FzErrorArgument, ValueError) as e:
            # 整页异常（渲染全炸，如坏页）
            logger.error("page %d parse failed: %s", i, e)
            local["failed_pages"].append(i)
            local["page_errors"][i] = f"page: {e}"
            merge_stats(local)
            return i, {"page_no": i, "channel": "C_failed", "text": "", "tables": [], "images": []}

    results: dict[int, dict] = {}
    sem = _threading.BoundedSemaphore(3)  # 并发页数闸门（同时限 VLM 并发峰值）

    def guarded_process(i, page):
        with sem:
            return process_page(i, page)

    pool_size = min(4, (os.cpu_count() or 2) + 1)
    with ThreadPoolExecutor(max_workers=pool_size) as pool:
        futures = {pool.submit(guarded_process, i, page) for i, page in enumerate(doc)}
        done_count = 0
        for fut in as_completed(futures):
            i, legacy = fut.result()
            results[i] = legacy
            done_count += 1
            if progress_cb:
                progress_cb(done_count, total)

    doc.close()
    pages = [results[i] for i in sorted(results) if results[i] is not None]
    return pages, stats