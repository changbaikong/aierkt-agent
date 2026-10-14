"""B站搜索与视频详情工具：解析、失败上报与参数校验（mock 传输，不发真实请求）。"""

from __future__ import annotations

import json

import httpx
import pytest

from agent_tools.bilibili import (
    BilibiliWebProvider,
    _extract_bvid,
    make_bilibili_search_tool,
    make_bilibili_video_tool,
)


def _search_response() -> dict:
    return {
        "code": 0,
        "message": "0",
        "data": {
            "result": [
                {
                    "bvid": "BV1xx411c7mD",
                    "title": "【梗百科】梁文谷是什么梗",
                    "description": "一支讲清楚这个梗来源的视频",
                    "author": "梗百科",
                    "pubdate": 1792000000,
                    "play": 123456,
                    "arcurl": "https://www.bilibili.com/video/BV1xx411c7mD",
                },
                {"title": "无链接的条目应当被丢弃"},
            ]
        },
    }


def _mock_client(handler) -> httpx.AsyncBaseTransport:
    return httpx.MockTransport(handler)


def test_search_parses_title_author_and_publish_date():
    async def fake_search_videos():
        provider = BilibiliWebProvider(transport=_mock_client(
            lambda request: httpx.Response(200, json=_search_response())
        ))
        return await provider.search_videos("梁文谷", max_results=5)

    import asyncio
    videos = asyncio.run(fake_search_videos())
    assert len(videos) == 1
    video = videos[0]
    assert video.bvid == "BV1xx411c7mD"
    assert "梁文谷" in video.title
    assert video.author == "梗百科"
    assert video.published_at
    assert video.play_count == 123456


def test_search_tool_reports_platform_and_failure_reason():
    provider = BilibiliWebProvider(transport=_mock_client(
        lambda request: httpx.Response(200, json=_search_response())
    ))
    tool = make_bilibili_search_tool(provider)
    result = tool.handler({"query": "梁文谷"}, scope=None)
    assert result["status"] == "ok"
    output = result["output"]
    assert output["platform"] == "bilibili"
    assert output["result_count"] == 1
    row = output["results"][0]
    assert row["platform"] == "bilibili"
    assert row["published_at"]
    assert row["url"].startswith("https://www.bilibili.com/video/")
    # 渠道检查缺省时不谎报可用性
    assert output["channel_check"] is None


def test_search_failure_keeps_failure_reason_for_degradation():
    provider = BilibiliWebProvider(transport=_mock_client(
        lambda request: httpx.Response(200, json={"code": -412, "message": "请求被拦截"})
    ))
    tool = make_bilibili_search_tool(provider)
    result = tool.handler({"query": "梁文谷"}, scope=None)
    assert result["status"] == "failed"
    assert "-412" in result["error"]
    assert result["output"]["failure_reason"]
    assert result["output"]["platform"] == "bilibili"


def test_search_rejects_invalid_arguments_as_permanent():
    tool = make_bilibili_search_tool(BilibiliWebProvider(transport=_mock_client(
        lambda request: httpx.Response(200, json=_search_response())
    )))
    for arguments in ({}, {"query": "   "}, {"query": "x", "max_results": "many"}):
        result = tool.handler(arguments, scope=None)
        assert result["status"] == "failed"
        assert result["error_kind"] == "permanent"


def test_video_detail_returns_title_and_description():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "bvid=BV1xx411c7mD" in str(request.url)
        return httpx.Response(200, json={
            "code": 0,
            "data": {
                "bvid": "BV1xx411c7mD",
                "title": "【梗百科】梁文谷是什么梗",
                "desc": "视频简介：这个梗出自……",
                "owner": {"name": "梗百科"},
                "pubdate": 1792000000,
                "stat": {"view": 123456},
            },
        })

    tool = make_bilibili_video_tool(BilibiliWebProvider(transport=_mock_client(handler)))
    result = tool.handler({"video": "https://www.bilibili.com/video/BV1xx411c7mD"}, scope=None)
    assert result["status"] == "ok"
    output = result["output"]
    assert output["fetch_status"] == "ok"
    assert "梁文谷" in output["title"]
    assert output["description"].startswith("视频简介")


def test_extract_bvid_accepts_link_and_bare_id():
    assert _extract_bvid("https://www.bilibili.com/video/BV1xx411c7mD?p=1") == "BV1xx411c7mD"
    assert _extract_bvid("BV1xx411c7mD") == "BV1xx411c7mD"
    assert _extract_bvid("https://example.com/nothing") == ""


def test_search_results_are_stripped_of_highlight_tags():
    payload = _search_response()
    payload["data"]["result"][0]["title"] = '<em class="keyword">梁文谷</em>是什么梗'
    payload["data"]["result"][0]["description"] = '<em class="keyword">梗</em>来源讲解'
    provider = BilibiliWebProvider(transport=_mock_client(
        lambda request: httpx.Response(200, json=payload)
    ))
    tool = make_bilibili_search_tool(provider)
    result = tool.handler({"query": "梁文谷"}, scope=None)
    row = result["output"]["results"][0]
    assert "<em" not in row["title"]
    assert row["title"].startswith("梁文谷")
    assert "<em" not in row["snippet"]


# ── bili 命令适配层（Agent-Reach 管理的后端）──────────────────────────
import sys

from agent_tools.bilibili import (
    BilibiliBackendRouter,
    BilibiliCliProvider,
    BilibiliCliUnavailable,
    _bili_argv,
    _run_bili_command,
    build_bilibili_backend,
)

CLI_SEARCH_PAYLOAD = {
    "ok": True,
    "schema_version": "1",
    "data": [
        {"bvid": "BV1dAaW6gEkA", "id": "BV1dAaW6gEkA", "title": "梁文谷时刻",
         "author": "Runnin99", "play": 145809, "duration": "0:31"},
        {"title": "无链接条目应被丢弃"},
    ],
}
CLI_VIDEO_PAYLOAD = {
    "ok": True,
    "data": {
        "video": {
            "bvid": "BV1dAaW6gEkA", "title": "梁文谷时刻", "description": "峰谷价说明",
            "url": "https://www.bilibili.com/video/BV1dAaW6gEkA",
            "owner": {"id": "316211071", "name": "Runnin99"},
        },
    },
}


def test_cli_parse_search_keeps_chinese_and_drops_unusable_rows():
    videos = BilibiliCliProvider.parse_search(CLI_SEARCH_PAYLOAD)
    assert len(videos) == 1
    assert videos[0].title == "梁文谷时刻"
    assert videos[0].author == "Runnin99"
    assert videos[0].bvid == "BV1dAaW6gEkA"
    assert videos[0].url.endswith("BV1dAaW6gEkA")


def test_cli_parse_video_returns_title_and_description():
    video = BilibiliCliProvider.parse_video(CLI_VIDEO_PAYLOAD)
    assert video is not None
    assert video.title == "梁文谷时刻"
    assert video.description == "峰谷价说明"
    assert video.author == "Runnin99"


def test_cli_parse_reports_failure_payload():
    import pytest
    with pytest.raises(RuntimeError, match="bili reported failure"):
        BilibiliCliProvider.parse_search({"ok": False, "message": "请求被风控"})


def test_bili_argv_handles_windows_paths_with_spaces():
    """配置成带空格的绝对路径时按命令行拆分并去引号，不用 shell。"""
    argv = _bili_argv(r'"C:\Users\Test User\.agent-reach-venv\Scripts\bili.exe"', ["search", "--json", "梁文谷"])
    assert argv[0] == r"C:\Users\Test User\.agent-reach-venv\Scripts\bili.exe"
    assert argv[1:] == ["search", "--json", "梁文谷"]
    plain = _bili_argv(r"C:\tools\bili.exe", ["video", "BV1"])
    assert plain == [r"C:\tools\bili.exe", "video", "BV1"]


def test_run_bili_command_handles_chinese_output_and_failures(tmp_path):
    """子进程强制 utf-8：中文标题经管道不乱码；非零退出码如实报错。"""
    script = "import json; print(json.dumps({'ok': True, 'data': [{'title': '梁文谷时刻'}]}, ensure_ascii=False))"
    payload = _run_bili_command(sys.executable, ["-X", "utf8", "-c", script], timeout=30)
    assert payload["data"][0]["title"] == "梁文谷时刻"

    failing = "import sys; sys.stderr.write('被拦截'); sys.exit(1)"
    import pytest
    with pytest.raises(RuntimeError, match="被拦截"):
        _run_bili_command(sys.executable, ["-X", "utf8", "-c", failing], timeout=30)


def test_run_bili_command_classifies_uninstalled_and_timeout():
    import pytest

    with pytest.raises(BilibiliCliUnavailable) as exc_info:
        _run_bili_command("definitely-not-bili-xyz-9137", ["search"], timeout=10)
    assert exc_info.value.reason == "uninstalled"

    sleeper = "import time; time.sleep(5)"
    with pytest.raises(BilibiliCliUnavailable) as exc_info:
        _run_bili_command(sys.executable, ["-X", "utf8", "-c", sleeper], timeout=0.3)
    assert exc_info.value.reason == "timeout"


def test_cli_command_env_config_respected(monkeypatch):
    monkeypatch.setenv("BILI_COMMAND", r"C:\tools\bili.exe")
    provider = BilibiliCliProvider()
    assert provider.command == r"C:\tools\bili.exe"


def _direct_fallback() -> BilibiliWebProvider:
    return BilibiliWebProvider(transport=_mock_client(
        lambda request: httpx.Response(200, json=_search_response())
    ))


def test_router_falls_back_to_direct_api_when_cli_uninstalled():
    """工具未安装 → 降级直连接口，degraded_reason 如实标记。"""
    import asyncio

    router = BilibiliBackendRouter(
        cli_provider=BilibiliCliProvider(command="definitely-not-bili-xyz-9137"),
        fallback=_direct_fallback(),
    )
    videos, meta = asyncio.run(router.search_meta("梁文谷", max_results=5))
    assert meta["backend"] == "web-api-direct"
    assert meta["degraded_reason"] == "uninstalled"
    assert len(videos) == 1


def test_router_marks_timeout_and_unreachable():
    import asyncio

    def timeout_runner(command, arguments, *, timeout):
        raise BilibiliCliUnavailable("timeout", "too slow")

    router = BilibiliBackendRouter(
        cli_provider=BilibiliCliProvider(command="x", runner=timeout_runner),
        fallback=_direct_fallback(),
    )
    _, meta = asyncio.run(router.search_meta("梁文谷", max_results=5))
    assert meta["degraded_reason"] == "timeout"

    def dead_runner(command, arguments, *, timeout):
        raise RuntimeError("bilibili search failed: code=-412")

    router2 = BilibiliBackendRouter(
        cli_provider=BilibiliCliProvider(command="x", runner=dead_runner),
        fallback=_direct_fallback(),
    )
    _, meta2 = asyncio.run(router2.search_meta("梁文谷", max_results=5))
    assert meta2["degraded_reason"].startswith("unreachable")
    assert "-412" in meta2["degraded_reason"]


def test_search_tool_reports_empty_state_without_results():
    """搜索成功但 0 条结果 → fetch_status=empty（无可靠结果），不是失败。"""
    router = BilibiliBackendRouter(
        cli_provider=BilibiliCliProvider(command="x", runner=lambda c, a, *, timeout: {"ok": True, "data": []}),
        fallback=_direct_fallback(),
    )
    tool = make_bilibili_search_tool(router)
    result = tool.handler({"query": "乱码词汇不存在的词"}, scope=None)
    assert result["status"] == "ok"
    assert result["output"]["fetch_status"] == "empty"
    assert result["output"]["backend"] == "bili-cli"
    assert result["output"]["result_count"] == 0


def test_tool_level_fallback_marks_backend_in_output():
    """handler 层输出带 backend 字段：证据来源可追溯，不猜。"""
    router = build_bilibili_backend(
        command="definitely-not-bili-xyz-9137",
        fallback=_direct_fallback(),
    )
    tool = make_bilibili_search_tool(router)
    result = tool.handler({"query": "梁文谷"}, scope=None)
    assert result["status"] == "ok"
    assert result["output"]["backend"] == "web-api-direct"
    assert result["output"]["degraded_reason"] == "uninstalled"
