"""测试共享：从 tests/test_routing.py 和 tests/test_route_guardrails.py 抽出共用的 FakeRegistry。

只提供路由层用到的 resolve —— 套 1/3/6 矩阵直接用它，避免每个矩阵各自再定义一遍。
"""

from __future__ import annotations

from typing import Any, Iterable

from agent_core.routing import PLAN_SUBMIT_TOOL, SUBAGENT_TOOL, WORK_SUBMIT_TOOL


class FakeRegistry:
    """最小 registry：只提供 resolve，返回 object() 占位或 None。

    路由层只问"这个工具在不在"，不真调 handler，所以占位对象就够。
    """

    def __init__(self, names: Iterable[str] = ()) -> None:
        self._names = set(names)

    def resolve(self, name: str) -> Any | None:
        return object() if name in self._names else None


def full_registry() -> FakeRegistry:
    """三件套齐全：work__submit + plan_submit + subagent__dispatch。"""
    return FakeRegistry([WORK_SUBMIT_TOOL, PLAN_SUBMIT_TOOL, SUBAGENT_TOOL])
