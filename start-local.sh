#!/usr/bin/env bash
# Run TradeLab on this machine (needs Python 3.11+ and Node.js 20+).
set -euo pipefail
cd "$(dirname "$0")"
[ -d backend/.venv ] || python3 -m venv backend/.venv
backend/.venv/bin/pip install -q -r backend/requirements.txt
if [ ! -f frontend/dist/index.html ]; then (cd frontend && npm ci && npm run build); fi
cd backend
export TRADELAB_DATA_DIR=../data
echo "TradeLab is running at http://localhost:8000"
exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
