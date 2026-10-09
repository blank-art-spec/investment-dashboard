<#
功能：停止本目录启动的投资仪表盘，不误关其他项目。
参数：无；可用本次进程的 DASH_PORT 指定端口，默认 5001。
识别：既检查端口、app.py 命令，也检查解释器及其父进程属于本目录。
原理：Windows 虚拟环境会先启动 .venv 的解释器入口，再转交 .runtime 中的
真实 Python 子进程。真正监听端口的是子进程，因此必须验证这条父子关系。
返回：退出码 0 表示服务已关闭、未运行或其他项目被保留；1 表示停止失败。
安全：不按进程名称批量结束，不仅凭端口停止，不删除文件或修改其他项目。
#>
$ErrorActionPreference = 'Stop'
# 参数仍来自 DASH_PORT；向上两级定位项目，旧入口与直接调用共用同一环境。
$taskRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
try {
    $port = 5001
    if ($env:DASH_PORT) { $port = [int]$env:DASH_PORT }
    $localPython = [IO.Path]::GetFullPath((Join-Path $taskRoot '.venv\Scripts\python.exe'))
    $localPythonw = [IO.Path]::GetFullPath((Join-Path $taskRoot '.venv\Scripts\pythonw.exe'))
    $localExecutables = @($localPython, $localPythonw)
    $runtimeExecutables = @()
    $configPath = Join-Path $taskRoot '.venv\pyvenv.cfg'

    # 从虚拟环境配置读真实解释器目录，只接受本项目 .runtime\python 内的路径。
    if (Test-Path -LiteralPath $configPath) {
        $homeLine = Get-Content -LiteralPath $configPath -Encoding UTF8 | Where-Object { $_ -match '^home\s*=' } | Select-Object -First 1
        if ($homeLine -match '^home\s*=\s*(.+)$') {
            $runtimeHome = [IO.Path]::GetFullPath($Matches[1].Trim())
            $runtimeRoot = [IO.Path]::GetFullPath((Join-Path $taskRoot '.runtime\python')).TrimEnd('\') + '\'
            if ($runtimeHome.StartsWith($runtimeRoot, [StringComparison]::OrdinalIgnoreCase)) {
                $runtimeExecutables = @((Join-Path $runtimeHome 'python.exe'), (Join-Path $runtimeHome 'pythonw.exe'))
            }
        }
    }

    # 去重监听进程编号，避免同时监听多个地址时重复处理同一进程。
    $listenerIds = @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
        Where-Object { $_.LocalPort -eq $port } |
        Select-Object -ExpandProperty OwningProcess -Unique)
    if ($listenerIds.Count -eq 0) {
        Write-Host ("本项目服务未运行，端口 " + $port + " 没有监听。")
        exit 0
    }

    $stopped = $false
    $appPattern = '(?:^|\s|[\\/"])app\.py(?:\s|"|$)'
    foreach ($listenerId in $listenerIds) {
        $serviceProcess = Get-CimInstance Win32_Process -Filter ("ProcessId = " + $listenerId)
        if (-not $serviceProcess) { continue }

        # 本地解释器可以直接监听；便携解释器则必须有本地虚拟环境父进程作证。
        $isLocalEntry = $localExecutables -contains $serviceProcess.ExecutablePath
        $isRuntimeChild = $runtimeExecutables -contains $serviceProcess.ExecutablePath
        $parentProcess = $null
        $parentIsLocal = $false
        if ($isRuntimeChild) {
            $parentProcess = Get-CimInstance Win32_Process -Filter ("ProcessId = " + $serviceProcess.ParentProcessId)
            $parentIsLocal = $parentProcess -and
                ($localExecutables -contains $parentProcess.ExecutablePath) -and
                ($parentProcess.CommandLine -match $appPattern)
        }
        $isOurService = ($serviceProcess.CommandLine -match $appPattern) -and
            ($isLocalEntry -or ($isRuntimeChild -and $parentIsLocal))
        if (-not $isOurService) {
            Write-Host ("端口 " + $port + " 属于其他程序或无法确认身份，已保留该进程。")
            continue
        }

        # 先停止监听子进程，再清理虚拟环境入口；父进程通常会随子进程退出。
        $targets = @($serviceProcess)
        if ($parentIsLocal) { $targets += $parentProcess }
        foreach ($target in $targets) {
            $currentProcess = Get-CimInstance Win32_Process -Filter ("ProcessId = " + $target.ProcessId)
            # 再比对创建时间，避免进程已经结束、编号被另一程序复用时误操作。
            if ($currentProcess -and $currentProcess.CreationDate -eq $target.CreationDate -and
                $currentProcess.ExecutablePath -eq $target.ExecutablePath) {
                Stop-Process -Id $target.ProcessId -ErrorAction Stop
                Write-Host ("已停止本项目进程：" + $target.ProcessId)
            }
        }
        $stopped = $true
    }

    if ($stopped) {
        Write-Host '本项目后台服务已关闭，可以关闭此窗口。'
    } else {
        Write-Host '未找到可确认属于本项目的服务，没有停止任何程序。'
    }
    exit 0
} catch {
    Write-Host ("停止失败：" + $_.Exception.Message) -ForegroundColor Red
    exit 1
}