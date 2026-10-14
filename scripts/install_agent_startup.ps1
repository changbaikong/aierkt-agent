<#
.SYNOPSIS
    把 start_agent.ps1 注册成"当前用户登录时自动启动"的计划任务。

.DESCRIPTION
    幂等：反复执行只会更新同一个任务，不会创建出多个。

    * 任务名固定（AgentLocalStartup）；
    * 触发条件 = 当前用户登录；
    * 默认**不要求管理员权限**（注册在用户自己的任务空间下）；
    * 不把任何密钥写进任务参数或脚本 —— 密钥只留在 .env 里。

.PARAMETER TaskName
    任务名，默认 AgentLocalStartup。

.PARAMETER Uninstall
    卸载：删除该计划任务。

.PARAMETER DelaySeconds
    登录后延迟多少秒再启动，默认 20。
    开机瞬间网络和各种服务都在抢资源，稍等一下成功率高得多。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_agent_startup.ps1

.EXAMPLE
    # 登录后 60 秒再启动，且只起后端
    powershell -ExecutionPolicy Bypass -File scripts\install_agent_startup.ps1 -DelaySeconds 60
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'AgentLocalStartup',
    [switch]$Uninstall,
    [int]$DelaySeconds = 20,
    [switch]$NoFrontend
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$StartScript = Join-Path $RepoRoot 'scripts\start_agent.ps1'

if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $existing) {
        Write-Host "[install_agent_startup] 任务 $TaskName 不存在，无需卸载。"
        exit 0
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "[install_agent_startup] 已删除任务 $TaskName。"
    exit 0
}

if (-not (Test-Path $StartScript)) {
    Write-Host "[install_agent_startup] 找不到启动脚本：$StartScript"
    exit 1
}

# 用 powershell.exe 显式调用，不带任何密钥参数 —— 任务定义是明文存盘的，
# 往里塞密钥等于把密钥写到一个可读的文件里。
$argumentParts = @(
    '-NoProfile',
    '-ExecutionPolicy', 'Bypass',
    '-File', "`"$StartScript`""
)
if ($NoFrontend) { $argumentParts += '-NoFrontend' }

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument ($argumentParts -join ' ') `
    -WorkingDirectory $RepoRoot

# 登录触发：只对当前用户生效，不需要管理员。
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = "PT${DelaySeconds}S"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

# 幂等：先删同名任务再注册，避免反复执行累积出多个。
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "[install_agent_startup] 任务 $TaskName 已存在，更新其定义。"
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description '登录后自动启动本地 Agent（后端 + 前端）。由 scripts/install_agent_startup.ps1 注册。' | Out-Null

Write-Host "[install_agent_startup] 已注册任务 $TaskName（登录后 ${DelaySeconds} 秒启动）。"
Write-Host "[install_agent_startup] 查看：Get-ScheduledTask -TaskName $TaskName"
Write-Host "[install_agent_startup] 立即试跑：Start-ScheduledTask -TaskName $TaskName"
Write-Host "[install_agent_startup] 取消：powershell -ExecutionPolicy Bypass -File scripts\install_agent_startup.ps1 -Uninstall"
exit 0
