@echo off
rem ============================================================
rem  MCSR-Launcher - start WITH a console window (for debugging:
rem  tracebacks and log output stay visible here).
rem  ASCII only - see the comment in run.cmd for why.
rem ============================================================
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto have_venv
echo [!] .venv not found; falling back to the python on PATH.
python -m mcsl %*
goto done

:have_venv
".venv\Scripts\python.exe" -m mcsl %*

:done
echo.
echo [MCSR-Launcher] exited. Press any key to close this window.
pause >nul
endlocal
