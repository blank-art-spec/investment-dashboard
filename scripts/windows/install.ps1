<#
功能：为当前投资仪表盘安装独立的 Python 和依赖，不启动或编译业务程序。
参数：无。所有路径以仓库根目录（本脚本上两级）为基准，双击“安装依赖.bat”即可调用。
目录：.runtime 保存便携工具、Python 和临时文件；.venv 保存项目依赖。
隔离：不改系统 PATH、注册表或其他项目环境；已有 Edge 只被复用，不会被安装或更新。
说明：只使用现成的二进制包（wheel），禁止从源码构建，也不预编译 Python 字节码。
参考：https://docs.astral.sh/uv/reference/cli/
#>
$ErrorActionPreference = 'Stop'
# 脚本集中到 scripts/windows 后，向上两级定位项目，不依赖当前工作目录。
$taskRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$runtimeDir = Join-Path $taskRoot '.runtime'
$toolsDir = Join-Path $runtimeDir 'tools'
$pythonDir = Join-Path $runtimeDir 'python'
$tempDir = Join-Path $runtimeDir 'tmp'
$uvPath = Join-Path $toolsDir 'uv.exe'
$venvDir = Join-Path $taskRoot '.venv'
$pythonPath = Join-Path $venvDir 'Scripts\python.exe'
$requirementsPath = Join-Path $taskRoot 'requirements.txt'
$lockPath = Join-Path $taskRoot 'requirements.local.lock.txt'
# 仓库版本约束供新电脑复现；已有本地约束优先，避免升级本机环境。
$sharedLockPath = Join-Path $taskRoot 'requirements.lock.txt'

try {
    # 环境变量仅作用于本次 PowerShell 及其子进程，窗口关闭后不影响其他程序。
    New-Item -ItemType Directory -Path $toolsDir,$pythonDir,$tempDir -Force | Out-Null
    $env:UV_PYTHON_INSTALL_DIR = $pythonDir
    $env:UV_CACHE_DIR = Join-Path $runtimeDir 'cache'
    $env:UV_PYTHON_INSTALL_BIN = '0'
    $env:UV_PYTHON_INSTALL_REGISTRY = '0'
    $env:UV_NO_CONFIG = '1'
    $env:UV_COMPILE_BYTECODE = '0'
    $env:UV_LINK_MODE = 'copy'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:PYTHONNOUSERSITE = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUTF8 = '1'
    $env:TMP = $tempDir
    $env:TEMP = $tempDir

    # 便携工具下载地址和 SHA-256 均来自 uv 官方发布；不运行全局安装器。
    if (-not (Test-Path -LiteralPath $uvPath)) {
        Write-Host '正在下载本目录专用的便携安装工具……'
        $archivePath = Join-Path $toolsDir 'uv.zip'
        Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/astral-sh/uv/releases/download/0.12.24/uv-x86_64-pc-windows-msvc.zip' -OutFile $archivePath
        $expectedHash = '7c38608c8a18ee137d748a1773053b07ec8f3a30fab49aebaa6f4e4efeceb019'
        if ((Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedHash) {
            throw '下载包校验失败，请重新安装；未执行下载包中的程序。'
        }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $archive = [IO.Compression.ZipFile]::OpenRead($archivePath)
        try {
            $entry = $archive.Entries | Where-Object { $_.Name -eq 'uv.exe' } | Select-Object -First 1
            if (-not $entry) { throw '下载包内缺少 uv.exe。' }
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $uvPath, $true)
        } finally {
            $archive.Dispose()
        }
        Remove-Item -LiteralPath $archivePath
    }

    # Python 只存入 .runtime；--no-bin 和 --no-registry 禁止创建全局入口及注册信息。
    Write-Host '步骤 1/3：准备项目专用 Python 3.12……'
    & $uvPath python install 3.12 --install-dir $pythonDir --no-bin --no-registry --no-config --no-cache
    if ($LASTEXITCODE -ne 0) { throw '本地 Python 安装失败。' }

    # 虚拟环境明确指定 managed-python，不使用 PATH 中的全局 Python。
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        & $uvPath venv $venvDir --python 3.12 --managed-python --no-config --no-cache
        if ($LASTEXITCODE -ne 0) { throw '项目虚拟环境创建失败。' }
    }

    # 上次保存的版本约束防止重复安装时无意升级；首次安装后生成版本记录。
    Write-Host '步骤 2/3：安装项目依赖（只使用现成安装包，不编译）……'
    $installArgs = @('pip','install','--python',$pythonPath,'--requirements',$requirementsPath,'--only-binary',':all:','--no-config','--no-cache')
    if (Test-Path -LiteralPath $lockPath) {
        $installArgs += @('--constraint',$lockPath)
    } elseif (Test-Path -LiteralPath $sharedLockPath) {
        $installArgs += @('--constraint',$sharedLockPath)
    }
    & $uvPath @installArgs
    if ($LASTEXITCODE -ne 0) { throw '项目依赖安装失败。' }
    & $uvPath pip check --python $pythonPath --no-config --no-cache
    if ($LASTEXITCODE -ne 0) { throw '依赖兼容性检查失败。' }
    $packages = & $uvPath pip freeze --python $pythonPath --no-config --no-cache
    if ($LASTEXITCODE -ne 0) { throw '依赖版本记录失败。' }
    [IO.File]::WriteAllLines($lockPath, [string[]]$packages, (New-Object Text.UTF8Encoding($false)))

    # 源代码使用 msedge 通道或 Edge 路径，无需额外下载 Chromium。
    Write-Host '步骤 3/3：检查现有 Edge 浏览器……'
    $edgePaths = @('C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe','C:\Program Files\Microsoft\Edge\Application\msedge.exe')
    $edgePath = $edgePaths | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if ($edgePath) {
        Write-Host "已复用现有 Edge：$edgePath"
    } else {
        Write-Warning '未发现 Edge。主程序依赖已安装，网页抓取功能还需要手动安装 Edge。'
    }
    Write-Host '安装完成。请手动双击“启动投资仪表盘.vbs”验证程序运行。'
    exit 0
} catch {
    Write-Host ("安装失败：" + $_.Exception.Message) -ForegroundColor Red
    exit 1
}