"""后台播放辅助：播放完成后把网易云客户端主窗口彻底隐藏，纯后台响。

netease__play 通过 orpheus:// 深链让本机网易云客户端播放。客户端收到深链
时会把主窗口激活顶到前台；本模块在播放成功后把主窗口彻底隐藏
（ShowWindow(SW_HIDE)），连任务栏图标都不出现，音乐继续后台播放。

注意：隐藏是每播一次生效一次；客户端若再次自己 show 窗口，重试会再次隐藏。
用户之后想用窗口，点桌面图标/托盘图标即可恢复。

只在 Windows 上生效；其他平台是安全的无操作。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

# ShowWindowAsync 的 nCmdShow 取值：SW_HIDE = 0（彻底隐藏窗口，含任务栏图标）
_SW_HIDE = "0"

# 网易云客户端主进程名（Electron 多进程，只有主进程持有窗口句柄）
_PROCESS_NAMES = "cloudmusic"

# user32.ShowWindowAsync 动态加载 + 把网易云主窗口隐藏。
# 每次执行都是独立 PowerShell 进程，类型名重复注册无冲突。
_HIDE_WINDOW_SCRIPT = r"""
$signature = '[DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);'
$hide = Add-Type -MemberDefinition $signature -Name NativeShowAsync -Namespace Native -PassThru
Get-Process -Name cloudmusic -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowHandle -ne 0 } |
    ForEach-Object { [void]$hide::ShowWindowAsync($_.MainWindowHandle, 0) }
""".strip()

PowerShellRunner = Callable[[str], Awaitable[str]]


async def _run_powershell(script: str) -> str:
    """异步执行一段 PowerShell，返回 stdout；非 win32 时为安全无操作。"""
    if sys.platform != "win32":
        return ""
    process = await asyncio.create_subprocess_exec(
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=15)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise
    return stdout.decode("utf-8", errors="replace")


async def hide_netease_window(
    *,
    attempts: int = 4,
    interval_seconds: float = 0.3,
    runner: PowerShellRunner | None = None,
) -> int:
    """反复尝试彻底隐藏网易云客户端主窗口（SW_HIDE，不含任务栏图标）。

    参数:
        attempts: 最多尝试次数。客户端可能在播放后才重新 show 窗口，
            多试几次兜底。
        interval_seconds: 两次尝试之间的间隔秒数；传 0 可跳过等待（测试用）。
        runner: 可注入的 PowerShell 执行器，便于测试。

    返回:
        实际成功执行的次数；非 Windows 平台为 0。
    """
    if sys.platform != "win32" and runner is None:
        return 0
    execute = runner or _run_powershell
    executed = 0
    for _ in range(max(1, int(attempts))):
        try:
            await execute(_HIDE_WINDOW_SCRIPT)
        except Exception as exc:
            logger.debug("hide attempt failed: %s: %s", type(exc).__name__, exc)
        else:
            executed += 1
        if interval_seconds > 0:
            await asyncio.sleep(interval_seconds)
    return executed


__all__ = ["PowerShellRunner", "hide_netease_window"]