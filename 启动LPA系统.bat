@echo off
title LPA 车间问题分层审核与跟踪系统
REM ============================================================
REM LPA 系统一键启动脚本
REM 用法: 双击本文件启动系统, 关闭本窗口即停止系统
REM 注意: 本文件为 ANSI(GBK)编码+CRLF换行, 请勿用记事本另存为UTF-8
REM ============================================================

cd /d "D:\lpa_audit_system"

if not exist "app.py" (
    echo [错误] 在 D:\lpa_audit_system 下找不到 app.py
    echo 请确认项目文件已复制到该目录后再试
    pause
    exit /b 1
)

if exist "venv\Scripts\python.exe" (
    set "PYTHON=venv\Scripts\python.exe"
) else (
    set "PYTHON=python"
)

echo ============================================
echo   正在启动 LPA 车间问题分层审核与跟踪系统...
echo   浏览器将自动打开, 关闭本窗口即停止系统
echo ============================================
echo.

%PYTHON% -m streamlit run app.py

echo.
echo 系统已停止。如上方有报错信息, 请截图留存。
pause
