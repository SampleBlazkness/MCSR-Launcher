@echo off
rem 带控制台窗口启动（看日志/报错用）
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m mcsl %*
) else (
    python -m mcsl %*
)
pause
endlocal
