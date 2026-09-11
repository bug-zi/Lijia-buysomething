@echo off
REM ============================================================
REM  ShoppingAgent - Web UI launcher (web_server.py, port 8765)
REM  NOTE: keep this file pure ASCII (no Chinese) to avoid cmd
REM        codepage parsing issues. Chinese is allowed in the
REM        filename only.
REM ============================================================
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if defined PY goto :run
where python >nul 2>nul && python -c "import sys" >nul 2>nul && set "PY=python"
if defined PY goto :run
where python3 >nul 2>nul && python3 -c "import sys" >nul 2>nul && set "PY=python3"
if defined PY goto :run
echo [ERROR] Python not found. Please install Python 3.9+ first:
echo         https://www.python.org/downloads/windows/
pause
exit /b 1
:run
echo ============================================================
echo   Shopping Agent Web UI
echo   URL:     http://127.0.0.1:8765/
echo   Python:  %PY%
echo   (close this window to stop the server)
echo ============================================================
%PY% web_server.py
if errorlevel 1 (
    echo.
    echo [ERROR] Server exited with an error. Check log:
    echo         %%TEMP%%\shopping_agent_web_error.log
    pause
)
