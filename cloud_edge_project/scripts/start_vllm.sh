#!/bin/bash
set -e

source /root/miniconda3/bin/activate cloud_llm

MODEL_PATH="${MODEL_PATH:-/root/autodl-tmp/models/Qwen3-14B-AWQ}"

exec vllm serve "${MODEL_PATH}" \
    --host 127.0.0.1 \
    --port 6006 \
    --served-model-name qwen-cloud \
    --max-model-len 4096 \
    --gpu-memory-utilization 0.9
