"""测试全局配置。

- CI/干净环境无 .env：在 config 导入前注入最小假配置（测试为纯单测，不真连 PG/Redis；
  setdefault 保证本地/服务器已有 .env 时不被覆盖）
- 模型文件缺失时跳过真实推理测试（CI 不携带大模型；本地/服务器带模型环境照常全量跑）
"""
import os
from pathlib import Path

os.environ.setdefault("PG_DSN", "postgresql://rag_app:ci_fake@127.0.0.1:5432/rag_kb")
os.environ.setdefault("INTERNAL_API_KEY", "ci-fake-key")

import pytest  # noqa: E402

MODEL_DIR = Path(__file__).resolve().parents[1] / "models"
MODEL_AVAILABLE = (MODEL_DIR / "bge-base-zh-v1.5-onnx-int8" / "model.onnx").exists()

requires_model = pytest.mark.skipif(
    not MODEL_AVAILABLE,
    reason="模型文件缺失（CI 不携带大模型），跳过真实推理测试",
)
