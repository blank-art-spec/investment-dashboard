<#
功能：保留原有安装入口，转交 scripts/windows/install.ps1；不启动或编译业务程序。
参数：无。所有环境和依赖仍安装在本项目根目录的 .runtime 与 .venv 内。
返回：沿用实际安装脚本的退出码，0 为成功，非 0 为失败。
学习说明：入口与实现分离后，只需维护 scripts/windows 下的一份安装逻辑。
#>
& (Join-Path $PSScriptRoot 'scripts\windows\install.ps1')
exit $LASTEXITCODE
