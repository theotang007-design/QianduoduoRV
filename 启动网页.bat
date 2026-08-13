@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ================================
echo   钱多多智能复盘 - 启动Web服务
echo ================================
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" web_app.py
) else (
    python web_app.py
)
pause