@echo off
rem ============================================================
rem  MCSR-Launcher - start the GUI (no console window kept open)
rem
rem  ASCII ONLY on purpose: cmd.exe reads a .cmd file using the
rem  system OEM code page (GBK on a Chinese Windows), so UTF-8
rem  Chinese comments get mis-decoded and can swallow the
rem  following line's newline (the script then dies with
rem  "'xxx?setlocal' is not recognized as an internal or
rem  external command"). Keep every .cmd/.bat ASCII.
rem ============================================================
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" goto no_venv

start "" ".venv\Scripts\pythonw.exe" -m mcsl
exit /b 0

:no_venv
echo [!] .venv not found in this folder:
echo     %CD%
echo.
echo Run these two commands first:
echo     python -m venv .venv
echo     .venv\Scripts\python -m pip install -r requirements.txt
echo.
pause
exit /b 1
