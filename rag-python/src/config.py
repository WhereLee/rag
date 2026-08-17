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


# --- MiMo LLM ---
MIMO_API_KEY = os.getenv("MIMO_API_KEY", "")
MIMO_BASE_URL = os.getenv("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1")
MIMO_MODEL = os.getenv("MIMO_MODEL", "mimo-v2.5")
LLM_TIMEOUT = _int("LLM_TIMEOUT", 120)          # 单次调用超时（秒）
LLM_MAX_RETRIES = _int("LLM_MAX_RETRIES", 2)    # 失败重试次数

# --- 数据层 ---
PG_DSN = os.getenv("PG_DSN", "postgresql://postgres:root@localhost:5432/rag_kb")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
PG_POOL_MIN = _int("PG_POOL_MIN", 2)                 # cloud 缩减（master=5）
PG_POOL_MAX = _int("PG_POOL_MAX", 10)                # cloud 缩减（master=20）

# --- 本地推理 ---
EMBED_DIM = _int("EMBED_DIM", 768)
EMBED_MODEL_DIR = os.getenv("EMBED_MODEL_DIR", "bge-base-zh-v1.5-onnx-int8")
# cloud-deploy: 使用 bge-reranker-base（~100MB，纯中文场景够用）
# master 分支使用 bge-reranker-v2-m3（543MB，多语言最强）
RERANK_MODEL_DIR = os.getenv("RERANK_MODEL_DIR", "bge-reranker-base-onnx-int8")
EMBED_BATCH_SIZE = _int("EMBED_BATCH_SIZE", 16)       # cloud 4G 内存缩减（master=32）
ONNX_THREADS = _int("ONNX_THREADS", 2)               # cloud 4核（master=8 for i5-12500H P-cores）

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
RERANK_TIMEOUT = _int("RERANK_TIMEOUT", 15)     # 秒，超时降级 RRF
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
    if not INTERNAL_API_KEY:
        warnings.append("INTERNAL_API_KEY 未设置，仅允许 localhost 访问")
    if "root" in PG_DSN and "localhost" in PG_DSN:
        warnings.append("PG_DSN 使用默认密码 'root'")
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
