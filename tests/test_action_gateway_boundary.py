"""动作网关的边界守门：谁可以执行动作、谁不能。

【这个文件为什么单独存在】

`tests/test_action_gateway.py` 守的是网关**做对事**（顺序、超时、归一化）；
这个文件守的是**别人绕不过它**。

两者分开是因为失效方式不同：
- 做错事 → 表现成"某个用例红了"，一眼能看出是网关的问题；
- 绕过去 → 表现成**什么都没红**，因为绕行者自己也能跑通。
  第二类只有靠"结构性断言"才抓得住。

【当前守的是哪一条】

`ToolExecutor` 从公开面摘掉。它是一个真实的逃避口：条件任务那条链路
一直自己 `ToolExecutor().execute(...)`，完全没有权限判定。
只要它还是"正经公开导出"，下一个写新功能的人就会顺手用它，
网关的封口就永远封不住。

【本轮不守的那条（别误会成忘了）】

"全仓不得出现除网关外的 handler 调用点"这条总闸门**故意留到最后**：
现在还有四个调用点没迁完（loop.py 三处、condition_tasks.py 一处），
现在写上它会长期红灯，让整个测试主干失去可信度 ——
分不清"这是预期的红"还是"这是回归"。等最后一个调用点迁完再加。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_CORE = REPO_ROOT / "agent_core"


def test_tool_executor_is_not_exported_from_the_package() -> None:
    """`from agent_core import ToolExecutor` 必须失败。

    这是给"新代码顺手用老入口"设的护栏：导入不进来，
    写下的那一刻就会报错，而不是等到某天发现条件任务没走权限判定。
    """
    import agent_core

    assert not hasattr(agent_core, "ToolExecutor"), (
        "ToolExecutor 又被导出了。它已降级为内部实现，"
        "执行动作请走 agent_core.actions.ActionGateway。"
    )
    assert "ToolExecutor" not in getattr(agent_core, "__all__", [])


def test_tool_executor_is_not_in_tools_public_names() -> None:
    """`agent_core.tools` 自己的 `__all__` 也不许列它。"""
    from agent_core import tools

    exported = getattr(tools, "__all__", None)
    assert exported is not None, "tools 模块必须显式声明 __all__，否则导出面随手就漏"
    assert "ToolExecutor" not in exported


def test_the_private_name_is_what_remains() -> None:
    """私有名还在（过渡期用），这样 §3 的逐个迁移才验证得动。"""
    from agent_core import tools

    assert hasattr(tools, "_ToolExecutor"), (
        "私有实现不见了。迁移还没做完就删掉它，会让剩下四个调用点无法单独验证。"
    )


def test_action_gateway_is_the_only_thing_that_calls_a_tool_handler() -> None:
    """`agent_core` 里，只有 `actions.py` 允许直接执行 `ToolSpec.handler`。

    静态扫描而不是运行期断言：绕过是**结构**问题，跑一次不一定触发。

    【为什么不能简单地找所有 `.handler(...)`】

    这个名字在项目里被两个毫不相干的机制共用：
    - `ToolSpec.handler` —— 工具的具体实现（**本次要封的**）；
    - `EventBus` 的 `sub.handler(event)` —— 事件订阅回调（`events.py`，
      跟动作执行毫无关系，封它没有意义）。

    按属性名一刀切会把事件总线一起误报。所以判据收紧成"**接收者是工具规格**"：
    属性名必须是 `handler`，且接收者是 `spec` / `self.spec` / 以 `spec` 结尾的名字。
    宁可漏报一个写法怪的调用点，也不要制造假警报 ——
    一条会误报的守门测试很快就会被加 `skip` 绕过，等于没有。

    【范围】
    只扫 `agent_core/`（内核）。`agent_tools/` 写 handler 是本职，
    它们**定义** handler，不该**调用** handler，两种事不该用同一条规则管。
    """
    offenders: list[str] = []
    for path in sorted(AGENT_CORE.rglob("*.py")):
        if path.name in {"actions.py", "tools.py"}:
            # actions.py 是网关本体（唯一合法调用点）；
            # tools.py 里是过渡期的 `_ToolExecutor`（§3 迁完后随调用点一起走）。
            continue
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:  # pragma: no cover - 语法错误该由别的测试抓
            pytest.fail(f"{path} 语法错误：{exc}")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "handler":
                continue
            if _is_tool_spec_receiver(func.value):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")

    assert offenders == [], (
        "内核里出现了网关之外的 ToolSpec.handler 调用点，"
        "绕过了登记/来源限制/参数校验/权限：\n" + "\n".join(offenders)
    )


def _is_tool_spec_receiver(value: ast.expr) -> bool:
    """这个表达式的名字看着像"工具规格"吗？

    接受 `spec` / `self.spec` / `tool_spec` / `resolved_spec` 这类；
    不接受 `sub` / `subscription` 这类（事件总线用的是后者）。
    """
    if isinstance(value, ast.Name):
        return value.id == "spec" or value.id.endswith("_spec")
    if isinstance(value, ast.Attribute):
        return value.attr == "spec" or value.attr.endswith("_spec")
    return False
