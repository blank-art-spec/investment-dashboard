@echo off
chcp 936 >nul
setlocal
rem 功能：在前台手动启动根目录 app.py，便于查看日志；不编译、不自动重载。
rem 参数：无；默认端口 5001，可通过当前进程的 DASH_PORT 覆盖。
rem 路径：脚本向上两级找到项目，仅使用该目录 .venv 内的 Python。
cd /d "%~dp0..\.."
set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [错误] 本项目环境尚未安装，请先运行根目录的“安装依赖.bat”。
  pause
  exit /b 1
)
if not defined DASH_PORT set "DASH_PORT=5001"
set "PYTHONNOUSERSITE=1"
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
"%PY%" app.py
set "RESULT=%ERRORLEVEL%"
pause
exit /b %RESULT%
