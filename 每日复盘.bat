@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ================================
echo   钱多多智能复盘 - 执行每日复盘
echo ================================
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" run_review.py %*
) else (
    python run_review.py %*
)