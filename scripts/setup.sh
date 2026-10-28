#!/bin/bash
# One-time local setup: Python venv, backend package, Electron dependencies.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
"$PYTHON" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -i "${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}" -e backend

if command -v npm >/dev/null 2>&1; then
  (cd app && npm install --no-audit --no-fund)
  echo "Electron shell dependencies installed."
else
  echo "npm 未安装；后端和浏览器 UI 仍可用。" >&2
fi

echo "完成。运行：.venv/bin/chc-voice serve --port 8791"
