"""解析编排：类型判定（扩展名 + 魔数复核）→ 选择解析器 → 清洗 → 校验 → 状态记录。

- 超时 kill：每文件经子进程执行（subprocess），超时强制终止，防止异常文件拖死主进程
- 状态：success（无问题）/ partial（部分节点降级占位）/ failed（异常或空产物）
- 规模上限：PDF ≤500 页、pptx ≤200 页、xlsx ≤500k 单元格、图片 ≤20MB（各解析器内置 + 这里兜底）
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .clean.cleaner import clean_nodes
from .parser.base import DocumentNode, ParseError
from .quality import validate_nodes

logger = logging.getLogger("rag.pipeline")

MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 独立图片 ≤20MB
DEFAULT_TIMEOUT = 120  # 每文件解析超时（秒）

# 扩展名 → 解析器模块（在子进程内按需 import，保持主进程轻）
PARSER_MAP = {
    "txt": "TxtMdParser", "md": "TxtMdParser",
    "pdf": "PdfParser",
    "docx": "DocxParser", "xlsx": "XlsxParser", "pptx": "PptxParser",
    "png": "ImageParser", "jpg": "ImageParser", "jpeg": "ImageParser", "webp": "ImageParser",
}

# 魔数复核：扩展名与文件头必须一致（txt/md 无魔数，不校验）
_MAGIC = {
    "pdf": (b"%PDF",),
    "docx": (b"PK\x03\x04",), "xlsx": (b"PK\x03\x04",), "pptx": (b"PK\x03\x04",),
    "png": (b"\x89PNG",),
    "jpg": (b"\xff\xd8\xff",), "jpeg": (b"\xff\xd8\xff",),
    "webp": (b"RIFF",),
}


@dataclass
class ParseResult:
    """单文件解析状态记录（可 JSON 化，R6 落库/日志）。"""

    file: str
    status: str  # success | partial | failed
    nodes: List[DocumentNode] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)
    duration: float = 0.0
    error: str = ""
    warnings: List[str] = field(default_factory=list)


# ---------- 类型判定 ----------

def detect_type(path: Path) -> str:
    """扩展名小写（不带点）；未知扩展名抛 ParseError。"""
    ext = path.suffix.lower().lstrip(".")
    if ext not in PARSER_MAP:
        raise ParseError(f"{path.name}: 不支持的文件类型 .{ext}")
    return ext


def check_magic(path: Path, ext: str) -> None:
    """文件头复核：前 8 字节与扩展名魔数一致（防御伪装，与 Java 上传域同思路）。"""
    magic = _MAGIC.get(ext)
    if not magic:
        return  # txt/md 无魔数约束
    with open(path, "rb") as f:
        head = f.read(8)
    if not any(head.startswith(m) for m in magic):
        raise ParseError(f"{path.name}: 文件头与 .{ext} 不符，疑似伪装类型")


def check_size(path: Path, ext: str) -> None:
    """图片体积上限（其他格式由解析器页数/单元格上限约束）。"""
    if ext in ("png", "jpg", "jpeg", "webp"):
        size = path.stat().st_size
        if size > MAX_IMAGE_BYTES:
            raise ParseError(f"{path.name}: 图片过大（{size // 1024 // 1024}MB > 20MB）")


# ---------- 子进程执行（超时 kill） ----------

_CHILD_SCRIPT = r"""
import json, sys, time, importlib
# Windows 管道下 stdout/stderr 默认 GBK，强制 utf-8（主进程按 utf-8 解码）
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
sys.path.insert(0, {src!r})
path, cls_name = sys.argv[1], sys.argv[2]
mod = importlib.import_module("ingest.parser")
parser = getattr(mod, cls_name)()
t0 = time.time()
try:
    # PDF 页级进度经 stderr 回传（主进程 Popen 边跑边读；其他格式无页概念不发）
    if cls_name == "PdfParser":
        def _prog(done, total):
            print("RAG_PROGRESS " + json.dumps({{'done': done, 'total': total}}),
                  file=sys.stderr, flush=True)
        nodes = parser.parse(path, progress_cb=_prog)
    else:
        nodes = parser.parse(path)
    from ingest.clean.cleaner import clean_nodes
    nodes = clean_nodes(nodes)
    from ingest.quality import validate_nodes
    issues = validate_nodes(nodes)
    flags = sorted({{f for n in nodes for f in (n.meta.get("cleaned_flags") or [])}})
    out = {{"nodes": [[n.type, n.text, n.meta] for n in nodes],
            "flags": flags, "issues": issues, "error": ""}}
except Exception as e:
    out = {{"nodes": [], "flags": [], "issues": [], "error": str(e)}}
out["duration"] = round(time.time() - t0, 3)
print(json.dumps(out, ensure_ascii=False))
"""


# ---------- 对外入口 ----------

def parse_file(path: Path, timeout: int = DEFAULT_TIMEOUT,
               progress_cb=None) -> ParseResult:
    """单文件完整管线：判定 → 魔数 → 上限 → 子进程解析+清洗+校验 → 状态。

    progress_cb(stage, progress, detail)：可选三参回调（进度回报，PDF 页级经 stderr 回传）。
    """
    path = Path(path)
    t0 = time.time()
    try:
        ext = detect_type(path)
        check_magic(path, ext)
        check_size(path, ext)
    except ParseError as e:
        return ParseResult(file=path.name, status="failed", error=str(e),
                           duration=round(time.time() - t0, 3))

    src_dir = str(Path(__file__).resolve().parents[1])  # rag-python/src
    cmd = [sys.executable, "-c",
           _CHILD_SCRIPT.format(src=src_dir), str(path), PARSER_MAP[ext]]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as e:
        return ParseResult(file=path.name, status="failed", error=f"子进程启动失败: {e}",
                           duration=round(time.time() - t0, 3))

    # stdout/stderr 均用读线程（防管道满阻塞）：主流程先 wait(timeout) 保证超时 kill 生效，
    # 读线程边跑边解析 stderr 的 RAG_PROGRESS 页级进度行回调 progress_cb
    stdout_lines: list[str] = []

    def _drain_stdout() -> None:
        for line in proc.stdout:
            stdout_lines.append(line)

    def _drain_stderr() -> None:
        for line in proc.stderr:
            line = line.strip()
            if not line.startswith("RAG_PROGRESS "):
                continue
            try:
                d = json.loads(line[len("RAG_PROGRESS "):])
                if progress_cb and d.get("total"):
                    # 页级进度映射到解析阶段区间 5%~45%（与旧同步路径区间约定一致）
                    progress_cb("parsing",
                                round(0.05 + 0.40 * d["done"] / d["total"], 3), d)
            except Exception:
                pass  # 进度行损坏不影响解析结果

    reader = threading.Thread(target=_drain_stdout, daemon=True)
    stderr_reader = threading.Thread(target=_drain_stderr, daemon=True)
    reader.start()
    stderr_reader.start()

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        logger.error("解析超时: %s (>%ds)", path.name, timeout)
        return ParseResult(file=path.name, status="failed", error=f"解析超时（>{timeout}s）",
                           duration=round(time.time() - t0, 3))
    reader.join(timeout=5)
    stderr_reader.join(timeout=5)
    stdout = "".join(stdout_lines)

    try:
        data = json.loads(stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        logger.error("子进程输出异常: %s: %s", path.name, stdout[-300:])
        return ParseResult(file=path.name, status="failed", error="解析子进程输出异常",
                           duration=round(time.time() - t0, 3))

    nodes = [DocumentNode(t, text, meta) for t, text, meta in data["nodes"]]
    issues = data["issues"]
    error = data["error"]
    flags = data["flags"]

    if error:
        status = "failed"
    elif any("空产物" in i or "重复率" in i for i in issues):
        status = "failed"  # 无内容可入库 / 内容无意义 → 拒绝
    elif any("占位" in i for i in issues):
        status = "partial"  # 部分节点降级，可入库但标注
    else:
        status = "success"  # 过短等 warning 级问题不拒绝
    fatal = [i for i in issues if "空产物" in i or "重复率" in i]
    if status == "failed" and not error:
        error = "；".join(fatal)
    return ParseResult(file=path.name, status=status, nodes=nodes, flags=flags,
                       error=error, warnings=issues,
                       duration=data["duration"])
