@echo off
rem 一键启动图形界面（不带控制台窗口）
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" -m mcsl
) else (
    echo [!] 没找到 .venv，请先执行:
    echo     python -m venv .venv
    echo     .venv\Scripts\python -m pip install -r requirements.txt
    pause
)
endlocal
