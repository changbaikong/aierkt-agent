"""回复洁净度判定：一条文本是不是"内部过程泄漏"。

【为什么单独成文件】
这四个判定原本是 ``loop.AgentLoop`` 的静态方法，只服务"模型的最终回复"。
现在管家过程消息（见 :mod:`agent_core.steward`）也必须过同一道闸：

    过程句是**模型生成**的，它同样可能吐出工具协议、思考链、JSON、
    或者干脆自称"找到了"。用户的气泡里出现任何一样，都是事故。

抽出来是为了让两处共用**同一份**判定，而不是各写一版、慢慢长歪。

【判定顺序】
从便宜到贵：先扫关键字，再尝试 JSON 解析，最后才是正则形状匹配。
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable


def contains_internal_protocol(text: str) -> bool:
    """判断文本是不是"工具协议泄漏"。

    防的是模型把内部结构当正文说给用户听（也防用户贴假协议来注入）。
    三层判定：
    1. 出现 tool_call_id / tool_calls / function_call 等关键字 → 直接命中；
    2. 整体是个 JSON 对象，且含 status/output/error/role/function/tool_calls 任一键 → 命中；
    3. 出现 `role: tool` / `role: function` 这种形如角色声明的片段 → 命中。
    """
    value = str(text or "").strip()
    lowered = value.lower()
    markers = ("tool_call_id", "tool_calls", "function_call", "arguments_json", "observation")
    if any(marker in lowered for marker in markers):
        return True
    if re.match(r"^\s*\{", value):
        try:
            payload = json.loads(value)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict) and any(
            key in payload for key in ("status", "output", "error", "role", "function", "tool_calls")
        ):
            return True
    if re.search(r"(?:^|[\s\[{])role\s*[:=]\s*[\"']?(?:tool|function)\b", lowered):
        return True
    return False


def looks_like_reasoning_chain(text: str) -> bool:
    """判断是否把思考过程当成正文输出。"""
    value = str(text or "").strip()
    lowered = value.lower()
    if re.match(r"^\s*(?:思考过程|推理过程|分析过程|逐步分析)\s*[:：]", value):
        return True
    if "chain of thought" in lowered:
        return True
    if re.match(
        r"^\s*(?:[*#_`\s]*)(?:my\s+)?(?:response\s+generation\s+process|response\s+to|reaction\s+to|reasoning\s+process|analysis\s+process|thoughts?\s+on\s+responding|thoughts?\s+about|thoughts?)\b",
        lowered,
    ):
        return True
    return False


def looks_like_completion_claim(text: str) -> bool:
    """判断是不是一句没有证据支撑的"已完成"。"""
    value = str(text or "").strip()
    return bool(
        re.fullmatch(
            r"(?:已经完成|已完成|完成了|搞定了|弄好了|好了|任务已完成|已经搞定|已搞定)[。！？!?]*",
            value,
        )
    )


def looks_like_tool_schema(text: str, tool_names: Iterable[str]) -> bool:
    """判断是不是把工具 schema 当正文输出。

    三个条件**同时**成立才算命中：代码块/JSON 外壳 + schema 关键字 + 提到真实工具名。
    少一条都会误伤（比如正常回答里提到"天气"这个工具名）。
    """
    value = str(text or "").strip()
    if not value:
        return False
    lowered = value.lower()
    has_code_block = "```" in value or value.lstrip().startswith("{") or value.lstrip().startswith("[")
    has_schema_markers = any(
        marker in lowered
        for marker in (
            '"parameters"',
            '"description"',
            '"tools"',
            "parameters:",
            "description:",
            "function:",
        )
    )
    mentions_tool_name = any(str(name).lower() in lowered for name in tool_names)
    return bool(has_code_block and has_schema_markers and mentions_tool_name)


def reply_issue(text: str, *, tool_names: Iterable[str] = ()) -> str | None:
    """一条可见文本的问题类型；没问题返回 None。

    返回值与 ``loop.AgentLoop._final_reply_issue`` 一致：
    ``"protocol"`` / ``"reasoning"`` / ``"completion"`` / ``None``。
    """
    value = str(text or "").strip()
    if not value:
        return None
    names = frozenset(str(name) for name in tool_names)
    if contains_internal_protocol(value) or looks_like_tool_schema(value, names):
        return "protocol"
    if looks_like_reasoning_chain(value):
        return "reasoning"
    if looks_like_completion_claim(value):
        return "completion"
    return None


INTERNAL_TERMS = (
    "worker",
    "provider",
    "host",
    "agentloop",
    "tool_call",
    "traceback",
    "exception",
    "pending_confirmation",
    "work_item",
    "attempt_id",
    "plan_id",
    "run_id",
    "step 1",
    "第1步",
    "第一步",
    "步骤",
)


def mentions_internal_terms(text: str, *, extra_terms: Iterable[str] = ()) -> str:
    """文本里出现的第一个内部术语；没有则返回空串。

    ``extra_terms`` 由调用方注入真实工具名（registry 里注册了什么就禁什么），
    这样"工具名泄漏"这条规则不需要硬编码任何工具清单。
    """
    lowered = str(text or "").lower()
    for term in INTERNAL_TERMS:
        if term in lowered:
            return term
    for term in extra_terms:
        name = str(term).strip()
        if name and name.lower() in lowered:
            return name
    return ""


def strip_code_fences(text: str) -> str:
    """去掉代码块围栏与包裹性的括号，避免"```json 可见文本"绕过判定。"""
    value = str(text or "").strip()
    value = re.sub(r"^```[a-zA-Z0-9]*\s*", "", value)
    value = re.sub(r"\s*```$", "", value)
    return value.strip().strip("`").strip()
