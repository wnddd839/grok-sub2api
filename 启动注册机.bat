@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

title Grok 注册机

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到 .venv\Scripts\python.exe
    echo 请先在项目目录创建 Python 虚拟环境并安装依赖。
    pause
    exit /b 1
)

if not exist "grok_register_ttk.py" (
    echo [错误] 未找到 grok_register_ttk.py
    pause
    exit /b 1
)

echo [启动] Grok 注册机...
".venv\Scripts\python.exe" "grok_register_ttk.py"

if errorlevel 1 (
    echo.
    echo [错误] 程序异常退出，错误码：%errorlevel%
    pause
)

endlocal
