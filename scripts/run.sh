#!/bin/bash
# Start the local UI (browser) on a port. Override with PORT=... scripts/run.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${PORT:-8791}"
exec .venv/bin/chc-voice serve --port "$PORT" --open
