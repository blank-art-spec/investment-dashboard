@echo off
chcp 936 >nul
setlocal
rem 功能：调用中文停止脚本，先核对项目进程归属再关闭服务；无参数。
rem 编码：本文件使用当前中文 Windows 命令行的代码页 936，避免命令被截断。
rem DASH_NO_PAUSE=1 只用于检查入口，可跳过最后的按键等待；双击时仍保留窗口。
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0停止本地服务.ps1"
set "RESULT=%ERRORLEVEL%"
if not "%DASH_NO_PAUSE%"=="1" pause
exit /b %RESULT%
