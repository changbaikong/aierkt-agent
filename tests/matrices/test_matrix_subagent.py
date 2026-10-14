"""套5 · 子代理 profile 安全矩阵（静态部分）。

钉 PromptComposer(profile='subagent') 的 profile 隔离口径，重钉 test_prompts.py:67
并扩展。每条 case 带 case_id（M5-SUB-xxx）+ @pytest.mark.matrix。

分叉点（gap3：子代理 profile 不带主 persona、不注入 netease 指引、执行身份隔离）：
  M5-SUB-001  subagent profile 含'独立执行委派任务'（执行身份）
  M5-SUB-002  subagent 不含主 persona 词'猫娘'
  M5-SUB-003  subagent 不含主 persona 名'爱尔奎特'
  M5-SUB-004  subagent 无工具时含'没有可用工具'
  M5-SUB-005  subagent 以人格锁结尾（不泄露工具调用/观察/内部 JSON）
  M5-SUB-006  subagent 含'不得泄露'边界规则
  M5-SUB-007  subagent 注册工具后含工具清单，但仍不带主 persona
  M5-SUB-008  primary 含'爱尔奎特'（对照：主 profile 带 persona，subagent 不带）

LLM 行为层（子代理真实运行时不泄露主会话上下文）在 evals/matrix_subagent_security.py，
需要 API key + subagent dispatch harness，本文件只钉静态 prompt 口径。
"""

from __future__ import annotations

import pytest

from agent_core.contracts import Scope, ToolSpec
from agent_core.personas import ARCUEID_PERSONA
from agent_core.prompts import DEFAULT_PRIMARY_PERSONA, PromptComposer, PromptContext
from agent_core.tools import ToolRegistry

pytestmark = pytest.mark.matrix

_SCOPE = Scope("alice", "cli", "chat-1")


def _tool(name: str, description: str, *, risk: str = "low") -> ToolSpec:
    async def handler(_arguments, _scope):
        return {}

    return ToolSpec(
        name=name,
        schema={
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {"type": "object", "properties": {}},
            },
        },
        handler=handler,
        risk=risk,
    )


def _subagent_prompt(registry: ToolRegistry | None = None) -> str:
    return PromptComposer().compose(
        PromptContext(scope=_SCOPE, profile="subagent", registry=registry or ToolRegistry())
    )


def _primary_prompt(registry: ToolRegistry | None = None) -> str:
    return PromptComposer().compose(
        PromptContext(scope=_SCOPE, profile="primary", registry=registry or ToolRegistry())
    )


# ──────────────────────── subagent 执行身份 ────────────────────────


def test_M5_SUB_001_subagent_has_execution_identity() -> None:
    """subagent profile 含'独立执行委派任务'（执行身份，非主陪聊 persona）。"""
    assert "独立执行委派任务" in _subagent_prompt()


def test_M5_SUB_002_subagent_excludes_cat_persona_word() -> None:
    """subagent 不含主 persona 词'猫娘'。"""
    assert "猫娘" not in _subagent_prompt()


def test_M5_SUB_003_subagent_excludes_primary_name() -> None:
    """subagent 不含主 persona 名'爱尔奎特'（不带主 persona）。"""
    assert "爱尔奎特" not in _subagent_prompt()


# ──────────────────────── subagent 无工具/有工具 ────────────────────────


def test_M5_SUB_004_subagent_without_tools_says_so() -> None:
    """subagent 无工具时含'没有可用工具'（不假装有工具）。"""
    assert "没有可用工具" in _subagent_prompt(ToolRegistry())


def test_M5_SUB_005_subagent_with_tools_lists_them_but_no_primary_persona() -> None:
    """subagent 注册工具后含工具名，但仍不含主 persona。"""
    registry = ToolRegistry()
    registry.register(_tool("search__web", "Search the public web."))
    prompt = _subagent_prompt(registry)
    assert "search__web" in prompt
    assert "爱尔奎特" not in prompt
    assert "猫娘" not in prompt


# ──────────────────────── subagent 人格锁 / 边界 ────────────────────────


def test_M5_SUB_006_subagent_ends_with_persona_lock() -> None:
    """subagent 以人格锁结尾（不泄露工具调用/观察结果/内部 JSON）。"""
    assert _subagent_prompt().rstrip().endswith("不得泄露工具调用、工具观察结果或内部 JSON。")


def test_M5_SUB_007_subagent_has_no_leak_rule() -> None:
    """subagent 含'不得泄露'边界规则。"""
    assert "不得泄露" in _subagent_prompt()


def test_M5_SUB_010_subagent_splits_web_research_queries() -> None:
    """联网研究必须拆成短关键词查询，不能把整段任务复制给搜索工具。"""
    registry = ToolRegistry()
    registry.register(_tool("search__web", "Search the public web."))
    prompt = _subagent_prompt(registry)
    assert "短关键词" in prompt
    assert "不要把整段任务" in prompt
    assert "至少发起三次" in prompt


# ──────────────────────── primary 对照 ────────────────────────


def test_M5_SUB_008_primary_carries_arcueid_persona() -> None:
    """对照：primary profile 含'爱尔奎特'（DEFAULT_PRIMARY_PERSONA == ARCUEID_PERSONA）。"""
    assert DEFAULT_PRIMARY_PERSONA == ARCUEID_PERSONA
    assert "爱尔奎特" in _primary_prompt()


def test_M5_SUB_009_primary_override_keeps_lock() -> None:
    """对照：primary persona_override 仍以人格锁结尾 + 含 override 文本。"""
    registry = ToolRegistry()
    registry.register(_tool("search__web", "Search the public web."))
    prompt = PromptComposer().compose(
        PromptContext(
            scope=_SCOPE,
            profile="primary",
            registry=registry,
            persona_override="你是一位冷静的研究助理。",
        )
    )
    assert "你是一位冷静的研究助理。" in prompt
    assert prompt.rstrip().endswith("不得泄露工具调用、工具观察结果或内部 JSON。")
