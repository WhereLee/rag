"""
全局配置：12-factor，从项目根 .env 加载。
"""
import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]      # rag-python/
RAG_ROOT = PROJECT_ROOT.parent                           # rag/
DATA_DIR = RAG_ROOT / "data"
CORPUS_DIR = DATA_DIR / "corpus"
PARSED_DIR = DATA_DIR / "parsed"
MODELS_DIR = PROJECT_ROOT / "models"

load_dotenv(RAG_ROOT / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


# --- MiMo VLM（视觉/图片转录专用；问答 LLM 见下方 LLM_*） ---
MIMO_API_KEY = os.getenv("MIMO_API_KEY", "")
MIMO_BASE_URL = os.getenv("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1")
MIMO_MODEL = os.getenv("MIMO_MODEL", "mimo-v2.5")
LLM_TIMEOUT = _int("LLM_TIMEOUT", 120)          # 单次调用超时（秒）
LLM_MAX_RETRIES = _int("LLM_MAX_RETRIES", 2)    # 失败重试次数

# --- 问答 LLM（与 VLM 分离：DeepSeek 等 OpenAI 兼容 API；未配置时回退 MiMo，兼容旧 .env） ---
LLM_API_KEY = os.getenv("LLM_API_KEY", "") or MIMO_API_KEY
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "") or MIMO_BASE_URL
LLM_MODEL = os.getenv("LLM_MODEL", "") or MIMO_MODEL
# MiMo 特有参数 enable_thinking 开关：DeepSeek 等不认该字段，置 0 不发（模型自选）
LLM_ENABLE_THINKING = os.getenv("LLM_ENABLE_THINKING", "1") == "1"

# --- 数据层 ---
# 数据库连接串：无默认值（防部署时静默使用默认密码/默认库）；缺失时启动 fail-fast
PG_DSN = os.getenv("PG_DSN", "")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
PG_POOL_MIN = _int("PG_POOL_MIN", 2)      # 2026-08-23：5→2（业务并发低，与 Java Hikari 5 合计 15 连接）
PG_POOL_MAX = _int("PG_POOL_MAX", 10)     # 2026-08-23：20→10（16 并发压测会等待但不失败，业务并发≤4 无感）

# --- 本地推理 ---
EMBED_DIM = _int("EMBED_DIM", 768)
EMBED_MODEL_DIR = os.getenv("EMBED_MODEL_DIR", "bge-base-zh-v1.5-onnx-int8")
# rerank 默认用 base（黄金集对比实验：候选 50→20 + base 质量零损失、延迟 -83%、内存 -277MB）；
# 语料扩展后回归退化可经 env 切回 v2-m3（RERANK_TOP_N 保持 20，无需回 50）
RERANK_MODEL_DIR = os.getenv("RERANK_MODEL_DIR", "bge-reranker-base-onnx-int8")
EMBED_BATCH_SIZE = _int("EMBED_BATCH_SIZE", 32)
ONNX_THREADS = _int("ONNX_THREADS", 8)          # P 核绑定（i5-12500H）
# 推理线程数拆分：embedding（短任务毫秒级）与 rerank（秒级 CPU 密集）分开配置，
# 默认跟随 ONNX_THREADS（行为不变）；4 核服务器经 .env 覆盖（EMBED_THREADS=4 RERANK_THREADS=2）
# 2026-08-23 实测（服务器 4 核，级联 on）：RERANK_THREADS=2 优于 4——4 并发时
# p50 19885→9545ms（-52%）、降级率 37.5%→15.4%、QPS +72%；8 并发时两者差异在噪声内
# （级联跳过率随机性影响，A/B 差距 ~8%）。结论：2 线程×信号量 2 = 4 核恰饱和，避免超订
EMBED_THREADS = _int("EMBED_THREADS", ONNX_THREADS)
RERANK_THREADS = _int("RERANK_THREADS", ONNX_THREADS)
# 2026-08-23 内存实验开关：空=onnxruntime 默认 arena（预分配大块，内存高）；
# kSameAsRequested=arena 按需扩展（内存低，性能待实测，判定标准：省≥400MB 且 p50 恶化≤10%）
RERANK_ARENA_STRATEGY = os.getenv("RERANK_ARENA_STRATEGY", "")
# rerank 并发推理实例数（信号量）：4 核 = 2 线程 × 2 实例恰饱和；
# 公式：推理线程数 × 并发实例 ≈ 物理核数（CPU 共享池，CFS 调度，非独占核）
RERANK_CONCURRENCY = _int("RERANK_CONCURRENCY", 2)
# 级联 rerank 共识窗口：同一块同时在向量 top3 与 BM25 top3 → 高置信免精排
CASCADE_TOP_K = _int("CASCADE_TOP_K", 3)

# 推理前绑定线程数（必须在 import onnxruntime 前生效）
os.environ.setdefault("OMP_NUM_THREADS", str(ONNX_THREADS))
os.environ.setdefault("MKL_NUM_THREADS", str(ONNX_THREADS))

# --- 检索参数 ---
VECTOR_TOP_K = _int("VECTOR_TOP_K", 24)
# E1 实验：向量列切换（embedding=768 维默认 / embedding2=1792 维 ritrieve）
VECTOR_COLUMN = os.getenv("VECTOR_COLUMN", "embedding")
if VECTOR_COLUMN not in ("embedding", "embedding2"):
    raise ValueError(f"非法 VECTOR_COLUMN: {VECTOR_COLUMN}")
BM25_TOP_K = _int("BM25_TOP_K", 24)
FINAL_TOP_K = _int("FINAL_TOP_K", 8)
RRF_K = _int("RRF_K", 60)
RERANK_TIMEOUT = _int("RERANK_TIMEOUT", 120)    # 秒，排队硬超时（超时抛 RerankBusyError 报错，不降级；2026-08-23 定夺）
RERANK_REJECT = float(os.getenv("RERANK_REJECT", "-5.0"))   # 低于此分 → 空结果
RERANK_LOW = float(os.getenv("RERANK_LOW", "0.0"))          # 低于此分 → 低置信

# --- 服务 ---
API_PORT = _int("API_PORT", 8090)
SERVICE_NAME = "rag-doc-qa"

# --- 安全 ---
# 内部 API Key：Python 服务与 Java 网关之间的鉴权密钥
# 生产环境必须设置，未设置时仅允许 localhost 访问
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "")

# 实验模式：EXPERIMENT_MODE=1 才允许通过 HTTP 修改实验开关（E3/E4）。
# 实验脚本（run_experiments.py）进程内直调不受此限制。
EXPERIMENT_MODE = os.getenv("EXPERIMENT_MODE", "") == "1"

for _d in (CORPUS_DIR, PARSED_DIR, MODELS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ===== 启动安全校验 =====
import logging as _logging
_logger = _logging.getLogger("rag.security")

def _check_security_config():
    """检测敏感配置是否使用默认值，生产环境拒绝启动。"""
    warnings = []
    if not PG_DSN:
        # 与 Java 网关（SPRING_DATASOURCE_PASSWORD 无默认值 + fail-fast）对齐：
        # 数据库连接串不应有代码内默认值，缺失说明部署配置遗漏，直接拒绝启动
        raise RuntimeError("PG_DSN 未设置（无默认值，防静默使用默认密码/默认库），请检查 .env")
    if not INTERNAL_API_KEY:
        warnings.append("INTERNAL_API_KEY 未设置，仅允许 localhost 访问")
    if "root" in PG_DSN and "localhost" in PG_DSN:
        warnings.append("PG_DSN 使用默认密码 'root'（本地开发库；生产必须更换）")
    if not MIMO_API_KEY:
        warnings.append("MIMO_API_KEY 未设置，LLM 功能不可用")

    is_prod = os.getenv("SPRING_PROFILES_ACTIVE", "") == "prod"
    if warnings:
        msg = "\n========== 安全配置警告 ==========\n" + \
              "\n".join(f"  [!] {w}" for w in warnings) + \
              "\n  生产部署前请设置对应环境变量！" + \
              "\n=================================="
        if is_prod:
            raise RuntimeError(f"生产环境检测到不安全配置: {warnings}")
        _logger.warning(msg)
    else:
        _logger.info("安全配置检查通过")

_check_security_config()
