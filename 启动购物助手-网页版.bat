@echo off
REM ============================================================
REM  全自动个人购物AI助手 — 网页版 一键启动脚本
REM  功能：
REM    1) 自动探测 Python 解释器
REM    2) 启动 web_server.py（默认端口 8765）
REM    3) 自动打开默认浏览器访问 http://127.0.0.1:8765/
REM  出错后会自动打印错误并 pause，不再闪退
REM ============================================================

setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
set "LOG_FILE=%TEMP%\shopping_agent_web_error.log"
del /f /q "%LOG_FILE%" 2>nul
call :log "========== 网页版 启动日志 %DATE% %TIME% =========="
call :log "工作目录: %cd%"

REM ---- 探测 Python（4 层兜底）----
set "PY_CMD="
set "PY_EXE="

where /q py.exe 2>nul
if %ERRORLEVEL%==0 (
    for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>"%LOG_FILE%"') do set "PY_EXE=%%i"
    if defined PY_EXE ( set "PY_CMD=py -3" & goto :py_found )
)

where /q python.exe 2>nul
if %ERRORLEVEL%==0 (
    for /f "delims=" %%i in ('python -c "import sys;print(sys.executable)" 2^>>"%LOG_FILE%"') do set "PY_EXE=%%i"
    if defined PY_EXE ( set "PY_CMD=python" & goto :py_found )
)

where /q python3.exe 2>nul
if %ERRORLEVEL%==0 (
    for /f "delims=" %%i in ('python3 -c "import sys;print(sys.executable)" 2^>>"%LOG_FILE%"') do set "PY_EXE=%%i"
    if defined PY_EXE ( set "PY_CMD=python3" & goto :py_found )
)

for %%P in (
    "D:\Python313\python.exe"
    "D:\Python312\python.exe"
    "D:\Python311\python.exe"
    "D:\Python310\python.exe"
    "D:\Python39\python.exe"
    "C:\Python313\python.exe"
    "C:\Python312\python.exe"
    "C:\Python311\python.exe"
    "C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python313\python.exe"
    "C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python312\python.exe"
    "C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python311\python.exe"
) do (
    if exist %%~P (
        set "PY_CMD=%%~P"
        set "PY_EXE=%%~P"
        goto :py_found
    )
)

REM ===== 未找到 Python =====
echo.
echo [启动失败] 未检测到 Python 3.9+
echo   请安装 Python： https://www.python.org/downloads/windows/
echo   安装时务必勾选 [v] Add Python to PATH
echo   （推荐安装到 D 盘，如 D:\Python311\）
echo.
echo 详细日志见："%LOG_FILE%"
pause
exit /b 1

:py_found
call :log "Python: %PY_CMD%"
call :log "路径: %PY_EXE%"

REM ---- 版本检查 ----
set /a MAJ=0 & set /a MIN=0
for /f "tokens=1,2 delims=." %%a in ('%PY_CMD% -c "import sys;print(sys.version_info[0],sys.version_info[1],sep='.')" 2^>^> "%LOG_FILE%"') do (
    set /a MAJ=%%a & set /a MIN=%%b
)
if %MAJ% LSS 3 (
    echo [错误] 需要 Python 3.9+，当前是 Python %MAJ%.%MIN%
    pause & exit /b 1
)
if %MIN% LSS 9 (
    echo [错误] 需要 Python 3.9+，当前是 Python %MAJ%.%MIN%
    pause & exit /b 1
)

REM ---- 文件检查 ----
set "MISSING="
for %%F in (web_server.py index.html profile_module.py product_searcher.py recommender.py order_manager.py request_parser.py shopping_agent.py) do (
    if not exist "%%~F" set "MISSING=%%~F !MISSING!"
)
if defined MISSING (
    echo [错误] 缺少必要文件：%MISSING%
    pause & exit /b 1
)

REM ---- 端口检查 & 启动服务（web_server.py 内部会开浏览器）----
set "PORT=8765"
chcp 65001 >nul 2>&1
echo ============================================================
echo   🛒 全自动个人购物AI助手 - 网页版
echo   Python:   %PY_EXE%
echo   地址:     http://127.0.0.1:%PORT%/
echo   日志:     %LOG_FILE%
echo   (关闭本窗口即停止服务)
echo ============================================================
echo.
echo 正在启动... 浏览器稍后会自动打开。
echo 若未自动打开，请手动访问上面的地址。
echo.

REM 启动服务（由 web_server.py 内部调用 webbrowser.open 打开浏览器）
%PY_CMD% -u "%~dp0web_server.py" %PORT% 2>>"%LOG_FILE%"

if not %ERRORLEVEL%==0 (
    echo.
    echo [服务异常退出，错误日志如下]
    type "%LOG_FILE%"
    echo.
    echo 请把以上内容发给开发人员排查。
    pause
)
exit /b 0

:log
setlocal
echo %*>>"%LOG_FILE%"
endlocal
goto :eof
