@echo off
chcp 936 >nul
setlocal
rem 仅调用本目录安装脚本；执行策略只在本次进程中生效。
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0安装本地环境.ps1"
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" echo [错误] 安装未完成，请查看上方中文提示。
pause
exit /b %RESULT%
