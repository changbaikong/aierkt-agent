"""ActionGateway 的行为契约。

【这个文件守的是什么】

网关是**唯一**能把手伸进 `ToolSpec.handler` 的地方。它一个人负责七件事，
顺序即正确性：

    ① 原子登记动作编号（重复编号不得重跑）
    ② 来源限制（origin vs spec.allowed_origins）
    ③ 参数校验（JSON Schema）
    ④ 结构校验（必填字符串不能全是空白；跨字段关系由工具交出回调）
    ⑤ 权限三态（allow / confirm / deny）
    ⑥ 低风险持久动作的独立一致性复核（persistent + risk=low 那一格）
    ⑦ 执行 + 超时 + 异常边界 + 结果归一化

每一条的失败都发生在"执行之前"，这是刻意的：持久动作跑出去就留下东西了。

【为什么每一步都要单独一条用例】

这些步骤里任何一步被跳过，症状都是"看起来正常但实际有洞"：
- 少了 ①：用户重复点确认，副作用发生两次；
- 少了 ②：模型伪造内部动作（如 Work 的内部动作）就能跑；
- 少了 ③：handler 收到畸形参数才崩，错误信息指向 handler 而不是调用方；
- 少了 ④：`"   "` 这种"有长度但没内容"的参数一路跑到持久动作里；
- 少了 ⑤：高风险动作直接执行；
- 少了 ⑥：低风险持久动作没人看"这次调用说清了要干什么"；
- 少了 ⑦：一个 handler 卡死拖垮整个回合，或返回词表外的状态污染下游。

写这个文件时 `agent_core/actions.py` 还不存在（或只有骨架），
所以它一开始是红的 —— 这就是本轮的"先红"。
"""

from __future__ import annotations

import asyncio

import pytest

from agent_core.contracts import (
    ACTION_STATUS_INVALID,
    EFFECT_PERSISTENT,
    EFFECT_READ,
    ActionRequest,
    Scope,
    ToolSpec,
)
from agent_core.tools import DefaultRiskPolicy, ToolRegistry


def _spec(
    name: str = "demo__ping",
    handler=None,
    **overrides,
) -> ToolSpec:
    """造一个最小可用工具。参数校验用真实 schema，不走简化路径。"""
    async def default_handler(arguments, scope):
        return {"status": "ok", "output": {"echo": arguments.get("value")}}

    schema = overrides.pop("schema", {
        "type": "function",
        "function": {
            "name": name,
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        },
    })
    return ToolSpec(
        name=name,
        schema=schema,
        handler=handler or default_handler,
        **overrides,
    )


def _gateway(*specs: ToolSpec, policy=None, journal=None, approval_sink=None, **kwargs):
    """造一个网关。

    **默认把账本和审批落库都接上**，理由不是"方便"，而是这两件事是网关
    正经职责的一部分 —— 不接的话，本文件里 ①（防重放）和 ④（等审批）
    两条契约就永远走不到真实分支，测试变成在验证一个残缺的网关。
    需要显式验证"没接账本时怎么办"的用例，自己传 `journal=None` 是区分不开的，
    那种用例请直接调 `ActionGateway(...)`。
    """
    from agent_core.actions import ActionGateway

    registry = ToolRegistry()
    for spec in specs:
        registry.register(spec)
    return ActionGateway(
        registry=registry,
        risk_policy=policy or DefaultRiskPolicy(),
        journal=journal,
        approval_sink=approval_sink,
        **kwargs,
    )


def _journal(tmp_path) -> "object":
    """一个接了临时目录的账本。每个用例自己 new 一个，天然隔离。"""
    from agent_core.action_journal import ActionJournal

    return ActionJournal(tmp_path / "actions.db")


def _approval_sink():
    """最小审批落库桩：只回答"审批编号是多少"，编号按调用次数递增。"""
    issued: list[str] = []

    def sink(request, spec) -> str:
        issued.append(request.action_id)
        return f"appr-{len(issued)}"

    sink.issued = issued  # type: ignore[attr-defined]
    return sink


def _request(name: str = "demo__ping", **overrides) -> ActionRequest:
    base = dict(
        action_id="act-1",
        name=name,
        arguments={"value": "hi"},
        scope=Scope("u1", "web", "c1", "companion"),
        origin="chat_model",
    )
    base.update(overrides)
    return ActionRequest(**base)


# ──────────────────────── ① 原子登记：重复编号不得重跑 ────────────────────────


async def test_same_action_id_is_not_executed_twice(tmp_path) -> None:
    """同一个 action_id 第二次进来，必须返回既有终态，**不得再跑一遍 handler**。

    这是"用户重复点确认"防重放的第一道锁，也是审批恢复的基础。

    这里刻意不断言 `second.output == first.output`：审计表**不存返回内容**
    （只存字段名与终态，因为参数值里有本机路径、坐标、用户原话）。
    回放拿不回的产物必须是**明说的**，所以断言的是 `replayed=True` +
    空 output，而不是假装成功。
    """
    calls: list[dict] = []

    async def counting(arguments, scope):
        calls.append(dict(arguments))
        return {"status": "ok", "output": {"n": len(calls)}}

    gateway = _gateway(_spec(handler=counting), journal=_journal(tmp_path))
    first = await gateway.invoke(_request())
    second = await gateway.invoke(_request())

    assert len(calls) == 1, f"handler 被跑了 {len(calls)} 次，重复编号必须复用既有结果"
    assert first.status == "completed"
    assert first.replayed is False, "第一次是真跑的，不是回放"
    assert first.output == {"n": 1}

    assert second.status == "completed", "重复编号要回既有终态，不能凭空失败"
    assert second.replayed is True, "必须标出这是读到的历史终态"
    assert second.output == {}, "审计表不存产物，回放拿不回内容就要明说，不能伪造"


# ──────────────────────── ② 来源限制 ────────────────────────


async def test_origin_outside_allowed_origins_is_rejected() -> None:
    """不在 allowed_origins 里的来源必须被拒，且**不得调用 handler**。"""
    called = []

    async def should_not_run(arguments, scope):
        called.append(True)
        return {"status": "ok"}

    gateway = _gateway(_spec(
        handler=should_not_run,
        allowed_origins=frozenset({"work_host"}),
    ))
    observation = await gateway.invoke(_request(origin="chat_model"))

    assert observation.status == "rejected"
    assert called == [], "来源不合法时 handler 绝不能被调用"


async def test_origin_inside_allowed_origins_is_allowed() -> None:
    """来源在名单里就放行（否则来源限制会把合法链路也堵死）。"""
    gateway = _gateway(_spec(allowed_origins=frozenset({"work_host"})))
    observation = await gateway.invoke(_request(origin="work_host"))
    assert observation.status == "completed"


async def test_no_allowed_origins_means_any_registered_origin() -> None:
    """不声明 allowed_origins 表示不限制来源。"""
    gateway = _gateway(_spec())
    for origin in ("chat_model", "scheduler", "api"):
        observation = await gateway.invoke(_request(action_id=f"act-{origin}", origin=origin))
        assert observation.status == "completed", origin


# ──────────────────────── ③ 参数校验 ────────────────────────


async def test_missing_required_argument_is_rejected_before_handler() -> None:
    """缺必填参数：网关就必须拒，不能让 handler 收到畸形参数才崩。

    错误信息要指向调用方（参数问题），不是 handler 内部。
    """
    called = []

    async def should_not_run(arguments, scope):
        called.append(True)
        return {"status": "ok"}

    gateway = _gateway(_spec(handler=should_not_run))
    observation = await gateway.invoke(_request(arguments={}))

    assert observation.status == "rejected"
    assert observation.error_kind == "permanent"
    assert called == []
    # 错误里要能看出是哪个字段缺了
    assert "value" in observation.error


async def test_wrong_argument_type_is_rejected() -> None:
    gateway = _gateway(_spec())
    observation = await gateway.invoke(_request(arguments={"value": 123}))
    assert observation.status == "rejected"
    assert observation.error_kind == "permanent"


async def test_unknown_argument_is_rejected_when_schema_forbids_it() -> None:
    """schema 声明了 additionalProperties=False 时，多余字段必须被拒。

    这条防的是"猜字段名绕过"：work_host 那边有一整套别名名单，
    这里是通用层的第一道。
    """
    gateway = _gateway(_spec())
    observation = await gateway.invoke(_request(arguments={"value": "hi", "approval_granted": True}))
    assert observation.status == "rejected"


# ──────────────────────── ④ 权限三态 ────────────────────────


async def test_high_risk_action_waits_for_approval_and_does_not_execute(tmp_path) -> None:
    """高风险动作：落等待审批，**不执行**。"""
    called = []

    async def should_not_run(arguments, scope):
        called.append(True)
        return {"status": "ok"}

    gateway = _gateway(
        _spec(handler=should_not_run, risk="high"),
        journal=_journal(tmp_path),
        approval_sink=_approval_sink(),
    )
    observation = await gateway.invoke(_request())

    assert observation.status == "waiting_approval"
    assert observation.approval_id, "等待审批必须带回审批编号，否则无法恢复"
    assert called == [], "等审批的动作绝不能先执行"


async def test_waiting_approval_without_a_sink_is_rejected_not_executed(tmp_path) -> None:
    """没有审批落库时，高风险动作只能被拒 —— 不能"没地方记就干脆跑了"。

    "等待审批"必须有一个可恢复的审批编号；连编号都发不出来时，
    正确做法是拒绝，而不是降级成直接执行。
    """
    called = []

    async def should_not_run(arguments, scope):
        called.append(True)
        return {"status": "ok"}

    gateway = _gateway(_spec(handler=should_not_run, risk="high"), journal=_journal(tmp_path))
    observation = await gateway.invoke(_request())

    assert observation.status == "rejected"
    assert observation.error_kind == "permission"
    assert called == []


async def test_denied_action_is_rejected_without_asking() -> None:
    """被拒的动作直接拒绝，连"要不要执行"都不问。"""
    called = []

    async def should_not_run(arguments, scope):
        called.append(True)
        return {"status": "ok"}

    gateway = _gateway(_spec(handler=should_not_run, risk="high"))
    # 匿名身份 + 高风险 → 默认策略判 deny
    observation = await gateway.invoke(_request(scope=Scope("anonymous:x", "web", "c1", "companion")))

    assert observation.status == "rejected"
    assert called == []


async def test_unknown_action_is_failed_not_raised() -> None:
    """未知动作名：返回失败观察，不抛异常（抛出去会打断整个回合）。"""
    gateway = _gateway(_spec())
    observation = await gateway.invoke(_request(name="nope__missing", action_id="act-x"))
    assert observation.status == "failed"
    assert observation.error_kind == "permanent"


# ──────────────────────── ⑤ 超时 / 异常 / 归一化 ────────────────────────


async def test_readonly_timeout_is_failed_timeout() -> None:
    """只读动作超时：failed + timeout（可能只是慢，重试有救）。"""
    async def slow(arguments, scope):
        await asyncio.sleep(5)
        return {"status": "ok"}

    gateway = _gateway(_spec(handler=slow, effect=EFFECT_READ, timeout_seconds=0.05))
    observation = await gateway.invoke(_request())

    assert observation.status == "failed"
    assert observation.error_kind == "timeout"


async def test_non_readonly_timeout_is_outcome_unknown_and_not_replayable() -> None:
    """**非只读**动作超时：outcome_unknown。

    含义必须收紧（酥主 2026-09-21 拍板）：它不表示"终止了线程"，
    只表示"系统无法确认副作用最终状态"。线程可能还在跑，
    所以**禁止自动重放**。
    """
    async def slow_side_effect(arguments, scope):
        await asyncio.sleep(5)
        return {"status": "ok"}

    gateway = _gateway(_spec(handler=slow_side_effect, effect=EFFECT_PERSISTENT, timeout_seconds=0.05))
    observation = await gateway.invoke(_request())

    assert observation.status == "outcome_unknown"
    assert observation.error_kind == "indeterminate"
    assert observation.retryable is False, "结果未知的副作用绝不能自动重试"


async def test_handler_exception_becomes_failed_not_raised() -> None:
    """handler 抛异常：转成失败观察，异常不冒泡打断回合。"""
    async def boom(arguments, scope):
        raise RuntimeError("handler exploded")

    gateway = _gateway(_spec(handler=boom))
    observation = await gateway.invoke(_request())

    assert observation.status == "failed"
    assert observation.error_kind == "permanent"
    assert "RuntimeError" in observation.error


async def test_sync_handler_runs_without_blocking_the_event_loop() -> None:
    """同步 handler 必须丢线程池，不能占住事件循环。

    判据用**心跳**而不是总耗时：起一个每 20ms 醒一次的任务，
    断言最坏间隔 <100ms。耗时受调度抖动影响，心跳停摆是二值事实。
    """
    import time

    def blocking(arguments, scope):
        time.sleep(0.4)
        return {"status": "ok", "output": {"done": True}}

    gateway = _gateway(_spec(handler=blocking, effect=EFFECT_READ, timeout_seconds=5))

    gaps: list[float] = []
    stop = asyncio.Event()

    async def heartbeat() -> None:
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.02)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    beat = asyncio.create_task(heartbeat())
    try:
        observation = await gateway.invoke(_request())
    finally:
        stop.set()
        await beat

    assert observation.status == "completed"
    assert gaps, "心跳一次都没醒，说明事件循环被占住了"
    assert max(gaps) < 0.1, f"事件循环被阻塞了 {max(gaps):.3f}s（同步 handler 没有丢线程池）"


# ──────────────────────── 状态归一化 ────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ok", "completed"),
        ("completed", "completed"),
        ("failed", "failed"),
        ("rejected", "rejected"),
        ("not_executed", "rejected"),
        ("paused", "waiting_approval"),
        ("waiting_approval", "waiting_approval"),
        ("cancelled", "cancelled"),
        ("interrupted", "outcome_unknown"),
    ],
)
def test_status_words_are_normalised(raw: str, expected: str) -> None:
    """各家的状态词必须映射到同一套规范词（前端只认这一套）。"""
    from agent_core.contracts import normalise_action_status

    status, code = normalise_action_status(raw)
    assert status == expected
    assert code == ""


def test_unknown_status_word_degrades_to_failed() -> None:
    """词表外的状态词不许流到下游，降级成 failed + 明确错误码。"""
    from agent_core.contracts import normalise_action_status

    status, code = normalise_action_status("something_new")
    assert status == "failed"
    assert code == ACTION_STATUS_INVALID


async def test_handler_returning_module_status_word_is_normalised() -> None:
    """handler 返回 media 那种 `completed`，观察结果必须是同一个规范词。"""
    async def module_like(arguments, scope):
        return {"status": "completed", "output": {"artifact": "a1"}}

    gateway = _gateway(_spec(handler=module_like))
    observation = await gateway.invoke(_request())
    assert observation.status == "completed"


async def test_handler_returning_unknown_status_is_marked_invalid() -> None:
    """handler 返回词表外的状态 → failed + invalid_action_status（不静默当成功）。"""
    async def weird(arguments, scope):
        return {"status": "mostly_fine", "output": {}}

    gateway = _gateway(_spec(handler=weird))
    observation = await gateway.invoke(_request())
    assert observation.status == "failed"
    assert observation.error_kind == "permanent"
    assert observation.error == ACTION_STATUS_INVALID or ACTION_STATUS_INVALID in observation.error


# ──────────────────────── 观察结果转模型消息（单向） ────────────────────────


async def test_observation_converts_to_tool_result_for_the_model() -> None:
    """观察结果可以适配成 ToolResult 回填模型；这是单向映射。"""
    gateway = _gateway(_spec())
    observation = await gateway.invoke(_request())
    result = observation.as_tool_result()

    assert result.status == "completed"
    assert result.output["echo"] == "hi"


async def test_action_request_rejects_unauthorised_origin() -> None:
    """来源必须在登记表里：拼错的来源会让来源限制静默失效。"""
    with pytest.raises(ValueError):
        _request(origin="chat_modle")


# ──────────────────────── ④ 结构校验：schema 判不了的两件事 ────────────────────────
#
# JSON Schema 只看类型、以及"字段在不在"。有两件事它管不着，于是由这一步补：
#
#   1. **必填字符串不能全是空白。** `"   "` 满足 `type: string`，加个 `minLength: 1`
#      也照样过 —— 它有长度，只是没有内容。于是 `required` 的承诺其实弱得多：
#      它保证"键在"，不保证"说清了"。这一条对所有 `required` 里的字符串字段统一生效，
#      不需要每个工具各写一遍。
#   2. **跨字段关系。** "提交 file.organize 必须带 directory"这种话没法用 JSON Schema
#      表达（它是条件式的，而且要认识任务类型的含义）。这一步给工具一个可选回调，
#      由工具自己在注册时交出来。


def _required_text_spec(**overrides) -> ToolSpec:
    """一个"必填字符串"的最小工具，schema 与 `media.submit_tts` 同形。"""
    schema = {
        "type": "function",
        "function": {
            "name": "demo__ping",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string", "minLength": 1}},
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    }
    return _spec(schema=schema, **overrides)


async def test_blank_required_string_is_rejected_before_execution() -> None:
    """纯空白的必填字符串等于没写，且必须**在 handler 之前**被拦下。

    "被拒"和"跑完才发现不对"是两件事：这一步守的都是持久动作，
    跑出去就留下东西了。
    """
    calls: list[dict] = []

    async def counting(arguments, scope):
        calls.append(dict(arguments))
        return {"status": "ok", "output": {"ran": True}}

    gateway = _gateway(_required_text_spec(handler=counting))
    observation = await gateway.invoke(_request(arguments={"text": "   "}))

    assert observation.status == "rejected"
    assert observation.error_kind == "permanent"
    assert observation.error == "invalid_arguments: text: blank"
    assert calls == [], "空参数不该跑到 handler 里"


async def test_empty_string_is_caught_by_the_schema_step() -> None:
    """空串更早一步就被 schema 的最小长度拦下，错误同样指向字段而不是整包参数。"""
    gateway = _gateway(_required_text_spec())
    observation = await gateway.invoke(_request(arguments={"text": ""}))

    assert observation.status == "rejected"
    assert observation.error.startswith("invalid_arguments: text")


async def test_blank_optional_string_is_not_rejected() -> None:
    """只查 `required` 里的字符串。

    可选字段给个空白是"没给"，不是"给了坏值"。在结构层拦它，
    会把一批"可选项留空"的正常调用打成拒绝 —— 那是把检查变成了误伤。
    """
    schema = {
        "type": "function",
        "function": {
            "name": "demo__ping",
            "parameters": {
                "type": "object",
                "properties": {
                    "value": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["value"],
                "additionalProperties": False,
            },
        },
    }
    gateway = _gateway(_spec(schema=schema))
    observation = await gateway.invoke(
        _request(arguments={"value": "hi", "note": "   "})
    )

    assert observation.status == "completed"


async def test_argument_validator_can_reject_a_cross_field_violation() -> None:
    """工具交出的回调可以在结构层挡下跨字段问题，且拒因与附加信息原样传出去。"""
    from agent_core.contracts import ToolArgumentViolation

    calls: list[dict] = []

    async def counting(arguments, scope):
        calls.append(dict(arguments))
        return {"status": "ok", "output": {"ran": True}}

    def validator(arguments):
        if not arguments.get("note"):
            return ToolArgumentViolation(
                error="cross_field_missing", output={"missing": ["note"]}
            )
        return None

    gateway = _gateway(_required_text_spec(handler=counting, argument_validator=validator))
    observation = await gateway.invoke(_request(arguments={"text": "写点东西"}))

    assert observation.status == "rejected"
    assert observation.error == "cross_field_missing"
    assert observation.output == {"missing": ["note"]}
    assert calls == []


async def test_argument_validator_passing_lets_the_call_through() -> None:
    """回调返回"没意见"（None）就是放行，不是"拒"。"""
    from agent_core.contracts import ToolArgumentViolation  # noqa: F401  仅说明返回形状

    def validator(arguments):
        return None

    gateway = _gateway(_required_text_spec(argument_validator=validator))
    observation = await gateway.invoke(_request(arguments={"text": "写点东西"}))

    assert observation.status == "completed"


async def test_argument_validator_that_raises_is_reported_as_a_broken_tool() -> None:
    """回调自己炸了 = **工具坏了**，不是调用方给了坏参数。

    两种问题的归属不同：前者要人去修工具，后者要让模型回去问用户。
    混成一个错误码，排查时就会一直往参数上找。
    """
    def validator(arguments):
        raise RuntimeError("validator exploded")

    gateway = _gateway(_required_text_spec(argument_validator=validator))
    observation = await gateway.invoke(_request(arguments={"text": "写点东西"}))

    assert observation.status == "rejected"
    assert observation.error == "argument_validator_failed"


async def test_argument_validator_returning_the_wrong_type_is_also_a_broken_tool() -> None:
    """返回了东西但不是那个类型 —— 一样按"工具坏了"处理，不猜它的意思。"""
    def validator(arguments):
        return "looks fine to me"

    gateway = _gateway(_required_text_spec(argument_validator=validator))
    observation = await gateway.invoke(_request(arguments={"text": "写点东西"}))

    assert observation.status == "rejected"
    assert observation.error == "argument_validator_failed"
