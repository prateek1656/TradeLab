#!/usr/bin/env bash
# Run TradeLab on this machine (needs Python 3.11+, Node.js 20+ and a running MongoDB).
set -euo pipefail
cd "$(dirname "$0")"
[ -d backend/.venv ] || python3 -m venv backend/.venv
backend/.venv/bin/pip install -q -r backend/requirements.txt
if [ ! -f frontend/dist/index.html ]; then (cd frontend && npm ci && npm run build); fi
cd backend
export TRADELAB_DATA_DIR=../data
# A local MongoDB. Started as a replica set (mongod --replSet rs0, then rs.initiate() once), use
# mongodb://127.0.0.1:27017/?replicaSet=rs0 so saves are all-or-nothing.
export TRADELAB_MONGO_URL="${TRADELAB_MONGO_URL:-mongodb://127.0.0.1:27017}"
echo "TradeLab is running at http://localhost:8000"
exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
