"""网页原文读取工具（Jina Reader）：截断、失败上报与参数校验。"""

from __future__ import annotations

import httpx

from agent_tools.web_read import make_web_read_tool


def _tool(handler) -> any:
    return make_web_read_tool(
        base_url="https://r.jina.ai", api_key="", max_chars=100,
        transport=httpx.MockTransport(handler), timeout=5.0,
    )


def test_read_returns_title_and_content():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith("https://r.jina.ai/https://example.com/page")
        return httpx.Response(200, text="Title: 梗百科页面\n\n正文第一段：梁文谷是一条网络热梗。\n" + "补充" * 200)

    result = _tool(handler).handler({"url": "https://example.com/page"}, scope=None)
    assert result["status"] == "ok"
    output = result["output"]
    assert output["fetch_status"] == "truncated"
    assert output["title"] == "梗百科页面"
    assert output["truncated"] is True
    assert len(output["content"]) <= 100


def test_read_failure_reports_reason():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    result = _tool(handler).handler({"url": "https://example.com/private"}, scope=None)
    assert result["status"] == "failed"
    assert "403" in result["error"]
    assert result["output"]["fetch_status"] == "failed"
    assert result["output"]["failure_reason"]


def test_read_rejects_non_http_urls():
    result = _tool(lambda request: httpx.Response(200, text="x")).handler(
        {"url": "file:///C:/Windows/system32/config"}, scope=None,
    )
    assert result["status"] == "failed"
    assert result["error_kind"] == "permanent"
