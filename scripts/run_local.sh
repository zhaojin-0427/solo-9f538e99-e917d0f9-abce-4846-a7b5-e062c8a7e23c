#!/usr/bin/env bash
# 本地启动 Web（无需 Docker）。依赖：python3 + Flask。
# 若 Flask 未安装，可通过 PYTHONPATH 指向自定义 site-packages，或先：
#   python3 -m pip install -r requirements.txt
set -euo pipefail
cd "$(dirname "$0")/.."

export DATA_DIR="${DATA_DIR:-$(pwd)/data}"
export DB_PATH="${DB_PATH:-$(pwd)/db/fermenter.db}"
export ENABLE_DEMO_RESET="${ENABLE_DEMO_RESET:-1}"
export PORT="${PORT:-8000}"

mkdir -p "$(dirname "$DB_PATH")" "$DATA_DIR"
exec python3 run.py
