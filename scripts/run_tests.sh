#!/usr/bin/env bash
# 运行全部测试（标准库 unittest）。
# 若 Flask 装在非标准位置，用 TEST_SITE 指向其 site-packages。
set -euo pipefail
cd "$(dirname "$0")/.."
export TEST_SITE="${TEST_SITE:-}"
exec python3 -I -m unittest discover -s tests -v
