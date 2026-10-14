"""运行时装配：Web 与 CLI 必须构建出**同一个**能力面。

【为什么这条必须守】

Web 与 CLI 是同一个 Agent 的两个入口。用户视角里"换个入口，能力应该一样"，
但代码里它们各自装配了一遍。实测出的分叉：

| 项目 | Web (`web_api._initialize_runtime`) | CLI (`chat_cli`) |
|---|---|---|
| `TaskRouter` 注入 | 有 | **无** |
| 任务库环境变量名 | 直接默认 `"agent_tasks.db"` | 读 `AGENT_TASK_DB` |
| 条件任务库 | 直接默认 | 读 `AGENT_CONDITION_TASK_DB` |

分叉的后果不是"CLI 少个功能"那么轻：**同一个目标在 Web 上会走确定性路由、
在 CLI 上会交给模型**，于是同一个 bug 只在其中一个入口复现，
排障时两边对不上。

【守门方式的选择：不启服务】

不写"起一个 Web 服务再起一个 CLI 比较"的测试 —— 那要端口、要进程、
要清理，而且不稳定。改成：**让两边都只做装配**，比较装配结果。
装配是纯函数式的一步（建目录 → 建 registry → 建 router），不需要绑定端口。

【TaskRouter 为什么不能"装不上就退化成没有"】

它是**本机内核组件**，不是外部可选服务。外部服务（高德 key、MCP）装不上时
降级是对的：没它照样能聊。但 TaskRouter 装不上意味着"确定性路由整条不生效"，
那不是降级，那是**静默换了一套行为**——用户会看到同一个问句在两个入口得到不同处理。
所以装配失败必须**显式报运行时初始化失败**，不许吞掉。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_API = REPO_ROOT / "web_api.py"
CHAT_CLI = REPO_ROOT / "chat_cli.py"


# ── 装配入口必须是同一个 ────────────────────────────────────────────


def test_task_router_failure_surfaces_as_bootstrap_error() -> None:
    """TaskRouter 装配失败必须抛 `RuntimeBootstrapError`，不许退化成"没有路由"。

    上面那条是静态扫描（防"有人把它包进会吞异常的 try"）；
    这条是**实跑**：直接让 TaskRouter 的构造炸掉，确认异常真的冒出来了。

    二者缺一不可：静态扫描抓不到"异常在上游就被吞了"，
    实跑抓不到"下一次有人把它改成静默降级"。
    """
    from unittest import mock

    from agent_core.capability_runtime import RuntimeBootstrapError, build_runtime

    with mock.patch("agent_core.routing.TaskRouter", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeBootstrapError, match="TaskRouter could not be installed"):
            build_runtime()


def test_normal_assembly_produces_a_router() -> None:
    """正常路径下路由器必须存在（否则上一条用例只是"永远都抛"在出汗）。"""
    from agent_core.capability_runtime import build_runtime

    assert build_runtime().task_router is not None


def test_both_entrypoints_use_the_shared_runtime_builder() -> None:
    """Web 与 CLI 都必须调 `capability_runtime` 的同一个构建函数。

    源码级检查是刻意的：运行期比较只能证明"这一次一样"，
    而分叉是**下次有人改一边**造成的。要抓的是"有没有第二份装配代码"。
    """
    from agent_core import capability_runtime

    builder = getattr(capability_runtime, "build_runtime", None)
    assert builder is not None, (
        "capability_runtime.build_runtime 不存在。"
        "Web 与 CLI 的装配必须收敛到这一个函数，否则两边会各长一套。"
    )

    missing: list[str] = []
    for path in (WEB_API, CHAT_CLI):
        source = path.read_text(encoding="utf-8")
        if "capability_runtime" not in source:
            missing.append(path.name)
    assert missing == [], (
        f"这些入口没有走共享装配：{missing}。"
        "各自的装配代码就是分叉的来源（Web 有 TaskRouter、CLI 没有）。"
    )


def test_cli_no_longer_builds_its_own_registry() -> None:
    """CLI 不许再自己 `ModuleRegistry()` + `tools_for()`。

    这两行正是"CLI 有自己的装配"的物证。共享装配之后它们应该消失。
    """
    source = CHAT_CLI.read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", "") or getattr(func, "attr", "")
            if name in {"ModuleRegistry", "tools_for"}:
                offenders.append(f"{CHAT_CLI.name}:{node.lineno} 调了 {name}()")
    assert offenders == [], (
        "CLI 还在自建注册表：\n" + "\n".join(offenders)
        + "\n模块与工具的构建请交给 capability_runtime.build_runtime。"
    )


# ── TaskRouter：两个入口都必须有，且失败必须显式 ──────────────────────


@pytest.mark.parametrize("path", [WEB_API, CHAT_CLI], ids=["web_api", "chat_cli"])
def test_every_entrypoint_installs_task_router(path: Path) -> None:
    """Web 与 CLI 都必须安装 `TaskRouter`。

    现在 CLI 没有 —— 这就是本用例要抓的红。
    """
    source = path.read_text(encoding="utf-8")
    assert "TaskRouter" in source, (
        f"{path.name} 没有安装 TaskRouter。"
        "它是本机内核组件，不是外部可选服务：缺了它，"
        "同一个目标在 Web 与 CLI 会走不同的处理路径。"
    )


def test_task_router_is_not_treated_as_an_optional_degradation() -> None:
    """TaskRouter 的构建失败不许走"记个警告继续跑"那条路。

    判据：`TaskRouter` / `build_task_router` 的构造必须落在某个
    `try` 的**主体**里并且该 `except` 会重新抛出（或根本不包 try）。
    这条防的是有人图省事写 `try: router = TaskRouter(...) except Exception: router = None`——
    那正是"静默换一套行为"。
    """
    offenders: list[str] = []
    for path in (WEB_API, CHAT_CLI):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            constructs_router = any(
                isinstance(inner, ast.Call)
                and (getattr(inner.func, "id", "") or getattr(inner.func, "attr", ""))
                in {"TaskRouter", "build_task_router"}
                for inner in ast.walk(node)
            )
            if not constructs_router:
                continue
            # 如果 except 里只是记日志/降级（没有 raise），就算静默降级。
            for handler in node.handlers:
                raises = any(isinstance(inner, ast.Raise) for inner in ast.walk(handler))
                if not raises:
                    offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], (
        "TaskRouter 的构建被包在会吞掉异常的 try 里：\n" + "\n".join(offenders)
        + "\n它是内核组件，装配失败必须显式报运行时初始化失败，不许静默退化成『没有路由』。"
    )


# ── db 路径口径统一 ────────────────────────────────────────────────


def test_env_overrides_are_a_layer_not_a_replacement() -> None:
    """给 `build_build_context` 传部分 env 时，**其余环境变量必须还在**。

    2026-09-21 修的这个坑：实现曾是 `source = os.environ if env is None else env`，
    于是传 `{"AGENT_TASK_DB": x}` 就把整份环境抹了 ——
    `AGENT_DISABLED_MODULES` 直接失效，症状是"配了禁用却完全不生效"且不报错。

    这条必须守：兼容层 `agent_tools.register_modules` 就是只传两个任务库路径的调用方，
    它恰好踩在这个坑上，而且是**静默**的。
    """
    from agent_core.capability_runtime import ENV_TASK_DB, build_build_context

    ctx = build_build_context(env={
        "AGENT_TASK_DB": "override.db",
        "AGENT_DISABLED_MODULES": "weather",
    })
    assert ctx.task_db == "override.db", "显式覆盖生效"
    assert "weather" in ctx.disabled_modules, (
        "同一批 env 里没显式提到的变量失效了 —— env 被当成替换层而不是覆盖层"
    )


def test_env_is_read_not_replaced_when_nothing_is_overridden() -> None:
    """不传 env 时读真实环境；传空 dict 等于"不覆盖"（不是"没有环境"）。"""
    from agent_core.capability_runtime import build_build_context

    empty = build_build_context(env={})
    from_env = build_build_context()
    assert empty.task_db == from_env.task_db, (
        "传空字典不该把默认值解释成空串"
    )


def test_model_routing_configuration_defaults_to_legacy_shadow(monkeypatch) -> None:
    from agent_core.capability_runtime import build_build_context

    monkeypatch.delenv("AGENT_ROUTING_MODE", raising=False)
    monkeypatch.delenv("AGENT_LEGACY_SHADOW_ENABLED", raising=False)
    context = build_build_context(env={})

    assert context.routing_mode == "legacy"
    assert context.legacy_shadow_enabled is True


def test_model_routing_configuration_accepts_model_mode() -> None:
    from agent_core.capability_runtime import build_build_context

    context = build_build_context(
        env={
            "AGENT_ROUTING_MODE": "model",
            "AGENT_LEGACY_SHADOW_ENABLED": "false",
        }
    )

    assert context.routing_mode == "model"
    assert context.legacy_shadow_enabled is False


def test_invalid_model_routing_mode_fails_runtime_configuration() -> None:
    from agent_core.capability_runtime import RuntimeBootstrapError, build_build_context

    with pytest.raises(RuntimeBootstrapError, match="AGENT_ROUTING_MODE"):
        build_build_context(env={"AGENT_ROUTING_MODE": "automatic"})


def test_invalid_legacy_shadow_value_fails_runtime_configuration() -> None:
    from agent_core.capability_runtime import RuntimeBootstrapError, build_build_context

    with pytest.raises(RuntimeBootstrapError, match="AGENT_LEGACY_SHADOW_ENABLED"):
        build_build_context(env={"AGENT_LEGACY_SHADOW_ENABLED": "sometimes"})


def test_model_mode_without_shadow_does_not_install_legacy_router() -> None:
    from agent_core.capability_runtime import build_runtime

    runtime = build_runtime(
        env={
            "AGENT_ROUTING_MODE": "model",
            "AGENT_LEGACY_SHADOW_ENABLED": "false",
        }
    )

    assert runtime.task_router is None


def test_model_mode_with_shadow_installs_legacy_router_for_observation() -> None:
    from agent_core.capability_runtime import build_runtime

    runtime = build_runtime(
        env={
            "AGENT_ROUTING_MODE": "model",
            "AGENT_LEGACY_SHADOW_ENABLED": "true",
        }
    )

    assert runtime.task_router is not None


def test_entrypoints_do_not_hardcode_task_db_defaults() -> None:
    """两个入口不许各自写一遍任务库的路径默认值。

    默认值只该在共享装配里出现一次。写两遍就会有"改了一边忘了另一边"，
    表现为"Web 上的定时任务在 CLI 里看不见"。
    """
    offenders: list[str] = []
    for path in (WEB_API, CHAT_CLI):
        source = path.read_text(encoding="utf-8")
        for needle in ('"agent_tasks.db"', '"agent_condition_tasks.db"'):
            if needle in source:
                offenders.append(f"{path.name} 里硬编码了 {needle}")
    assert offenders == [], (
        "任务库路径默认值在入口里重复：\n" + "\n".join(offenders)
        + "\n它应该只在 capability_runtime 里出现一次。"
    )
