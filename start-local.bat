@echo off
REM Run TradeLab on this Windows PC (needs Python 3.11+ and Node.js 20+).
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
echo.
echo TradeLab is running at http://localhost:8000
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
