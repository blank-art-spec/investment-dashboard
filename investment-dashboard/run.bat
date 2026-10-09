@echo off
chcp 936 >nul
setlocal
rem 功能：保留旧目录的前台启动入口，转交新的集中脚本；无参数。
rem 返回：保留实际启动脚本的退出码，避免维护两套运行逻辑。
call "%~dp0..\scripts\windows\run.bat"
exit /b %ERRORLEVEL%
