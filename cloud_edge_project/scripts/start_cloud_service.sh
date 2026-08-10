#!/bin/bash
set -e

source /root/miniconda3/bin/activate cloud_llm
export CLOUD_BACKEND="vllm"
export CLOUD_SERVICE_PORT="6008"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}"

exec python -m uvicorn cloud_service.app:app \
    --host 0.0.0.0 \
    --port 6008
