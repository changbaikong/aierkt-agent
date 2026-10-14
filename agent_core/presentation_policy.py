"""Deterministic presentation policy for the web companion."""

from __future__ import annotations

from typing import Any


HIGH_RISK_TERMS = ("药", "感冒", "饮酒", "喝酒", "医疗", "法律", "法规")


def expression_for_user_message(message: str) -> tuple[str, str]:
    return ("concern", "high_risk") if any(term in str(message) for term in HIGH_RISK_TERMS) else ("thinking", "thinking")


def expression_for_event(event_type: str, data: dict[str, Any] | None = None) -> tuple[str, str]:
    data = data or {}
    if event_type == "chat.started":
        return "thinking", "thinking"
    if event_type == "tool.started":
        return "thinking", "retrieval" if str(data.get("tool_name", "")).endswith("web") else "tool"
    if event_type == "chat.failed":
        return "concern", "failure"
    if event_type == "chat.completed":
        text = str(data.get("reply") or "")
        if any(token in text for token in ("没想到", "居然", "竟然", "吓一跳")):
            return "surprise", "reply"
        if any(token in text for token in ("害羞", "不好意思", "脸红")):
            if not any(neg in text for neg in ("不害羞", "没有害羞", "不不好意思")):
                return "shy", "reply"
        if any(token in text for token in ("有点累", "累了", "困了", "疲惫")):
            return "tired", "reply"
        if any(token in text for token in ("太好了", "恭喜", "开心", "成功")):
            return "joy", "reply"
        if any(token in text for token in ("注意", "风险", "不能确定", "建议就医", "小心")):
            return "concern", "reply"
        return "neutral", "reply"
    return "neutral", "idle"
