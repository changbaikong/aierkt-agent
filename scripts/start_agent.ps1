<#
.SYNOPSIS
    启动本地 Agent：后端（web_api.py）+ 前端（Vite）。

.DESCRIPTION
    真正的启动逻辑在 scripts/start_agent.py —— 那个文件负责端口探测、
    健康检查、日志重定向和失败原因区分。本脚本只是它的入口。

    为什么不在 PowerShell 里做：用户目录可能含中文（例：C:\Users\<中文用户名>\...），
    而 Windows PowerShell 5.1 按系统 ANSI 代码页读 .ps1 与拼命令行，
    中文路径在传递途中会被重新编码，症状是「系统找不到指定的文件」——
    看着像文件不存在，其实是路径被读坏了。Python 全程用 Unicode 处理路径，
    没有这一层问题。

    退出码（由 start_agent.py 给出，本脚本原样透传）：
      0  全部就绪（含"复用了已有服务"）
      1  启动失败（进程提前退出 / 超时 / 前端目录或依赖缺失）
      2  端口被别的程序占用
      3  缺少必需的环境变量

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\start_agent.ps1

.EXAMPLE
    # 只起后端
    powershell -ExecutionPolicy Bypass -File scripts\start_agent.ps1 -NoFrontend

.EXAMPLE
    # 换端口（后端 9000，前端 4000）
    powershell -ExecutionPolicy Bypass -File scripts\start_agent.ps1 -BackendPort 9000 -FrontendPort 4000
#>
[CmdletBinding()]
param(
    [int]$BackendPort = 8765,
    [int]$FrontendPort = 5173,
    [double]$TimeoutSeconds = 60,
    [switch]$NoFrontend
)

$ErrorActionPreference = 'Stop'

# 仓库根 = 本脚本所在目录的上一级。用 $PSScriptRoot 而不是当前工作目录，
# 从任何地方调用结果都一样。
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Launcher = Join-Path $RepoRoot 'scripts\start_agent.py'

if (-not (Test-Path $Launcher)) {
    Write-Host "[start_agent] 找不到启动器：$Launcher"
    exit 1
}

# 允许用 AGENT_PYTHON 指定解释器；否则用 PATH 上的 python。
$python = if ($env:AGENT_PYTHON) { $env:AGENT_PYTHON } else { 'python' }

# 注意别把数组命名为 $args —— 那是 PowerShell 的自动变量，
# 覆盖它会让本脚本自己的参数传递出问题。
$launcherArgs = @(
    $Launcher,
    '--backend-port', "$BackendPort",
    '--frontend-port', "$FrontendPort",
    '--timeout', "$TimeoutSeconds"
)
if ($NoFrontend) { $launcherArgs += '--no-frontend' }

# 不重定向输出：这是给人在终端里看的，进度实时可见比写进文件有用。
& $python @launcherArgs
exit $LASTEXITCODE
