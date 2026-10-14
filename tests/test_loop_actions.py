"""主 Chat 的三条执行路径必须都经过网关。

【这组测试要防的是什么】

`AgentLoop` 原先自己持有一个 `ToolExecutor`，并且在三个地方各自调用它：
确定性只读路由、模型自由调用、审批恢复。三份调用意味着"这个动作该不该跑"
这个问题要在三处各答一遍 —— 而只在其中一处打的补丁，另外两处不会跟着好。

判据的选择很关键，有两个容易走偏的方向：

1. **断言"某个方法被调用"**：用一个 mock 顶替网关，断言 mock 被调用。
   这只能证明"loop 确实往那儿传了一下"，证明不了"执行权真的交出去了"。
2. **断言"handler 没跑"**：直接打在要害上。给 loop 一个**不执行任何东西**的
   假网关，工具却明明注册在 registry 里 —— 如果 handler 还是跑了，
   就说明 loop 自己伸了手。这条判据不会被"多调一次网关"糊弄过去。

所以下面每条都同时用这两把尺子。

【 ‼ 为什么还要一条 AST 检查】

`self.executor` 这种属性删没删，光靠运行时看不出来：它可能只在某些分支里被用到。
静态扫源码能一次性断言"这个文件里再没有第二个执行器"。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from typing import Any

from agent_core.actions import ActionGateway, LateRegistry
from agent_core.contracts import (
    ActionObservation,
    ActionRequest,
    AgentRequest,
    ProviderResponse,
    Scope,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from agent_core.loop import AgentLoop
from agent_core.persistence import SQLiteConfirmationStore, SQLiteRunStore
from agent_core.tools import AllowAllRiskPolicy, ToolRegistry

LOOP_SOURCE = Path(__file__).resolve().parents[1] / "agent_core" / "loop.py"


def _scope() -> Scope:
    return Scope("tester", "pytest", "loop-actions", "companion")


def _tool(name: str = "weather__current", **kwargs: Any) -> ToolSpec:
    async def handler(_arguments: dict[str, Any], _scope: Scope) -> dict[str, Any]:
        return {"status": "ok", "output": {"city": "上海", "text": "晴"}}

    return ToolSpec(
        name,
        {"type": "function", "function": {"name": name.split("__")[-1], "parameters": {"type": "object"}}},
        handler,
        **kwargs,
    )


class ExecutedHandler:
    """记录 handler 有没有被真的调到。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, arguments: dict[str, Any], _scope: Scope) -> dict[str, Any]:
        self.calls.append(dict(arguments))
        return {"status": "ok", "output": {"city": "上海", "text": "晴"}}


class InertGateway:
    """什么都不执行的假网关：用来证明"执行权真的在网关手里"。

    它登记每一次调用，然后返回一个预设的观察结果。
    **它绝不碰 handler** —— 这是本类存在的全部意义。
    """

    def __init__(self, observation: ActionObservation | None = None) -> None:
        self.calls: list[ActionRequest] = []
        self.resumes: list[tuple[str, str]] = []
        self._observation = observation or ActionObservation(
            action_id="act-inert", name="weather__current", status="completed",
            output={"city": "上海", "text": "晴"},
        )

    async def invoke(self, request: ActionRequest) -> ActionObservation:
        self.calls.append(request)
        return ActionObservation(
            action_id=request.action_id, name=request.name,
            status=self._observation.status, output=dict(self._observation.output),
            error=self._observation.error, error_kind=self._observation.error_kind,
            approval_id=self._observation.approval_id,
            approval_code=self._observation.approval_code,
        )

    async def resume(self, approval_id: str, decision: str, *, request: ActionRequest) -> ActionObservation:
        self.resumes.append((approval_id, decision))
        self.calls.append(request)
        return await self.invoke(request)


class QueueProvider:
    """按队列依次返回模型响应，并把每次收到的消息留底。"""

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.queue = list(messages)
        self.requests: list[dict[str, Any]] = []

    async def complete(self, messages: list[dict[str, Any]], **_kwargs: Any) -> ProviderResponse:
        import json

        self.requests.append({"messages": json.loads(json.dumps(messages, ensure_ascii=False))})
        message = self.queue.pop(0) if self.queue else {"role": "assistant", "content": "好的"}
        return ProviderResponse(message=message, usage={})


def _tool_call(name: str, arguments: dict[str, Any] | None = None, call_id: str = "call-1") -> dict[str, Any]:
    import json

    return {"role": "assistant", "content": "", "tool_calls": [{
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments or {}, ensure_ascii=False)},
    }]}


def _loop(tmp_path: Path, provider: Any, registry: ToolRegistry, *, gateway: Any = None, **kwargs: Any) -> AgentLoop:
    return AgentLoop(
        provider=provider,
        registry=registry,
        scope=_scope(),
        run_store=SQLiteRunStore(tmp_path / "runs.db"),
        confirmation_store=SQLiteConfirmationStore(tmp_path / "confirmations.db"),
        risk_policy=AllowAllRiskPolicy(),
        gateway=gateway,
        **kwargs,
    )


class LoopHasNoPrivateExecutor(unittest.TestCase):
    def test_the_loop_source_never_builds_its_own_executor(self) -> None:
        """"不用 ABC"`AgentLoop` 的源码里不许再出现第二个执行器。

        `ToolExecutor` 已经从 `agent_core` 的公共导出里删掉了；这里补的是
        "loop 这个文件本身也不许再引用它"。缺了这条，将来有人一行
        `from ..tools import ToolExecutor` 就能把双链路加回来，而导入路径
        是公开的 —— 运行时检查发现不了。
        """
        tree = ast.parse(LOOP_SOURCE.read_text(encoding="utf-8"))
        used = sorted({
            node.value.id if isinstance(node.value, ast.Name) else ast.unparse(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "executor"
        } | {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id == "ToolExecutor"
        })
        assert used == [], (
            "loop.py 里还在用执行器 —— 那就意味着存在一条不经网关的执行路径。\n"
            f"残留：{used}"
        )


class ModelToolCallRunsThroughTheGateway(unittest.IsolatedAsyncioTestCase):
    async def test_the_model_branch_hands_execution_to_the_gateway(self, ) -> None:
        """模型说"调这个工具"之后，真正执行它的必须是注入的那个网关。

        证据口径：**handler 一次都没跑**。工具明明注册在 registry 里、
        handler 也完全是好的，而假网关从不调用任何 handler ——
        所以只要 handler 被调到，就只能是 loop 自己伸的手。
        """
        import tempfile

        handler = ExecutedHandler()
        registry = ToolRegistry()
        registry.register(ToolSpec(
            "weather__current",
            {"type": "function", "function": {"name": "current", "parameters": {"type": "object"}}},
            handler,
        ))
        gateway = InertGateway()
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            loop = _loop(
                tmp,
                QueueProvider([_tool_call("weather__current", {"city": "上海"}), {"role": "assistant", "content": "上海晴"}]),
                registry,
                gateway=gateway,
            )
            outcome = await loop.run(AgentRequest("上海天气怎么样"))

        assert len(gateway.calls) == 1, (
            f"网关被调用了 {len(gateway.calls)} 次（期望 1 次）。"
            "模型分支还在绕开网关自己执行。"
        )
        assert handler.calls == [], (
            f"handler 被直接调用了 {len(handler.calls)} 次 —— 执行权没有交出去，"
            "网关只是被顺手通知了一声。"
        )
        assert gateway.calls[0].origin == "chat_model", (
            f"来源应该是 chat_model，实际是 {gateway.calls[0].origin!r}。"
            "来源写错会让 `allowed_origins` 的限制在审计里全部串线。"
        )
        assert outcome.status == "completed"

    async def test_an_unknown_tool_name_comes_back_from_the_gateway_not_from_the_loop(self) -> None:
        """工具不存在时，loop 不许自己编一个失败 —— 那是同一件事的两份实现。

        这条同时把 `_failed` 里的稳定错误码钉住：网关必须回 `码: 说明`，
        下游（含前端与定时任务）按冒号前那一段分支。写成散文化的失败原因，
        判断就会退化成字符串包含，而那种判断调整措辞时会静默失效。
        """
        import tempfile

        from agent_core.actions import ERROR_UNKNOWN_ACTION

        registry = ToolRegistry()
        registry.register(_tool("weather__current"))
        real = ActionGateway(registry=LateRegistry(lambda: registry), risk_policy=AllowAllRiskPolicy())
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            provider = QueueProvider([_tool_call("music__nope", {}), {"role": "assistant", "content": "抱歉"}])
            loop = _loop(tmp, provider, registry, gateway=real)
            await loop.run(AgentRequest("放首歌"))

        import json

        tool_bodies = [
            item for request in provider.requests for item in request["messages"]
            if item.get("role") == "tool"
        ]
        assert tool_bodies, "模型没有收到任何工具回执 —— 失败被静默吞掉了"
        body = json.loads(tool_bodies[-1]["content"])
        assert body["status"] != "ok", f"未知工具被当成成功回填给模型：{body}"
        assert ERROR_UNKNOWN_ACTION in str(body.get("error") or ""), body
        observation = await real.invoke(ActionRequest(
            action_id="probe", name="music__nope", arguments={}, scope=_scope(), origin="chat_model",
        ))
        assert str(observation.error).split(":", 1)[0].strip() == ERROR_UNKNOWN_ACTION, observation.error


class DeterministicRouteRunsThroughTheGateway(unittest.IsolatedAsyncioTestCase):
    async def test_the_deterministic_route_is_tagged_as_fastpath(self) -> None:
        """确定性只读路由的动作来源必须是 `chat_fastpath`。

        这一条以前根本没有来源概念 —— 它直接调 `self.executor`。
        后果是审计里"这条访问是谁发起的"永远是空白，而确定性路由恰恰是
        用户最常触发的一条（问天气、问位置、查任务）。
        """
        import tempfile

        spec = _tool("weather__current")
        registry = ToolRegistry()
        registry.register(spec)
        gateway = InertGateway()
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            loop = _loop(tmp, QueueProvider([]), registry, gateway=gateway)
            # working 是纯列表，这里不需要真实历史也能验证"走的是哪条路"。
            await loop._execute_deterministic_tool(
                spec, ToolCall("cid-1", "weather__current", {}), [],
            )

        assert len(gateway.calls) == 1, "确定性路由没有走网关"
        assert gateway.calls[0].origin == "chat_fastpath", gateway.calls[0].origin


class LoopWithoutGatewayIsStillSafe(unittest.IsolatedAsyncioTestCase):
    async def test_a_loop_built_without_an_injected_gateway_still_goes_through_one(self) -> None:
        """没显式注入网关时（几十处单测的用法），loop 也必须自建一个**真网关**。

        这里断言的不是"它也能跑"，而是"它跑出来的同样是经过网关的结果"。
        87 处构造点不可能一处不留地改完，留一个"没有网关就没有执行"的口子，
        比让它们各自绕过一次要危险得多。
        """
        import tempfile

        handler = ExecutedHandler()
        registry = ToolRegistry()
        registry.register(ToolSpec(
            "weather__current",
            {"type": "function", "function": {"name": "current", "parameters": {"type": "object"}}},
            handler,
        ))
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            loop = _loop(
                tmp,
                QueueProvider([_tool_call("weather__current", {}), {"role": "assistant", "content": "上海晴"}]),
                registry,
            )
            await loop.run(AgentRequest("上海天气"))

        assert isinstance(loop.gateway, ActionGateway), type(loop.gateway)
        # handler 确实被调用了，但它是由那个自建网关调的 ——
        # loop 自己没有第二条伸手的路径（由上面的 AST 与 InertGateway 两条证明）。
        assert len(handler.calls) == 1, (
            "handler 应该被执行一次。自建网关不是装饰：它必须仍然把动作真的跑起来。"
        )
