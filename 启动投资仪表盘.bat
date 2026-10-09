@echo off
chcp 936 >nul
setlocal
rem 功能：使用项目自己的 Python 启动；不回退到其他项目或系统 Python。
rem 参数：无；既可直接双击，也可由“启动投资仪表盘.vbs”隐藏调用。
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PY%" set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [错误] 本项目环境尚未安装，请先双击“安装依赖.bat”。
  pause
  exit /b 1
)
rem 环境变量仅对本启动器及其子进程生效，避免读取用户级依赖或生成字节码。
set "PYTHONNOUSERSITE=1"
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
"%PY%" "%~dp0launcher.py"
exit /b %ERRORLEVEL%
