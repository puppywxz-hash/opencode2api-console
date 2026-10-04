@echo off
REM ============================================================
REM  opencode2api 控制台 启动器
REM  用法：双击本文件，或命令行 start_console.bat
REM ============================================================
setlocal
cd /d "%~dp0"

if not exist "opencode2api.exe" (
    echo [错误] 未找到 opencode2api.exe（上游网关）。
    echo.
    echo 请先获取网关，二选一：
    echo   1) 运行下载脚本： powershell -ExecutionPolicy Bypass -File download_gateway.ps1
    echo   2) 自行从上游 Releases 下载 Windows 版，解压出 opencode2api.exe 放到本目录
    echo      https://github.com/jasonxu114514/opencode2api/releases
    echo.
    pause
    exit /b 1
)

if not exist "o2a_console.exe" (
    echo [错误] 未找到 o2a_console.exe（本控制台）。
    echo 请从 GitHub Releases 下载完整发布包（含 o2a_console.exe），或运行源码 opencode2api_gui.py。
    echo.
    pause
    exit /b 1
)

echo 正在启动 opencode2api 控制台…
start "" "o2a_console.exe"
exit /b 0
