#!/bin/bash
set -e

VLLM_CONDA_ENV="${VLLM_CONDA_ENV:-cloud_llm}"
eval "$(conda shell.bash hook)"
conda activate "${VLLM_CONDA_ENV}"

MODEL_PATH="${VLLM_MODEL_PATH:-${MODEL_PATH:-models/Qwen3-14B-AWQ}}"

exec vllm serve "${MODEL_PATH}" \
    --host "${VLLM_HOST:-127.0.0.1}" \
    --port "${VLLM_PORT:-6006}" \
    --served-model-name "${VLLM_MODEL_NAME:-qwen-cloud}" \
    --max-model-len "${VLLM_MAX_MODEL_LEN:-4096}" \
    --gpu-memory-utilization "${VLLM_GPU_MEMORY_UTILIZATION:-0.9}"
