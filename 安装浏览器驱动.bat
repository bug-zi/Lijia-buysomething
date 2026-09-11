@echo off
REM ============================================================
REM  ShoppingAgent - Browser Driver Installer
REM  Installs the anti-detection browser driver (patchright)
REM  used for real product scraping.
REM  NOTE: keep this file pure ASCII (no Chinese) to avoid cmd
REM        codepage parsing issues. Chinese is allowed in the
REM        filename only.
REM ============================================================
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if defined PY goto :pyok
where python >nul 2>nul && python -c "import sys" >nul 2>nul && set "PY=python"
if defined PY goto :pyok
where python3 >nul 2>nul && python3 -c "import sys" >nul 2>nul && set "PY=python3"
if defined PY goto :pyok
echo [ERROR] Python not found. Please install Python 3.9+ first:
echo         https://www.python.org/downloads/windows/
pause
exit /b 1

:pyok
echo ============================================================
echo   Shopping Agent - Browser Driver Installer
echo   Python:  %PY%
echo ============================================================

REM ---- step 1: install patchright (playwright fallback is built into the app) ----
%PY% -c "import patchright" >nul 2>nul
if not errorlevel 1 (
    echo [1/3] patchright is already installed. Skip.
    goto :browser
)
echo [1/3] Installing patchright ...
%PY% -m pip install patchright --quiet
if errorlevel 1 (
    echo       Default index failed. Retrying with Tsinghua mirror ...
    %PY% -m pip install patchright --quiet -i https://pypi.tuna.tsinghua.edu.cn/simple
    if errorlevel 1 (
        echo [X] patchright install failed. Check your network, or run manually:
        echo     python -m pip install patchright
        echo     (the app will fall back to playwright if you prefer)
        pause
        exit /b 1
    )
)
echo       patchright installed.

:browser
REM ---- step 2: browser check - system Chrome/Edge preferred, no download ----
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" goto :chrome_ok
if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" goto :chrome_ok
if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" goto :chrome_ok
if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" goto :chrome_ok
if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" goto :chrome_ok
echo [2/3] No system Chrome/Edge found. Downloading Chromium (~150MB) ...
%PY% -m patchright install chromium
if errorlevel 1 (
    echo [X] Chromium download failed. Retry later manually:
    echo     python -m patchright install chromium
    pause
    exit /b 1
)
goto :verify

:chrome_ok
echo [2/3] System Chrome/Edge found. No browser download needed.

:verify
REM ---- step 3: verify against the app's own check ----
echo [3/3] Verifying ...
%PY% -c "import web_scraper; assert web_scraper._has_playwright()"
if errorlevel 1 (
    echo [X] Verification failed. Please re-run this installer.
    pause
    exit /b 1
)
echo.
echo ============================================================
echo   Done! Real scraping is ready.
echo   Next: run the web launcher bat in this folder, then send a
echo   shopping request. Scan the QR code once when the login page
echo   pops up - the session is kept locally afterwards.
echo ============================================================
pause
exit /b 0
