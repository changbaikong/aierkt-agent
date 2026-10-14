"""运行时能力探测的共享契约。

【在系统里的位置】
/api/health、/api/ready、以及 CapabilityDialog（前端"能力"面板）展示的
"某个依赖是不是真的可用"，最终都来自这里定义的探测函数。

【为什么要单独定一个 Protocol】
探测的**唯一职责是"报告依赖健康"**，绝不能顺手执行真正的业务动作。
把签名固定成"返回一串安全的健康记录"，就不可能在探测过程中误发一条消息、
真去写一遍文件。能力探测因此可以放心在 health 检查里高频调用。
"""

from __future__ import annotations

from typing import Any, Awaitable, Protocol


class CapabilityProbe(Protocol):
    """Return safe dependency health records without performing business actions."""

    # force=True 表示"忽略缓存，重新真实探测一次"（对应 /api/capabilities/refresh）。
    # 每条记录形如 {"name": ..., "status": ..., "reason": ...}，由上层汇总成 ready/not_ready。
    def __call__(self, *, force: bool = False) -> Awaitable[list[dict[str, Any]]]:
        ...
