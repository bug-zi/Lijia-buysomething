@echo off
REM ============================================================
REM  全自动个人购物AI助手 - 一键启动脚本（Windows）
REM  适用范围：双击运行 / 右键发送到桌面快捷方式
REM  改进点：
REM    1. 兼容 cmd 默认编码（ANSI/GBK），避免中文乱码闪退
REM    2. 自动探测 Python 解释器（py 启动器 / python / python3 / 常见安装路径）
REM    3. 所有错误输出到 %TEMP%\shopping_agent_error.log，方便排查
REM    4. 出错后自动 pause，不再一闪而过
REM ============================================================

setlocal EnableExtensions DisableDelayedExpansion

REM ---- 路径与日志 ----
cd /d "%~dp0"
set "LOG_FILE=%TEMP%\shopping_agent_error.log"
del /f /q "%LOG_FILE%" 2>nul
call :log "========== 启动日志 %DATE% %TIME% =========="
call :log "工作目录: %cd%"

REM ---- 切换到 UTF-8 以便 Python 脚本输出中文不乱码（失败不影响）----
chcp 65001 >nul 2>&1

REM ---- 第 1 步：寻找 Python 解释器 ----
set "PY_CMD="
set "PY_FOUND=0"

REM 策略 1：py 启动器（推荐）
where /q py.exe 2>nul
if %ERRORLEVEL%==0 (
    for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>"%LOG_FILE%"') do set "PY_EXE=%%i"
    if defined PY_EXE (
        set "PY_CMD=py -3"
        goto :py_found
    )
)

REM 策略 2：python 命令
where /q python.exe 2>nul
if %ERRORLEVEL%==0 (
    for /f "delims=" %%i in ('python -c "import sys;print(sys.executable)" 2^>>"%LOG_FILE%"') do set "PY_EXE=%%i"
    if defined PY_EXE (
        set "PY_CMD=python"
        goto :py_found
    )
)

REM 策略 3：python3 命令
where /q python3.exe 2>nul
if %ERRORLEVEL%==0 (
    set "PY_CMD=python3"
    goto :py_found
)

REM 策略 4：搜索常见安装目录（D盘优先，符合用户偏好）
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
    "C:\Program Files\Python311\python.exe"
    "C:\Program Files\Python312\python.exe"
) do (
    if exist %%~P (
        set "PY_CMD=%%~P"
        goto :py_found
    )
)

REM ====== 未找到 Python 时的友好提示 ======
echo.
echo ============================================================
echo   [启动失败] 未检测到 Python 3.9+
echo ============================================================
echo.
echo   请先安装 Python：
echo     1. 访问官网: https://www.python.org/downloads/windows/
echo     2. 下载 Python 3.11.x 或 3.12.x（推荐装到 D:\Python3xx\）
echo     3. 安装时务必勾选 [v] Add Python to PATH
echo.
echo   已安装？请确认下面命令能否执行：
echo     Win+R -^> 输入 cmd 回车 -^> 输入 python --version
echo.
call :log "[ERROR] 未找到 Python 解释器。py.exe/python.exe/python3 均不存在。"
call :log "PATH=%PATH%"
echo   详细日志见："%LOG_FILE%"
echo.
pause
exit /b 1

:py_found
set "PY_FOUND=1"
call :log "Python 解释器: %PY_CMD%"
call :log "实际路径: %PY_EXE%"

REM ---- 第 2 步：打印 Python 版本 ----
echo ============================================================
echo   Shopping Agent - 全自动个人购物AI助手
echo   工作目录: %cd%
echo   Python:   %PY_CMD%  (%PY_EXE%)
echo ============================================================
echo.

REM ---- 第 3 步：版本检查（需要 3.9+）----
set /a PY_VER_MAJOR=0
set /a PY_VER_MINOR=0
for /f "tokens=1,2 delims=." %%a in ('%PY_CMD% -c "import sys;print(sys.version_info[0],sys.version_info[1],sep='.')" 2^>^> "%LOG_FILE%"') do (
    set /a PY_VER_MAJOR=%%a
    set /a PY_VER_MINOR=%%b
)
call :log "Python 版本: %PY_VER_MAJOR%.%PY_VER_MINOR%"
if %PY_VER_MAJOR% LSS 3 (
    echo [错误] 需要 Python 3.9+，你现在是 Python %PY_VER_MAJOR%.%PY_VER_MINOR%
    echo       请安装 Python 3.11 或更高版本。
    pause
    exit /b 1
)
if %PY_VER_MINOR% LSS 9 (
    echo [错误] 需要 Python 3.9+，你现在是 Python %PY_VER_MAJOR%.%PY_VER_MINOR%
    echo       请升级 Python。
    pause
    exit /b 1
)

REM ---- 第 4 步：检查脚本文件是否齐全 ----
set "MISSING="
for %%F in (profile_module.py product_searcher.py recommender.py order_manager.py request_parser.py shopping_agent.py) do (
    if not exist "%%~F" set "MISSING=%%~F !MISSING!"
)
if defined MISSING (
    echo [错误] 缺少必要文件：%MISSING%
    echo       请确认所有 .py 文件都在同目录下。
    pause
    exit /b 1
)

REM ---- 第 5 步：导入检查（任何模块导入失败都显示报错）----
echo [1/2] 正在加载模块，请稍候...
%PY_CMD% -c "import profile_module, product_searcher, recommender, order_manager, request_parser, shopping_agent; print('OK: modules loaded')" 2>>"%LOG_FILE%"
if errorlevel 1 (
    echo.
    echo [错误] 模块加载失败。可能原因：
    echo    1) Python 版本不兼容
    echo    2) 文件损坏或路径含特殊字符
    echo    3) 缺少第三方依赖（本项目只需要Python标准库，通常不是此问题）
    echo.
    echo ---- 详细错误日志 ----
    type "%LOG_FILE%"
    echo ----------------------
    echo.
    echo 日志已保存到："%LOG_FILE%"
    echo 请把以上错误内容发我，我来帮你定位问题。
    echo.
    pause
    exit /b 1
)
echo       模块加载成功。

REM ---- 第 6 步：进入交互模式（支持传 demo 参数）----
echo [2/2] 启动交互模式... （输入 quit / exit / 退出 即可退出）
echo.

%PY_CMD% -u "%~dp0shopping_agent.py" %* 2>>"%LOG_FILE%"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo [提示] 程序异常退出 (退出码=%EXIT_CODE%)，错误日志："%LOG_FILE%"
    echo ---- 错误内容 ----
    type "%LOG_FILE%"
    echo ------------------
)

echo.
echo 再见 👋  想再次使用，双击本 bat 文件即可。
pause
exit /b 0

:log
setlocal
echo %*>> "%LOG_FILE%"
endlocal
goto :eof
