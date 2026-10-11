@echo off
REM Run TradeLab on this Windows PC (needs Python 3.11+, Node.js 20+ and MongoDB running on this PC).
REM Note: trades only fire while this PC is switched on and awake at 09:15 and 14:30.
cd /d %~dp0
if not exist backend\.venv (py -3 -m venv backend\.venv)
call backend\.venv\Scripts\activate.bat
pip install -q -r backend\requirements.txt
if not exist frontend\dist\index.html (
  pushd frontend
  call npm ci
  call npm run build
  popd
)
cd backend
set TRADELAB_DATA_DIR=..\data
REM A local MongoDB. Started as a replica set (mongod --replSet rs0, then rs.initiate() once), use
REM mongodb://127.0.0.1:27017/?replicaSet=rs0 so saves are all-or-nothing.
if "%TRADELAB_MONGO_URL%"=="" set TRADELAB_MONGO_URL=mongodb://127.0.0.1:27017
echo.
echo TradeLab is running at http://localhost:8000
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
