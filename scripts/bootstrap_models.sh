#!/usr/bin/env bash
# ============================================================
# RAG 项目 - 模型下载脚本（云服务器首次部署使用）
# 用法：chmod +x scripts/bootstrap_models.sh && ./scripts/bootstrap_models.sh
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MODELS_DIR="${SCRIPT_DIR}/../rag-python/models"

echo "=== RAG 模型下载 ==="
echo "目标目录: ${MODELS_DIR}"
echo ""

# 检查 huggingface-cli 或 wget
if command -v huggingface-cli &> /dev/null; then
    DL_TOOL="huggingface-cli"
elif command -v wget &> /dev/null; then
    DL_TOOL="wget"
else
    echo "[ERROR] 需要 huggingface-cli 或 wget，请先安装："
    echo "  pip install huggingface_hub[cli]"
    echo "  或 apt install wget"
    exit 1
fi

download_model() {
    local repo="$1"
    local local_name="$2"
    local target="${MODELS_DIR}/${local_name}"

    if [ -d "$target" ] && [ -f "${target}/model.onnx" ]; then
        echo "[SKIP] ${local_name} 已存在"
        return 0
    fi

    echo "[DOWNLOAD] ${repo} -> ${local_name}"
    mkdir -p "$target"

    if [ "$DL_TOOL" = "huggingface-cli" ]; then
        huggingface-cli download "$repo" --local-dir "$target"
    else
        # wget fallback: 逐个下载必要文件
        local base_url="https://huggingface.co/${repo}/resolve/main"
        for file in config.json model.onnx tokenizer.json tokenizer_config.json special_tokens_map.json; do
            if [ ! -f "${target}/${file}" ]; then
                echo "  downloading ${file}..."
                wget -q -O "${target}/${file}" "${base_url}/${file}" || {
                    echo "  [WARN] ${file} 下载失败，跳过"
                    rm -f "${target}/${file}"
                }
            fi
        done
        # 部分模型有 vocab.txt 或 sentencepiece.bpe.model
        for file in vocab.txt sentencepiece.bpe.model; do
            wget -q -O "${target}/${file}" "${base_url}/${file}" 2>/dev/null || rm -f "${target}/${file}"
        done
    fi

    if [ -f "${target}/model.onnx" ]; then
        local size=$(du -sh "${target}/model.onnx" | cut -f1)
        echo "[OK] ${local_name} (${size})"
    else
        echo "[ERROR] ${local_name} 下载不完整，model.onnx 缺失"
        return 1
    fi
}

# ---- 模型列表 ----

echo ""
echo "--- [1/2] Embedding: bge-base-zh-v1.5 (ONNX INT8, 768维, ~98MB) ---"
download_model "BAAI/bge-base-zh-v1.5-onnx-int8" "bge-base-zh-v1.5-onnx-int8"

echo ""
echo "--- [2/2] Reranker: bge-reranker-base (ONNX INT8, ~100MB) ---"
# 注意：BAAI 官方可能没有 ONNX INT8 版本，需要自行转换
# 方案 A：直接下载 ONNX 版本（如果存在）
# 方案 B：下载 PyTorch 版本后用 optimum-cli 转换
#   pip install optimum[onnxruntime]
#   optimum-cli export onnx --model BAAI/bge-reranker-base --task text-classification \
#       --quantize avx2 bge-reranker-base-onnx-int8/
download_model "BAAI/bge-reranker-base" "bge-reranker-base-onnx-int8"

echo ""
echo "=== 下载完成 ==="
echo ""
echo "模型目录结构:"
ls -la "${MODELS_DIR}/"
echo ""
echo "磁盘占用:"
du -sh "${MODELS_DIR}"/*
