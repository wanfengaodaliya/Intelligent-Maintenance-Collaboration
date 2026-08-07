#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
PYTHON_BIN=${PYTHON_BIN:-python}

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "Unable to start network controller: Python executable not found" >&2
    exit 1
fi

export NETWORK_CONFIG_DIR="${NETWORK_CONFIG_DIR:-$PROJECT_ROOT/config}"
export NETWORK_LOG_DIR="${NETWORK_LOG_DIR:-$PROJECT_ROOT/logs}"

cd "$PROJECT_ROOT"
exec "$PYTHON_BIN" -m controller.main
