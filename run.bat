@echo off
setlocal
cd /d %~dp0

if not exist .venv\Scripts\python.exe (
    echo .venv not found. Setup first:
    echo   py -3.11 -m venv .venv
    echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo   .venv\Scripts\python.exe -m playwright install chromium
    pause
    exit /b 1
)

REM Pass any extra args through, e.g.:  run.bat --services 4,7 --poll 45
.venv\Scripts\python.exe equeue_probe.py %*

echo.
echo === script exited ===
pause
