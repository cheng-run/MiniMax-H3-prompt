@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [start] .venv\Scripts\python.exe not found. Create the venv first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" scripts\start_web.py %*
if errorlevel 1 pause
endlocal
