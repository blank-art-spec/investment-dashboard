<#
功能：保留原有停止入口，转交 scripts/windows/stop.ps1，不按进程名批量关闭程序。
参数：无；DASH_PORT 由实际脚本读取，未设置时使用 5001。
返回：沿用实际脚本的退出码，0 为完成，1 为停止失败。
学习说明：此文件只做入口转发，进程归属检查仍由实际停止脚本执行。
#>
& (Join-Path $PSScriptRoot 'scripts\windows\stop.ps1')
exit $LASTEXITCODE
