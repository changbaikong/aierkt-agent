<#
.SYNOPSIS
    停止由 start_agent.ps1 启动的本地 Agent 服务。

.DESCRIPTION
    **只停自己启动的进程**，判据是 PID 文件（由 start_agent.ps1 写出）。
    端口上如果有别人的进程，一律不动 —— 这正是"别杀不属于自己的服务"。

    如果 PID 文件丢了（脚本之外启动的服务），本脚本不猜、不强杀，
    只告诉你端口上是谁，让你自己决定。

.PARAMETER Force
    连"端口上不是自己启动但确实是本项目"的情况也一并处理。
    默认关闭：宁可报一句"请自行确认"，也不要误杀。
#>
[CmdletBinding()]
param(
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$PidDir = Join-Path $RepoRoot 'logs'
$PidFiles = @(
    @{ Name = '后端'; Path = Join-Path $PidDir 'backend.pid' },
    @{ Name = '前端'; Path = Join-Path $PidDir 'frontend.pid' }
)

$stopped = 0
foreach ($entry in $PidFiles) {
    if (-not (Test-Path $entry.Path)) {
        Write-Host "[stop_agent] $($entry.Name)：没有 PID 文件，跳过（不是本脚本启动的）。"
        continue
    }
    $raw = (Get-Content -Path $entry.Path -Raw -ErrorAction SilentlyContinue).Trim()
    $processId = 0
    if (-not [int]::TryParse($raw, [ref]$processId)) {
        Write-Host "[stop_agent] $($entry.Name)：PID 文件内容无法识别（$raw），跳过。"
        continue
    }
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if (-not $process) {
        Write-Host "[stop_agent] $($entry.Name)：进程 $processId 已经不在（PID 文件过期）。"
        Remove-Item -Path $entry.Path -Force -ErrorAction SilentlyContinue
        continue
    }
    Write-Host "[stop_agent] 停止 $($entry.Name)：$($process.ProcessName) (PID $processId)"
    try {
        Stop-Process -Id $processId -Force -ErrorAction Stop
        $stopped++
    } catch {
        Write-Host "[stop_agent] 停止失败：$($_.Exception.Message)"
    }
    Remove-Item -Path $entry.Path -Force -ErrorAction SilentlyContinue
}

if ($stopped -eq 0) {
    Write-Host "[stop_agent] 没有停止任何本脚本启动的进程。"
    Write-Host "[stop_agent] 端口占用情况（如仍有服务在跑，请自行确认归属）："
    foreach ($port in @(8765, 5173)) {
        try {
            $connection = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop |
                Select-Object -First 1
            if ($connection) {
                $owner = Get-Process -Id $connection.OwningProcess -ErrorAction SilentlyContinue
                Write-Host "  - $port : $($owner.ProcessName) (PID $($connection.OwningProcess))"
            }
        } catch {
            # 没有监听者，正常。
        }
    }
}

exit 0
