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

REM Pass any extra args through, e.g.:  run.bat --poll 45
echo Running... output is also saved to run.log
.venv\Scripts\python.exe -u equeue_probe.py %* > run.log 2>&1

echo.
echo === script exited (see run.log) ===
pause
