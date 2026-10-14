"""Work 的真实动作必须过网关。

【这一条为什么是四条路径里最该收的】

`LocalWorkerProvider` 是系统里唯一会真的**改本机状态**的一段代码：
移动文件、截屏、调系统音量。它跑的场景是后台队列 —— 没有人盯着看，
也没有第二轮机会。审批那套（Host 的 `_requires_approval`）挡在最外面一层，
只回答"要不要先问用户一句"；再往里，真正把副作用做掉的那几行，
从来没有哪一层问过"这个动作该不该跑" —— 参数合不合法、超时怎么处理、
异常怎么归类、有没有留下记录，全靠运气。

    result = await asyncio.to_thread(self.files.apply_moves, effective)
    png    = await asyncio.to_thread(self._screenshot_capture)
    result = await asyncio.to_thread(self._volume_adjust, percent)

这三行之前没有任何一层会问"这个动作该不该跑"。Host 的 `_requires_approval`
挡在更外面一层，但它只管"要不要先问一句"，参数合法性、超时边界、
异常归类、执行留痕全都没人做。

【这个文件守三条】

1. **网关不认识这些动作时，一个副作用都发生不了**（最硬的一条）：把网关换成
   "什么都不认识"的空网关，四种任务必须全部失败，且注入的假音量/假截屏
   一次都没被碰到 —— 这证明效果就是从网关那条路出去的，没有第二条。
2. **每个真实动作带着 `work_host` 这个来源进网关**：来源限制（`allowed_origins`）
   只能靠它生效，缺了它就等于谁都能叫得动这几个内部动作。
3. **源码扫描**：那几个 effector 只允许出现在 `_act_*` 的处理函数里。
   这条是第 1 条的静态版本 —— 运行时证明"现在没有旁路"，
   源码扫描证明"下一个改动也不会顺手加回一条"。

【本期故意不做】

"全仓不得出现旁路"的总闸门留给最后一条路径迁完再加。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from agent_core.actions import ActionGateway
from agent_core.contracts import ActionObservation, ActionRequest, Scope
from agent_core.local_worker_provider import LocalWorkerProvider
from agent_core.tools import ToolRegistry
from agent_core.work_provider import WorkRequest

#: 只有这五个函数是"真的动了东西"的那一下。它们只能从动作处理函数里被调到。
_EFFECTORS = frozenset({
    "self.files.list_files",
    "self.files.preview_sort_by_extension",
    "self.files.apply_moves",
    "self._screenshot_capture",
    "self._volume_adjust",
})

_SOURCE = Path(__file__).resolve().parents[1] / "agent_core" / "local_worker_provider.py"


# ── 替身 ────────────────────────────────────────────────────────────


class RecordingGateway:
    """只记录、不真执行的网关替身。

    跟 `test_execution_paths.RecordingGateway` 一个路子：这里要测的是
    "Worker 有没有走网关"，不是"网关自己做得对不对"，所以不让真网关掺和。
    """

    def __init__(self) -> None:
        self.calls: list[ActionRequest] = []

    async def invoke(self, request: ActionRequest) -> ActionObservation:
        self.calls.append(request)
        return ActionObservation(action_id=request.action_id, name=request.name, status="completed", output={})


def _request(task_type: str, **overrides: Any) -> WorkRequest:
    scope = Scope("owner", "desktop", "chat", "work")
    base: dict[str, Any] = {
        "work_item_id": "w-1",
        "attempt_id": "a-1",
        "scope": scope,
        "objective": "test",
        "task_type": task_type,
    }
    base.update(overrides)
    return WorkRequest(**base)


def _emit(_event: Any) -> None:
    """同步的事件收集器。`_emit` 自己会处理"不需要 await"的情况。"""


def _worker(tmp_path: Path, **kwargs: Any) -> tuple[LocalWorkerProvider, dict[str, Any]]:
    """构造一个带假 effector 的 worker，返回 (worker, 调用记录)。"""
    calls: dict[str, Any] = {"capture": 0, "volume": []}

    def capture() -> bytes:
        calls["capture"] += 1
        return b"png-bytes"

    def volume(percent: float) -> dict[str, Any]:
        calls["volume"].append(percent)
        return {"status": "ok", "percent": percent}

    worker = LocalWorkerProvider(
        allowed_roots=[tmp_path],
        capture_fn=capture,
        volume_adjust=volume,
        artifact_dir=tmp_path / "artifacts",
        **kwargs,
    )
    return worker, calls


async def _run(worker: LocalWorkerProvider, request: WorkRequest) -> Any:
    return await worker.execute(request, _emit)


# ── 1. 没有网关就没有任何副作用 ─────────────────────────────────────


async def test_without_registered_actions_no_real_effect_happens(tmp_path: Path) -> None:
    """网关不认识这些动作时，四种任务全失败，且注入的 effector 一次都没被碰到。

    这是"没有旁路"的运行时证明：只要 worker 里还留着一条自己直接调用的线，
    假截屏/假音量就会被触发，这条立刻变红。
    """
    empty = ActionGateway.standalone(ToolRegistry())
    worker, calls = _worker(tmp_path, gateway=empty)

    # 目录里放一个文件，好让整理任务有东西可动。
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")

    results = {
        "file.list": await _run(worker, _request("file.list", input={"directory": str(tmp_path)})),
        "file.organize": await _run(worker, _request("file.organize", input={"directory": str(tmp_path)})),
        "screen.capture": await _run(worker, _request("screen.capture")),
        "system.volume.adjust": await _run(worker, _request("system.volume.adjust", input={"percent": 20})),
    }

    for task_type, result in results.items():
        assert result.status == "failed", f"{task_type} 居然成功了：{result.status} / {result.error}"
    assert calls["capture"] == 0, "截屏被绕过网关直接触发了"
    assert calls["volume"] == [], "音量被绕过网关直接调整了"
    assert target.exists(), "文件被动过了"


# ── 2. 每个真实动作都要带 work_host 来源 ────────────────────────────


async def test_every_task_type_dispatches_through_the_gateway(tmp_path: Path) -> None:
    """四种任务都真的发起了网关调用，且来源是 `work_host`。"""
    gateway = RecordingGateway()
    worker, _ = _worker(tmp_path, gateway=gateway)

    await _run(worker, _request("file.list", input={"directory": str(tmp_path)}))
    await _run(worker, _request("file.organize", input={"directory": str(tmp_path)}))
    await _run(worker, _request("file.organize", input={"directory": str(tmp_path)},
                                approval_manifest=({"source": "a", "target": "b"},), approval_granted=True))
    await _run(worker, _request("screen.capture"))
    await _run(worker, _request("system.volume.adjust", input={"percent": 20}))

    assert gateway.calls, "一个网关调用都没发出 —— 任务还在自己执行"
    assert {call.origin for call in gateway.calls} == {"work_host"}, [
        call.origin for call in gateway.calls
    ]
    # 动作编号要带上 attempt_id：同一次尝试里的同一个动作只该被登记一次。
    assert all(call.action_id.startswith("work-a-1-") for call in gateway.calls)


# ── 3. 源码静态守门 ─────────────────────────────────────────────────


def _dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def test_effectors_are_only_called_from_action_handlers() -> None:
    """那五个 effector 只允许出现在 `_act_*` 里。

    判据是"读了这个名字"，不是"把它当函数调用了"。只看调用禁不住这一类：

        await asyncio.to_thread(self.files.apply_moves, effective)

    这里 effector 在**参数位**上，`asyncio.to_thread` 才是被调的那个 ——
    只扫 `Call.func` 的写法会把整段绕过网关的代码判成干净的。

    反过来，`__init__` 里的 `self._screenshot_capture = ...` 是**写**
    （`ctx=Store`），不是读，所以不算违规 —— 判据只看读，两边都不误伤。
    """
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))

    owner: dict[ast.AST, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                owner[child] = node.name

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or not isinstance(node.ctx, ast.Load):
            continue
        name = _dotted(node)
        if name is None or name not in _EFFECTORS:
            continue
        holder = owner.get(node, "<module>")
        if not holder.startswith("_act_"):
            offenders.append(f"{holder}:{getattr(node, 'lineno', '?')} -> {name}")

    assert offenders == [], "effector 被动作处理函数以外的地方读到了：\n" + "\n".join(offenders)


def test_internal_actions_refuse_any_origin_but_the_host(tmp_path: Path) -> None:
    """内部动作声明了来源限制：不是 `work_host` 发起的一律跑不起来。"""
    worker, calls = _worker(tmp_path)
    specs = worker.internal_action_specs()

    assert specs, "worker 没有声明任何内部动作"
    for spec in specs:
        assert spec.allowed_origins == frozenset({"work_host"}), f"{spec.name}: {spec.allowed_origins}"
        assert spec.visibility == "internal", f"{spec.name}: {spec.visibility}"
    # 声明归声明，真的用 chat 来源打一次也必须被挡住。
    from agent_core.contracts import ActionRequest

    async def probe() -> Any:
        return await worker.gateway.invoke(
            ActionRequest(
                action_id="probe-1",
                name=specs[0].name,
                arguments={},
                scope=Scope("owner", "desktop", "chat", "work"),
                origin="chat_model",
            )
        )

    import asyncio

    observation = asyncio.run(probe())
    assert observation.status == "rejected", observation.status
    assert calls["capture"] == 0
