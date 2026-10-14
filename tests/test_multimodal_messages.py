from __future__ import annotations

from agent_core.contracts import AgentRequest, Scope
from agent_core.loop import AgentLoop


def test_api_history_preserves_multimodal_content():
    content = [
        {"type": "text", "text": "识别图片"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
    ]

    history = AgentLoop._api_history([{"role": "user", "content": content}])

    assert history == [{"role": "user", "content": content}]


def test_agent_request_keeps_attachment_metadata_separate_from_content():
    request = AgentRequest(
        "识别图片",
        scope=Scope("alice", "web", "conversation-1", "companion"),
        content_parts=[{"type": "text", "text": "识别图片"}],
        attachment_metadata=[{"attachment_id": "att-1", "media_type": "image/png"}],
    )

    assert request.content_parts[0]["type"] == "text"
    assert request.attachment_metadata == [{"attachment_id": "att-1", "media_type": "image/png"}]
