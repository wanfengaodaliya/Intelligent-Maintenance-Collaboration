#!/bin/bash
set -e

PROJECT_CONDA_ENV="${PROJECT_CONDA_ENV:-cloud_llm}"
eval "$(conda shell.bash hook)"
conda activate "${PROJECT_CONDA_ENV}"
export CLOUD_BACKEND="${CLOUD_BACKEND:-vllm}"
export CLOUD_SERVICE_PORT="${CLOUD_SERVICE_PORT:-6008}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}"

exec python -m uvicorn cloud_service.app:app \
    --host "${CLOUD_SERVICE_HOST:-0.0.0.0}" \
    --port "${CLOUD_SERVICE_PORT}"
