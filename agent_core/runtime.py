"""极简的事件溯源运行时状态，与渠道、UI 无关。

【在系统里的位置】
它是"Agent 现在处于什么阶段"的唯一权威：idle / thinking / waiting_confirmation。
监控台 monitor_console.py 展示的 phase、前端状态指示灯读的都是这里。

【核心思想：事件溯源（event sourcing）】
不直接写"phase = thinking"，而是发布一条事件（turn.started），
再由 reduce_state 这个**纯函数**把事件折叠成新状态。
好处是两个：
1. 状态怎么变成这样的，历史里全都有（events 列表就是审计轨迹）；
2. 折叠逻辑是纯函数，可以脱离 IO 单独测试、也可以在别处重放。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Protocol

from .contracts import Scope


@dataclass(frozen=True)
class AgentEvent:
    """一条不可变的运行时事件。

    type 用点号分层（如 "turn.started"），reduce_state 靠前缀和字面量判断。
    turn_id 把同一回合的多条事件串起来；event_id / created_at 自动生成，调用方不用管。
    """

    type: str
    scope: Scope
    payload: dict[str, Any] = field(default_factory=dict)
    turn_id: str = ""
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


@dataclass
class AgentState:
    """折叠出来的当前状态。注意它是**可变**的 dataclass（reduce_state 就地更新它）。"""

    owner_id: str
    module_id: str = "companion"
    phase: str = "idle"  # idle / thinking / waiting_confirmation
    active_turn_id: str = ""
    last_event_at: str = ""


class RuntimeStore(Protocol):
    """存储接口。publish 允许返回 awaitable，所以内存实现和持久化实现能同签名替换。"""

    def publish(self, event: AgentEvent) -> AgentState | Awaitable[AgentState]: ...
    def state(self, scope: Scope) -> AgentState: ...


def reduce_state(state: AgentState, event: AgentEvent) -> AgentState:
    """把一条事件折叠进状态（纯函数，就地改 state 并返回它）。

    状态机很短，但"paused 之后必须靠终态事件才能回 idle"这条是有意的：
    - turn.started  → 进入 thinking，记下当前回合
    - turn.paused   → 进入 waiting_confirmation（等用户点确认，可能跨进程重启）
    - turn.completed / turn.failed / turn.cancelled → 回 idle 并清空回合
    除这三类之外的 turn.* 事件不改 phase，避免细碎事件（比如打字机增量）乱动状态。
    """
    state.last_event_at = event.created_at
    if event.type == "turn.started":
        state.phase = "thinking"
        state.active_turn_id = event.turn_id
    elif event.type == "turn.paused":
        state.phase = "waiting_confirmation"
    elif event.type.startswith("turn.") and event.type in {"turn.completed", "turn.failed", "turn.cancelled"}:
        state.phase = "idle"
        state.active_turn_id = ""
    return state


class InMemoryRuntimeStore:
    """进程内实现：状态和事件都只活在内存里，进程一退就没了。

    适合测试和单进程 CLI。多进程/需要重启后恢复 phase 的场合要用持久化实现
    （见 persistence.py 里的 run store）。
    """

    def __init__(self) -> None:
        self.states: dict[Scope, AgentState] = {}
        self.events: list[AgentEvent] = []

    def state(self, scope: Scope) -> AgentState:
        # setdefault：第一次访问某个作用域时自动建一个 idle 状态，调用方不用先初始化。
        return self.states.setdefault(scope, AgentState(scope.owner_id, scope.module_id))

    def publish(self, event: AgentEvent) -> AgentState:
        # 先折叠出状态，再把原始事件追加进历史（两者都留，便于回放与排障）。
        state = reduce_state(self.state(event.scope), event)
        self.events.append(event)
        return state
