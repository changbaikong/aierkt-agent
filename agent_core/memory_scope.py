"""新语义记录的可见范围与授权（B 计划第三步）。

【两种格式并存】
``semantics_version >= 1`` 的记录用 ``scope_kind`` 显式声明范围
（conversation / project / owner）；旧记录没有这一列，继续按老的 ``visibility``
（module / shared）判断。判断按记录自己的版本分流，不去猜，也不把旧记录
当新格式读。

【三个范围的边界】
- conversation：同账号 + 同渠道 + 同会话 + 同模块（最严，也是新记录的默认）
- project：同账号 + 同项目，**可以跨会话**；项目标识只能来自可信运行配置或用户明确动作
- owner：同账号 + 允许的主聊天渠道；不自动开放给场景对话

【为什么授权集中在一处】
读取路径有三条：全文检索筛候选、向量检索筛候选、取到正文后复核。
三条各写一份判断，迟早出现「向量能捞到、SQL 放行不了」或者反过来越权捞到 ——
所以规则只写这里，三个入口都调它。
"""

from __future__ import annotations

import os
from typing import Any, Iterable, Mapping

#: 允许读取账号级（owner）记录的主聊天渠道。场景对话刻意不在其中。
DEFAULT_OWNER_CHANNELS: tuple[str, ...] = ("web", "cli", "desktop")


def owner_channels() -> tuple[str, ...]:
    """账号级记录允许的渠道；用 ``MEMORY_OWNER_CHANNELS`` 覆盖（逗号分隔）。"""
    raw = os.environ.get("MEMORY_OWNER_CHANNELS", "")
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    return values or DEFAULT_OWNER_CHANNELS


def project_binding_from_env() -> str:
    """当前运行配置里的可信项目绑定；没配就是空（等于没有绑定）。"""
    return os.environ.get("AGENT_MEMORY_PROJECT", "").strip()


def is_semantic(row: Mapping[str, Any]) -> bool:
    """是否为新语义记录。旧记录这一列是 0 或 None。"""
    try:
        return int(row.get("semantics_version") or 0) >= 1
    except (TypeError, ValueError):
        return False


def record_scope_kind(row: Mapping[str, Any]) -> str:
    """记录自己声明的范围；旧记录返回 ``"legacy"``。"""
    if not is_semantic(row):
        return "legacy"
    return str(row.get("scope_kind") or "").strip() or "conversation"


def authorize(
    row: Mapping[str, Any],
    scope: Any,
    *,
    project_id: str = "",
    channels: Iterable[str] | None = None,
) -> bool:
    """新语义记录能不能被当前作用域读到。

    调用方要在**取到正文之后**再调一次：SQL 与向量只负责筛候选，
    最终放行以这里为准。第一条就是账号边界 —— 候选筛选那层有没有带
    账号条件，都不影响这里的判断。
    """
    if str(row.get("owner_id") or "") != str(scope.owner_id):
        return False
    kind = record_scope_kind(row)
    if kind == "conversation":
        return _same_conversation(row, scope)
    if kind == "project":
        bound = str(project_id or "").strip()
        # 没有可信绑定就没有项目范围：光凭「项目名一样」不算授权。
        return bool(bound) and str(row.get("project_id") or "").strip() == bound
    if kind == "owner":
        allowed = tuple(channels) if channels is not None else owner_channels()
        return str(row.get("channel")) in allowed and str(scope.channel) in allowed
    return False


def _same_conversation(row: Mapping[str, Any], scope: Any) -> bool:
    return (
        str(row.get("channel")),
        str(row.get("conversation_id")),
        str(row.get("module_id")),
    ) == (
        str(scope.channel),
        str(scope.conversation_id),
        str(scope.module_id),
    )


def sql_filter_new(
    scope: Any,
    *,
    project_id: str = "",
    channels: Iterable[str] | None = None,
    prefix: str = "",
    include_semantic: bool = True,
) -> tuple[str, tuple[Any, ...]]:
    """新格式记录的候选筛选片段（SQL）。调用方自己带账号前缀，
    并把它和旧格式条件用 OR 拼起来。

    ``prefix`` 用来限定表名（多表 JOIN 时必须给），列名不做字符串替换 ——
    替换会在出现同名列时把条件改错，静默放宽权限。
    ``include_semantic=False`` 时返回一个恒假条件：召回开关关掉时，
    新记录对检索完全不可见，而不是"部分可见"。

    返回 ``(条件片段, 参数)``。候选出来了还要过 :func:`authorize`。
    """
    if not include_semantic:
        return "(0)", ()
    allowed = tuple(channels) if channels is not None else owner_channels()
    version = f"{prefix}semantics_version"
    scope_kind = f"{prefix}scope_kind"
    channel = f"{prefix}channel"
    conversation = f"{prefix}conversation_id"
    module = f"{prefix}module_id"
    project = f"{prefix}project_id"
    clauses = [
        f"({version}>=1 AND {scope_kind}='conversation' AND {channel}=? AND {conversation}=? AND {module}=?)"
    ]
    args: list[Any] = [scope.channel, scope.conversation_id, scope.module_id]
    bound = str(project_id or "").strip()
    if bound:
        clauses.append(f"({version}>=1 AND {scope_kind}='project' AND {project}=?)")
        args.append(bound)
    if allowed:
        placeholders = ",".join("?" for _ in allowed)
        clauses.append(f"({version}>=1 AND {scope_kind}='owner' AND {channel} IN ({placeholders}))")
        args.extend(allowed)
    return "(" + " OR ".join(clauses) + ")", tuple(args)


def payload_scope_should(
    scope: Any,
    *,
    project_id: str = "",
    channels: Iterable[str] | None = None,
    match: Any,
    match_any: Any,
    must: Any,
    include_semantic: bool = True,
) -> list[Any]:
    """向量检索的候选筛选条件（Qdrant 的 ``should`` 列表）。

    三个构造器由调用方传进来，这个模块因此不反向依赖向量库客户端；每一条
    分支自带账号/渠道约束，避免只按会话或项目过滤时把别的账号捞进来。
    ``include_semantic=False`` 时只留旧格式的两条分支。
    """
    allowed = tuple(channels) if channels is not None else owner_channels()
    should = [
        # 旧格式：同模块（四元组）
        must(
            [
                match("visibility", "module"),
                match("semantics_version", 0),
                match("channel", str(scope.channel)),
                match("conversation_id", str(scope.conversation_id)),
                match("module_id", str(scope.module_id)),
            ]
        ),
        # 旧格式：账号级已确认事实
        must([match("visibility", "shared"), match("channel", str(scope.channel))]),
    ]
    if not include_semantic:
        return should
    should.append(
        # 新格式：会话范围
        must(
            [
                match("scope_kind", "conversation"),
                match("channel", str(scope.channel)),
                match("conversation_id", str(scope.conversation_id)),
                match("module_id", str(scope.module_id)),
            ]
        )
    )
    bound = str(project_id or "").strip()
    if bound:
        should.append(must([match("scope_kind", "project"), match("project_id", bound)]))
    if allowed:
        should.append(must([match("scope_kind", "owner"), match_any("channel", list(allowed))]))
    return should
