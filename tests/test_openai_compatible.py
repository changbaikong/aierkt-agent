import asyncio


def test_stream_chunk_aggregation_preserves_content_and_tool_arguments():
    from agent_core_adapters.openai_compatible import OpenAICompatibleProvider

    chunks = [
        {"choices": [{"delta": {"content": "你"}}]},
        {"choices": [{"delta": {"content": "好"}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "web_search", "arguments": '{"query":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"药物"}'}}]}}]},
    ]

    response, deltas = OpenAICompatibleProvider.aggregate_stream_chunks(chunks)
    assert deltas == ["你", "好"]
    assert response.message["content"] == "你好"
    assert response.message["tool_calls"][0]["function"]["arguments"] == '{"query":"药物"}'
    assert OpenAICompatibleProvider.visible_stream_deltas(response, deltas) == []

    text_only, text_deltas = OpenAICompatibleProvider.aggregate_stream_chunks(chunks[:2])
    assert OpenAICompatibleProvider.visible_stream_deltas(text_only, text_deltas) == ["你", "好"]


def test_provider_disables_reasoning_for_user_facing_completion(monkeypatch):
    import httpx
    from agent_core_adapters.openai_compatible import OpenAICompatibleProvider

    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": "简短答案"}}]}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, **kwargs):
            captured.update(kwargs.get("json", {}))
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: FakeClient())
    provider = OpenAICompatibleProvider("https://example.invalid/v1", "key", "model")
    result = asyncio.run(provider.complete([{"role": "user", "content": "回答"}]))

    assert result.message["content"] == "简短答案"
    assert captured["thinking"] == {"type": "disabled"}


def test_provider_can_enable_hidden_reasoning_for_analysis_only(monkeypatch):
    import httpx
    from agent_core_adapters.openai_compatible import OpenAICompatibleProvider

    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "choices": [{"message": {
                    "role": "assistant",
                    "reasoning_content": "内部分析，不应成为可见回复",
                    "content": '{"reply":"完成"}',
                }}],
                "usage": {"completion_tokens": 12},
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, **kwargs):
            captured.update(kwargs.get("json", {}))
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: FakeClient())
    provider = OpenAICompatibleProvider(
        "https://example.invalid/v1", "key", "model", thinking_enabled=True,
    )
    result = asyncio.run(provider.complete([{"role": "user", "content": "分析"}], max_tokens=8192))

    assert captured["thinking"] == {"type": "enabled"}
    assert captured["max_tokens"] == 8192
    assert result.message["content"] == '{"reply":"完成"}'
