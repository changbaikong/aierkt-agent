from __future__ import annotations

import json

from agent_tools.agent_reach_router import AgentReachRouter


def test_router_reads_doctor_json_and_selects_only_safe_channels():
    router = AgentReachRouter(runner=lambda _: json.dumps({
        "web": {"active_backend": "jina"},
        "github": {"active_backend": "gh"},
        "twitter": {"active_backend": "twitter-cli"},
    }))
    assert router.available_platforms() == ("github", "web")
    assert router.select(("web", "github", "twitter")) == ("web", "github")


def test_router_failure_is_empty_and_does_not_enable_login_channels():
    router = AgentReachRouter(runner=lambda _: (_ for _ in ()).throw(RuntimeError("missing")))
    assert router.available_platforms() == ()
    assert router.select(("twitter", "reddit")) == ()


def test_router_uses_explicit_command_and_passes_doctor_flag():
    """执行路径允许显式配置：doctor 参数必须跟在配置的命令后面。"""
    seen: list[list[str]] = []

    def runner(argv: list[str]) -> str:
        seen.append(list(argv))
        return json.dumps({"bilibili": {"active_backend": "bili-cli"}})

    router = AgentReachRouter(runner=runner, command="C:/tools/agent-reach.exe")
    assert router.available_platforms() == ("bilibili",)
    assert seen == [["C:/tools/agent-reach.exe", "doctor", "--json"]]


def test_router_command_env_override(monkeypatch):
    monkeypatch.setenv("AGENT_REACH_COMMAND", "python -m agent_reach")
    router = AgentReachRouter(runner=lambda argv: json.dumps({}))
    # 带空格的配置要按命令行拆开（posix=False 保留 Windows 路径反斜杠）。
    assert router._argv()[:3] == ["python", "-m", "agent_reach"]


def test_router_defaults_to_path_lookup_without_config(monkeypatch):
    # 显式置空（而不是 delenv）：项目 .env 已固定 AGENT_REACH_COMMAND，
    # delenv 后 load_dotenv 会把它读回来，测不了"无配置回退默认值"这条路径。
    monkeypatch.setenv("AGENT_REACH_COMMAND", "")
    router = AgentReachRouter(runner=lambda argv: json.dumps({}))
    assert router._argv() == ["agent-reach"]


def test_doctor_decodes_utf8_output_without_using_windows_code_page(monkeypatch):
    """Agent-Reach 的 UTF-8 中文输出不能交给 Windows 默认代码页解码。"""
    from types import SimpleNamespace

    seen: dict[str, object] = {}

    def fake_run(command, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout='{"web":{"active_backend":"jina","message":"中文"}}'.encode("utf-8"),
            stderr=b"",
        )

    monkeypatch.setattr("agent_tools.agent_reach_router.subprocess.run", fake_run)
    raw = AgentReachRouter._run_doctor(["agent-reach", "doctor", "--json"])

    assert json.loads(raw)["web"]["message"] == "中文"
    assert seen["encoding"] == "utf-8"
    assert seen["errors"] == "replace"


def test_cached_select_returns_none_and_refreshes_in_background(monkeypatch):
    """搜索热路径绝不阻塞等 doctor：缓存未知时返回 None 并后台刷新。"""
    import time as _time

    calls: list[list[str]] = []

    def slow_runner(argv: list[str]) -> str:
        calls.append(list(argv))
        _time.sleep(0.3)  # 模拟 doctor 的网络体检耗时
        return json.dumps({"bilibili": {"active_backend": "bili-cli"}})

    router = AgentReachRouter(runner=slow_runner)
    first = router.select_cached(("web", "bilibili"))
    assert first is None, "没有新鲜缓存时必须立即返回 None，不能阻塞"
    assert router.available_platforms_cached() is None
    _time.sleep(0.6)  # 等后台刷新完成
    second = router.select_cached(("web", "bilibili"))
    assert second == ("bilibili",)
    assert len(calls) == 1, "TTL 内只跑一次 doctor"


def test_injected_runner_uses_instance_cache_not_shared():
    """注入 runner 的测试实例互不污染，也 不占用进程级共享缓存。"""
    from agent_tools.agent_reach_router import _SHARED_CACHE

    router_a = AgentReachRouter(runner=lambda _: json.dumps({"web": {"active_backend": "jina"}}))
    router_b = AgentReachRouter(runner=lambda _: json.dumps({}))
    assert router_a.available_platforms() == ("web",)
    assert router_b.available_platforms() == ()
    assert not any("agent-reach" == key for key in _SHARED_CACHE), "注入 runner 不得写共享缓存"
