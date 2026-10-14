"""Local task Provider backed by bounded, structured system services.

【它为什么必须把真实动作交给网关】

这个文件是全仓唯一会真的改本机状态的一段：移动文件、截屏、调系统音量。
在网关之前，那几行长这样::

    result = await asyncio.to_thread(self.files.apply_moves, effective)
    png    = await asyncio.to_thread(self._screenshot_capture)
    result = await asyncio.to_thread(self._volume_adjust, percent)

**没有任何一层问过"这个动作该不该跑"**。Host 的 `_requires_approval` 挡在更外面，
只回答"要不要先问用户一句"；参数合不合法、超时怎么处理、异常怎么归类、
有没有留下记录 —— 全都不归它管。

现在这五下全部注册成了内部动作，只能从 `_act_*` 出发，而且

    tests/test_work_action_paths.py

会把"有没有旁路"钉死：既有一条运行时证明（网关不认识这些动作时，四种任务
必须全部失败且一个副作用都不许发生），也有一条源码扫描（effector 只允许出现在
`_act_*` 里）。

【为什么内部动作没放进全仓那张注册表】

三条理由，每条都对应一个真实的坑：

1. **主注册表建完就冻结。** 能力目录的设计前提是"谁能提供工具在启动时就定死"，
   运行期再往里塞东西就是第二份真相重新长出来。
2. **这些处理函数绑在 worker 实例上。** 允许的根目录、注入的截屏/音量实现都是
   每个实例一份（`LocalWorkerProvider(allowed_roots=..., capture_fn=...)`）。
   塞进全仓共享的表里，等于让整个进程共用某一个实例的配置。
3. **`work__` 前缀是 work 模块的命名空间**，唯一注册来源有守门测试；这里用
   `workhost__` 前缀避开它，顺带表明"这是 Host 自己的东西，不是能力"。

代价说清楚：这个自检网关不带审计账本，所以"同一个动作编号不跑第二次"这条保护
在 Work 这条链上不靠它 —— 靠的是 Host 的 `claim_attempt`（原子的），
它本来就是 Work 那份耐用记录。
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .actions import ActionGateway
from .contracts import (
    EFFECT_PERSISTENT,
    EFFECT_READ,
    EFFECT_TRANSIENT,
    ActionRequest,
    ToolSpec,
)
from .file_work import FileWorkService
from .tools import DefaultRiskPolicy, ToolRegistry
from .work_provider import (
    LOCAL_WORK_TASK_TYPES,
    TaskTypeCapabilities,
    WorkArtifact,
    WorkEvent,
    WorkProviderCapabilities,
    WorkRequest,
    WorkResult,
)


def _default_volume_adjust(percent: float) -> dict[str, Any]:
    from audio_control import adjust_volume

    return adjust_volume(percent)


def _default_screenshot_capture() -> bytes:
    """Capture the primary display as PNG through mss, without shell access."""
    try:
        import mss
        from mss import tools
    except Exception as exc:  # pragma: no cover - depends on local optional package
        raise RuntimeError("screenshot capability unavailable: install mss") from exc

    with mss.mss() as screen:
        monitors = screen.monitors
        if len(monitors) < 2:
            raise RuntimeError("screenshot capability unavailable: no display")
        shot = screen.grab(monitors[1])
        return tools.to_png(shot.rgb, shot.size)


# ── 内部动作声明 ────────────────────────────────────────────────────
#
# 五个动作对应五下"真的动了东西"。比四种任务多一个，是因为 `file.organize`
# 拆成了"预览（读）"与"执行移动（写）"两下 —— 这个拆分正是重点：
# 只有写那一下需要 Host 的审批闸门，预览永远可以先跑给用户看。

ACTION_FILE_LIST = "workhost__file_list"
ACTION_FILE_PREVIEW_SORT = "workhost__file_preview_sort"
ACTION_FILE_APPLY_MOVES = "workhost__file_apply_moves"
ACTION_SCREEN_CAPTURE = "workhost__screen_capture"
ACTION_VOLUME_ADJUST = "workhost__volume_adjust"


def _directory_schema(description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"directory": {"type": "string", "minLength": 1, "description": description}},
        "required": ["directory"],
        "additionalProperties": False,
    }


_INTERNAL_SCHEMAS: dict[str, dict[str, Any]] = {
    ACTION_FILE_LIST: {
        "type": "object",
        "properties": {
            "directory": {"type": "string", "minLength": 1},
            "sort_by": {"type": "string"},
            "descending": {"type": "boolean"},
            "recursive": {"type": "boolean"},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1},
        },
        "required": ["directory"],
        "additionalProperties": False,
    },
    ACTION_FILE_PREVIEW_SORT: _directory_schema("要预览的目录"),
    ACTION_FILE_APPLY_MOVES: {
        "type": "object",
        "properties": {
            "moves": {
                "type": "array",
                "items": {"type": "object"},
            },
        },
        "required": ["moves"],
        "additionalProperties": False,
    },
    ACTION_SCREEN_CAPTURE: {"type": "object", "properties": {}, "additionalProperties": False},
    ACTION_VOLUME_ADJUST: {
        "type": "object",
        "properties": {"percent": {"type": "number"}},
        "required": ["percent"],
        "additionalProperties": False,
    },
}

#: 音量后端用来表示"没成"的那几个词。`FileWorkService` 那边另有自己的 stale，
#: 两边词不一样，所以分开登记 —— 指望它们统一是不切实际的。
_VOLUME_FAILURE_STATUSES = frozenset({"unavailable", "error", "failed"})

#: 非只读动作给长一点：移动一堆文件的耗时没法预判，截到一半不含超时语义。
_TIMEOUTS: dict[str, float] = {
    ACTION_FILE_LIST: 60.0,
    ACTION_FILE_PREVIEW_SORT: 60.0,
    ACTION_FILE_APPLY_MOVES: 180.0,
    ACTION_SCREEN_CAPTURE: 60.0,
    ACTION_VOLUME_ADJUST: 30.0,
}

#: 效果标注决定超时之后怎么说话：read / transient 超时是"失败了可以再试"，
#: persistent 超时是"副作用可能已经发生，不许自动重来"。
#: `screen.capture` 归 transient 而不是 persistent：它新生成一张截图，重来一次
#: 只会多一张，不会破坏任何已有东西。`apply_moves` 才是 persistent —— 它真的
#: 移动了文件，超时之后没人知道移动了多少。
_EFFECTS: dict[str, str] = {
    ACTION_FILE_LIST: EFFECT_READ,
    ACTION_FILE_PREVIEW_SORT: EFFECT_READ,
    ACTION_FILE_APPLY_MOVES: EFFECT_PERSISTENT,
    ACTION_SCREEN_CAPTURE: EFFECT_TRANSIENT,
    ACTION_VOLUME_ADJUST: EFFECT_TRANSIENT,
}


def _spec(name: str, handler: Any) -> ToolSpec:
    """把一个内部动作包成 ToolSpec。

    `risk` 一律是 low —— 不是因为移动文件不危险，而是因为**这一层的授权不归网关管**：
    Work 的审批闸门在 Host（`_requires_approval` / `approval_manifest` / 账本），
    网关没有审批回调，标 high 只会让它被拒。**真正挡住别人的是 `allowed_origins`**：
    除了 `work_host` 谁都叫不动这几个动作，而 `work_host` 这个来源只有
    `LocalWorkerProvider.execute` 会用，它进来之前已经过了 Host 那道闸门。
    """
    return ToolSpec(
        name=name,
        schema={"type": "function", "function": {"name": name, "parameters": _INTERNAL_SCHEMAS[name]}},
        handler=handler,
        risk="low",
        timeout_seconds=_TIMEOUTS[name],
        effect=_EFFECTS[name],
        # 永不进模型清单：模型不该知道"直接移动文件"这个动作存在。
        visibility="internal",
        allowed_origins=frozenset({"work_host"}),
        capability_id="work",
    )


class LocalWorkerProvider:
    """Execute the small set of host-approved local tasks."""

    capabilities = WorkProviderCapabilities(
        "local-worker",
        tuple(sorted(LOCAL_WORK_TASK_TYPES)),
        supports_cancel=True,
        supports_resume=False,
    )

    def __init__(
        self,
        *,
        allowed_roots: list[str | Path],
        screenshot_capture: Callable[[], bytes] | None = None,
        capture_fn: Callable[[], bytes] | None = None,
        volume_adjust: Callable[[float], Mapping[str, Any]] | None = None,
        volume_backend: Callable[[float], Mapping[str, Any]] | None = None,
        artifact_dir: str | Path = "data/work_artifacts",
        artifact_ttl_hours: int = 24,
        artifact_expiry_hours: int | None = None,
        gateway: Any = None,
    ) -> None:
        self.files = FileWorkService(allowed_roots)
        self._cancelled: set[str] = set()
        self._screenshot_capture = screenshot_capture or capture_fn or _default_screenshot_capture
        self._volume_adjust = volume_adjust or volume_backend or _default_volume_adjust
        # 没接到外部注入的网关时自建一个 —— **仍然是同一个网关实现**，五步校验一条不少。
        # 直接构造 worker 的地方（大批单测、`web_api` 的兜底路径）因此也不可能绕过它。
        self.gateway = gateway or ActionGateway.standalone(
            self._internal_registry(), risk_policy=DefaultRiskPolicy()
        )
        self.artifact_dir = Path(artifact_dir)
        self.artifact_ttl_hours = max(
            1,
            int(artifact_expiry_hours if artifact_expiry_hours is not None else artifact_ttl_hours),
        )

    def task_capabilities(self, task_type: str) -> "TaskTypeCapabilities":
        """按 task_type 的能力报告：本地短任务没有快照/恢复/验证链这三样。

        取消确认的依据：``cancel`` 只置标记，每个副作用边界（动作发起前、
        每次移动后）都重查一次；动作一旦发出去，结果由动作网关判
        ``outcome_unknown``，不会把"可能已经动了"报成"没动"。
        """
        owned = task_type in self.capabilities.task_types
        return TaskTypeCapabilities(
            task_type=task_type,
            can_execute=owned,
            supports_cancel=owned,
            cancel_confirmable=owned,
            supports_resume=False,
            resume_has_checkpoint=False,
            emits_events=owned,
            accepts_snapshot=False,
            verifiable_result=owned,
            host_verification_chain=False,
            sandbox_confirmed=False,
            notes=() if owned else ("local worker 只认领本机小任务",),
        )

    # ── 内部动作：五下"真的动了东西" ────────────────────────────────
    #
    # 命名 `_act_*` 不是风格问题：`tests/test_work_action_paths.py` 有一条源码扫描，
    # 那五个 effector 只允许出现在这几段里，改名会让那条测试当场变红。

    def internal_action_specs(self) -> list[ToolSpec]:
        """本 worker 认的内部动作。给外部审查与装配看。"""
        return [
            _spec(ACTION_FILE_LIST, self._act_file_list),
            _spec(ACTION_FILE_PREVIEW_SORT, self._act_file_preview_sort),
            _spec(ACTION_FILE_APPLY_MOVES, self._act_file_apply_moves),
            _spec(ACTION_SCREEN_CAPTURE, self._act_screen_capture),
            _spec(ACTION_VOLUME_ADJUST, self._act_volume_adjust),
        ]

    def _internal_registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        for spec in self.internal_action_specs():
            registry.register(spec)
        return registry

    def _act_file_list(self, arguments: dict[str, Any], _scope: Any) -> dict[str, Any]:
        return self.files.list_files(
            arguments["directory"],
            sort_by=str(arguments.get("sort_by", "name")),
            descending=bool(arguments.get("descending", False)),
            recursive=bool(arguments.get("recursive", False)),
            offset=int(arguments.get("offset", 0)),
            limit=int(arguments.get("limit", 100)),
        )

    def _act_file_preview_sort(self, arguments: dict[str, Any], _scope: Any) -> dict[str, Any]:
        return self.files.preview_sort_by_extension(arguments["directory"])

    def _act_file_apply_moves(self, arguments: dict[str, Any], _scope: Any) -> dict[str, Any]:
        result = self.files.apply_moves([dict(item) for item in arguments["moves"]])
        # `apply_moves` 会在"源文件没了 / 变了"时返回 status=stale。
        # 这个业务词必须**在这里**说清楚，不能让网关去猜：返回值里带 `status`
        # 却没有 `output` 时，网关只能把它当成一整个普通字典（=成功）。
        # `stale` 已登记在业务状态词表里，所以它会原样穿过、落到 failed。
        return {"status": str(result.get("status") or "completed"), "output": result}

    def _act_screen_capture(self, _arguments: dict[str, Any], _scope: Any) -> dict[str, Any]:
        return {"png": self._screenshot_capture()}

    def _act_volume_adjust(self, arguments: dict[str, Any], _scope: Any) -> dict[str, Any]:
        result = dict(self._volume_adjust(float(arguments["percent"])))
        status = str(result.get("status") or "completed")
        if status in _VOLUME_FAILURE_STATUSES:
            # `error` 不在动作状态词表里（词表里只有 failed），原样交回去会被判成
            # "非法状态词"，那也是失败，但原因会被替换成一句没有信息量的话。
            # 后端给的原因（message / reason）才是排障要看的那句。
            return {
                "status": "unavailable" if status == "unavailable" else "failed",
                "output": result,
                "error": str(result.get("message") or result.get("reason") or "volume capability unavailable"),
            }
        return {"status": "completed", "output": result}

    # ── 走网关的两个薄封装 ──────────────────────────────────────────

    async def _invoke(self, request: WorkRequest, name: str, arguments: dict[str, Any]) -> Any:
        """发起一次内部动作。**这是 worker 触碰本机状态的唯一出口。**"""
        return await self.gateway.invoke(
            ActionRequest(
                # 带上 attempt_id 与动作名：同一次尝试里的同一个动作只会被登记一次，
                # 而"预览"与"执行移动"是两个不同动作，编号必须不同。
                action_id=f"work-{request.attempt_id}-{name}",
                name=name,
                arguments=arguments,
                scope=request.scope,
                origin="work_host",
                run_id=request.work_item_id,
                turn_id=request.attempt_id,
            )
        )

    @staticmethod
    def _as_work_result(observation: Any) -> WorkResult:
        """把网关的观察结果翻成 Worker 的结果。

        动作状态比任务状态多，多出来的两个的落点要说清楚：

        - `rejected`（来源不对、参数不合法、被拒）→ 任务 `failed`。
           Work 的结果词表里没有"被拒"这一档，而失败就是对外的那句话。
        - `outcome_unknown`（非只读超时，副作用可能已经发生）→ 任务 `failed`，
          但 error 里带着原因。词表同样没有这一档；把它伪装成 completed 是撒谎，
          所以它落成 failed，由账本把"不知道有没有做完"这件事留在记录里。
        """
        status = str(getattr(observation, "status", "") or "")
        output = dict(getattr(observation, "output", None) or {})
        error = str(getattr(observation, "error", "") or "")
        if status == "completed":
            return WorkResult("completed", output=output)
        if status == "cancelled":
            return WorkResult("cancelled", output=output, error=error or None)
        if status == "waiting_approval":
            return WorkResult("waiting_approval", output=output)
        if status == "outcome_unknown":
            return WorkResult("failed", output=output, error=error or "side effect state unknown")
        return WorkResult("failed", output=output, error=error or None)

    async def cancel(self, attempt_id: str) -> bool:
        self._cancelled.add(attempt_id)
        return True

    async def resume(
        self,
        request: WorkRequest,
        checkpoint: Mapping[str, Any],
        decision: str,
        emit,
    ) -> WorkResult:
        return WorkResult("interrupted", error="local worker does not resume unknown local state")

    def _is_cancelled(self, attempt_id: str) -> bool:
        return attempt_id in self._cancelled

    async def _emit(self, emit, attempt_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
        result = emit(WorkEvent(attempt_id, event_type, dict(payload)))
        if inspect.isawaitable(result):
            await result

    async def execute(self, request: WorkRequest, emit) -> WorkResult:
        if request.task_type not in self.capabilities.task_types:
            return WorkResult("failed", error=f"unsupported task_type: {request.task_type}")
        if self._is_cancelled(request.attempt_id):
            return WorkResult("cancelled", error="cancelled before execution")

        await self._emit(
            emit,
            request.attempt_id,
            "progress",
            {"phase": "planning", "task_type": request.task_type},
        )
        task_input = dict(request.task_input)
        if request.task_type in {"screen.capture", "system.volume.adjust"} and task_input.get("_requires_approval") and not request.approval_granted:
            return WorkResult("waiting_approval", output={"reason": "explicit approval required", "task_type": request.task_type})

        try:
            if request.task_type == "file.list":
                return await self._list_files(request, task_input, emit)
            if request.task_type == "file.organize":
                return await self._organize_files(request, task_input, emit)
            if request.task_type == "screen.capture":
                return await self._capture_screen(request, emit)
            if request.task_type == "system.volume.adjust":
                return await self._adjust_volume(request, task_input, emit)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return WorkResult("failed", error=f"{type(exc).__name__}: {exc}")
        return WorkResult("failed", error=f"unsupported task_type: {request.task_type}")

    async def _list_files(self, request: WorkRequest, task_input: dict[str, Any], emit) -> WorkResult:
        directory = task_input.get("directory") or task_input.get("path")
        if not isinstance(directory, (str, Path)):
            return WorkResult("failed", error="file.list requires input.directory")
        result = self._as_work_result(
            await self._invoke(
                request,
                ACTION_FILE_LIST,
                {
                    "directory": str(directory),
                    "sort_by": str(task_input.get("sort_by", "name")),
                    "descending": bool(task_input.get("descending", False)),
                    "recursive": bool(task_input.get("recursive", False)),
                    "offset": int(task_input.get("offset", 0)),
                    "limit": int(task_input.get("limit", 100)),
                },
            )
        )
        if result.status != "completed":
            return result
        listing = dict(result.output)
        if self._is_cancelled(request.attempt_id):
            return WorkResult("cancelled", output=listing)
        await self._emit(
            emit,
            request.attempt_id,
            "progress",
            {"phase": "listed", "count": listing.get("total", 0)},
        )
        return WorkResult(
            "completed",
            output=listing,
            evidence=[{"kind": "filesystem_listing", "count": listing.get("total", 0)}],
        )

    async def _organize_files(self, request: WorkRequest, task_input: dict[str, Any], emit) -> WorkResult:
        directory = task_input.get("directory") or task_input.get("path")
        if not isinstance(directory, (str, Path)):
            return WorkResult("failed", error="file.organize requires input.directory")
        if request.approval_manifest:
            return await self._apply_move_manifest(request, emit)
        preview = self._as_work_result(
            await self._invoke(request, ACTION_FILE_PREVIEW_SORT, {"directory": str(directory)})
        )
        if preview.status != "completed":
            return preview
        preview_output = dict(preview.output)
        if self._is_cancelled(request.attempt_id):
            return WorkResult("cancelled", output=preview_output)
        if preview_output.get("moves") and request.approval_granted:
            # 完全访问：预览**照生成**（"你要动哪些文件"必须看得见、能追溯），
            # 但不再停下来等人点。接着按**刚生成的这一份**清单执行 ——
            # 不是跳过整理、也不是回头复用某份旧清单，用的就是上面这次
            # preview 的返回值本身，所以"执行的"和"展示的"永远是同一份。
            await self._emit(
                emit,
                request.attempt_id,
                "waiting_approval",
                {"preview": preview_output, "auto_approved": True},
            )
            return await self._apply_move_manifest(request, emit, moves=preview_output["moves"])
        if preview_output.get("moves"):
            await self._emit(
                emit,
                request.attempt_id,
                "waiting_approval",
                {"preview": preview_output},
            )
            return WorkResult("waiting_approval", output=preview_output)
        return WorkResult("completed", output=preview_output)

    async def _apply_move_manifest(
        self,
        request: WorkRequest,
        emit,
        *,
        moves: list[Mapping[str, Any]] | None = None,
    ) -> WorkResult:
        """执行一份具体的移动清单。清单来源只有两个：checkpoint 里人工批准的，
        或本次预览刚生成的（完全访问）。两条路走同一段代码，避免"批准后执行"
        和"自动执行"出现两套行为。"""
        effective = [dict(item) for item in (moves if moves is not None else request.approval_manifest)]
        applied = self._as_work_result(
            await self._invoke(request, ACTION_FILE_APPLY_MOVES, {"moves": effective})
        )
        if applied.status != "completed":
            return applied
        result = dict(applied.output)
        for operation in result.get("applied", []):
            if self._is_cancelled(request.attempt_id):
                return WorkResult("cancelled", output=result)
            await self._emit(emit, request.attempt_id, "file_moved", operation)
        # 到这儿状态已经是"完成"：`stale`（源文件被改过）由 `workhost__file_apply_moves`
        # 在动作层说清楚，任务层不再自己判一遍 —— 两处都判的话，
        # 审计记录与任务结果迟早会出现一次意见不合。
        return WorkResult(
            "completed",
            output=result,
            evidence=[{"kind": "filesystem_move", "count": len(result.get("applied", []))}],
        )

    async def _capture_screen(self, request: WorkRequest, emit) -> WorkResult:
        if self._is_cancelled(request.attempt_id):
            return WorkResult("cancelled", error="cancelled before capture")
        outcome = self._as_work_result(await self._invoke(request, ACTION_SCREEN_CAPTURE, {}))
        if outcome.status != "completed":
            return outcome
        png = outcome.output.get("png")
        if self._is_cancelled(request.attempt_id):
            return WorkResult("cancelled", error="cancelled after capture")
        if not isinstance(png, bytes) or not png:
            return WorkResult("failed", error="screenshot provider returned no PNG data")
        artifact_id = f"{uuid.uuid4().hex}.png"
        target = self.artifact_dir / artifact_id
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, png)
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=self.artifact_ttl_hours)).isoformat(
            timespec="seconds"
        )
        artifact = WorkArtifact(
            artifact_id=artifact_id,
            attempt_id=request.attempt_id,
            media_type="image/png",
            size=len(png),
            path=str(target.resolve()),
            expires_at=expires_at,
        )
        await self._emit(emit, request.attempt_id, "artifact_created", artifact.as_dict())
        return WorkResult(
            "completed",
            output={
                "artifact_id": artifact.artifact_id,
                "artifact_path": artifact.path,
                "path": artifact.path,
                "media_type": artifact.media_type,
                "size": artifact.size,
                "size_bytes": artifact.size,
                "expires_at": artifact.expires_at,
            },
            evidence=[{"kind": "screenshot", "artifact_id": artifact.artifact_id}],
            artifacts=[artifact],
        )

    async def _adjust_volume(self, request: WorkRequest, task_input: dict[str, Any], emit) -> WorkResult:
        if "percent" not in task_input or task_input["percent"] is None:
            return WorkResult("failed", error="system.volume.adjust requires numeric input.percent")
        try:
            percent = float(task_input["percent"])
        except (TypeError, ValueError):
            return WorkResult("failed", error="system.volume.adjust requires numeric input.percent")
        if self._is_cancelled(request.attempt_id):
            return WorkResult("cancelled", error="cancelled before volume adjustment")
        outcome = self._as_work_result(
            await self._invoke(request, ACTION_VOLUME_ADJUST, {"percent": percent})
        )
        if outcome.status != "completed":
            return outcome
        result = dict(outcome.output)
        if self._is_cancelled(request.attempt_id):
            return WorkResult("cancelled", output=result)
        await self._emit(emit, request.attempt_id, "volume_adjusted", result)
        return WorkResult("completed", output=result, evidence=[{"kind": "volume_adjustment", **result}])
