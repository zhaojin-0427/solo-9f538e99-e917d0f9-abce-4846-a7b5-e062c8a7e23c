#!/usr/bin/env bash
# 设备模拟器快捷入口，默认操作 ./data 目录。
# 示例：
#   scripts/demo.sh write 3
#   scripts/demo.sh half
#   scripts/demo.sh rotate
#   scripts/demo.sh illegal nan
#   scripts/demo.sh auto          # 一键完整剧本（需 Web 已在 localhost:8000 运行）
set -euo pipefail
cd "$(dirname "$0")/.."
export DATA_DIR="${DATA_DIR:-$(pwd)/data}"
exec python3 -I demo/device_sim.py --data-dir "$DATA_DIR" "$@"
