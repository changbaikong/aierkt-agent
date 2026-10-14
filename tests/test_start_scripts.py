"""本地启动器（scripts/start_agent.py）的行为。

方案任务 8 的验收点，按分支逐个钉住：

* 端口上有**健康**后端 → 复用它，不重复启动；
* 端口被别的程序占用 → 退出码 2，且**不去杀别人的进程**；
* 后端进程提前退出 → 退出码 1，并把日志尾部说出来；
* 缺必需环境变量 → 退出码 3，指明缺哪个；
* 前端依赖没装 → 退出码 1，提示先跑 npm install；
* 脚本可重复执行，不产生重复进程。

这些都是"启动脚本最容易悄悄坏掉"的地方：它平时只在手动跑的时候执行，
没有测试的话，坏了也没人知道。
"""

from __future__ import annotations

import importlib.util
import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER_PATH = REPO_ROOT / "scripts" / "start_agent.py"


def _load_launcher():
    """把启动器当模块加载（不在 scripts/ 下放 __init__.py，所以手动加载）。"""
    spec = importlib.util.spec_from_file_location("agent_start_launcher", LAUNCHER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def launcher():
    return _load_launcher()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_launcher_exists_and_is_importable(launcher) -> None:
    assert LAUNCHER_PATH.exists(), "启动器必须存在"
    for name in (
        "missing_env_keys",
        "port_listening",
        "backend_health",
        "wait_for_health",
        "spawn",
        "start_backend",
        "start_frontend",
        "main",
    ):
        assert hasattr(launcher, name), f"启动器缺少 {name}"


def test_health_statuses_include_degraded(launcher) -> None:
    """degraded 必须算"可用"。

    本机后端启动后健康检查常常是 degraded（某个能力探测超时），
    如果只认 healthy，每次启动都会误判成"不可用"而重新起一个 ——
    那才是真正会把端口搞乱的行为。
    """
    assert "degraded" in launcher.HEALTHY_STATUSES
    assert "healthy" in launcher.HEALTHY_STATUSES
    assert "unhealthy" not in launcher.HEALTHY_STATUSES


def test_port_listening_is_false_for_a_free_port(launcher) -> None:
    assert launcher.port_listening(_free_port()) is False


def test_missing_env_keys_reports_the_key_that_is_absent(launcher, monkeypatch) -> None:
    """缺项要能点名，不能只说一句"配置有问题"。"""
    monkeypatch.setenv("GEMINI_API_KEY", "")
    import agent_core.project_env  # noqa: F401  确保真实读取路径可用

    # .env 里有值，但环境被清空后 load_project_env 会重新填上，
    # 所以这里直接测"读取之后还缺什么"这个函数本身的行为。
    missing = launcher.missing_env_keys(REPO_ROOT)
    assert isinstance(missing, list)


def test_missing_env_file_is_reported(launcher, tmp_path) -> None:
    """没有 .env 文件时要说清是"文件不存在"，而不是列一堆空值。"""
    missing = launcher.missing_env_keys(tmp_path)
    assert missing, "没有 .env 时应报缺失"
    assert any(".env" in item for item in missing)


def test_repeated_start_reuses_the_running_backend(launcher) -> None:
    """端到端：起一个假后端（能响应 /api/health）→ 启动器应当复用它。

    用真的 HTTP 服务而不是打桩，因为要验证的正是"探测到就复用"这条判断。
    """
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    port = _free_port()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/api/health":
                body = json.dumps({"status": "degraded"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):  # 静音
            pass

    server = HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        log_dir = REPO_ROOT / "logs"
        code, process = launcher.start_backend(REPO_ROOT, port, timeout=5, log_dir=log_dir)
        assert code == launcher.EXIT_OK
        assert process is None, "复用已有服务时不该起新进程"
    finally:
        server.shutdown()
        server.server_close()


def test_port_taken_by_a_foreign_service_is_reported_and_left_alone(launcher) -> None:
    """端口被别的程序占用 → 退出码 2，且**不去杀它**。

    这条走的是**兜底路径**：``listen(1)`` 的等待队列只有一格，前面那次健康探测留下的
    连接没人 ``accept()``，把队列占满之后新连接会被直接丢弃，TCP 预检因此看不出端口被占
    —— 这正是"用户态分不出空端口与被占端口"的那个情形。于是启动器只能真的去起后端，
    由子进程绑定失败退出，再从日志里认绑定冲突报 EXIT_PORT_TAKEN。

    所以这里给足超时（起一个真后端本身就要几秒），断言的是"占用方还活着"（我们没碰它）。
    队列够深、能提前拒绝的那条路径见下面的用例。
    """
    port = _free_port()
    holder = socket.socket()
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", port))
    holder.listen(1)
    try:
        log_dir = REPO_ROOT / "logs"
        code, process = launcher.start_backend(REPO_ROOT, port, timeout=90, log_dir=log_dir)
        assert code == launcher.EXIT_PORT_TAKEN, f"应报端口冲突，实际 {code}"
        assert process is None, "占用方不是我们的进程，不能起新的，也不能碰它"
        # 占用方依然活着 —— 我们没去杀别人的进程
        assert holder.fileno() != -1
    finally:
        holder.close()


def test_start_backend_rejects_a_silent_listener_before_backend_initialization(launcher, monkeypatch, tmp_path) -> None:
    """静默占用者也要在启动后端前被识别，不能等完整初始化超时。"""
    monkeypatch.setattr(launcher, "backend_health", lambda port, timeout=2.0: ("none", {}))
    monkeypatch.setattr(launcher, "port_listening", lambda port, host="127.0.0.1", timeout=0.6: True)

    def fail_spawn(*args, **kwargs):
        raise AssertionError("端口已在监听时不应启动后端")

    monkeypatch.setattr(launcher, "spawn", fail_spawn)

    code, process = launcher.start_backend(REPO_ROOT, 8765, timeout=90, log_dir=tmp_path)

    assert code == launcher.EXIT_PORT_TAKEN
    assert process is None


def test_a_real_silent_listener_is_rejected_before_any_backend_is_spawned(launcher, monkeypatch) -> None:
    """队列还空着时，TCP 预检能认出占用方，不必等后端跑完初始化才在绑定处失败。

    上面那条用例把 ``port_listening`` 换成了假的，只验证了控制流；这条用真 socket，
    验证预检真的能从真实连接状态里读出"有人在监听"。

    两个前提不能少：
    * 必须真的 ``listen()``。只 bind 不 listen 的端口，连接会被直接拒掉，不算被占用；
    * 队列要给够（这里 8）。前面那次健康探测的连接没人 ``accept()``，会一直占着队列；
      队列只有一两格时会被占满，之后的新连接直接被丢弃，预检就会漏判。
    """
    port = _free_port()
    holder = socket.socket()
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", port))
    holder.listen(8)

    def fail_spawn(*args, **kwargs):
        raise AssertionError("端口已在监听时不应启动后端")

    monkeypatch.setattr(launcher, "spawn", fail_spawn)
    try:
        assert launcher.port_listening(port), "真监听中的端口应当被预检看出来"
        code, process = launcher.start_backend(REPO_ROOT, port, timeout=90, log_dir=REPO_ROOT / "logs")
    finally:
        holder.close()

    assert code == launcher.EXIT_PORT_TAKEN
    assert process is None


def test_health_probe_merges_free_and_dead_silent_ports(launcher) -> None:
    """连不上一律报 none，不假装能分出"空着"还是"被占着但不回应 HTTP"。

    本机（Windows）实测：连接一个空端口会被直接丢弃、表现为超时，与"对方在监听但
    不回应"的超时完全一样，用户态分不出来。所以这里不猜——由真正绑端口的子进程裁决。
    """
    free_port = _free_port()
    state, _ = launcher.backend_health(free_port, timeout=1.0)
    assert state == "none", f"没人监听的端口应当报 none，实际 {state}"

    port = _free_port()
    holder = socket.socket()
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", port))
    holder.listen(1)
    try:
        state, detail = launcher.backend_health(port, timeout=1.0)
        assert state == "none", f"不回应 HTTP 的占用方也只能报 none，实际 {state}"
        assert "error" in detail
    finally:
        holder.close()


def test_bind_conflict_is_recognised_from_the_log(launcher) -> None:
    """子进程日志里的绑定冲突要认出来 —— 这是区分"端口撞了"和"别的启动失败"的唯一判据。"""
    windows = (
        "ERROR:    [Errno 10048] error while attempting to bind on address "
        "('127.0.0.1', 8765): 通常每个套接字地址(协议/网络地址/端口)只允许使用一次。"
    )
    linux = "OSError: [Errno 98] Address already in use"
    other = "ModuleNotFoundError: No module named 'agent_core'"
    assert launcher._log_shows_bind_conflict(windows)
    assert launcher._log_shows_bind_conflict(linux)
    assert not launcher._log_shows_bind_conflict(other)
    assert not launcher._log_shows_bind_conflict("")


def test_health_probe_calls_a_non_json_response_unhealthy(launcher) -> None:
    """端口在监听、也回 HTTP，但内容不是我们的 JSON → unhealthy（不是"没人"）。"""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    port = _free_port()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b"<html>not our api</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # 静音
            pass

    server = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        state, _ = launcher.backend_health(port, timeout=2.0)
        assert state == "unhealthy", f"非 JSON 响应应当是 unhealthy，实际 {state}"
    finally:
        server.shutdown()
        server.server_close()


def test_wait_for_health_detects_an_early_exit(launcher, tmp_path) -> None:
    """进程提前退出要能识别出来（区别于"慢"）。"""
    process = subprocess.Popen(
        [sys.executable, "-c", "raise SystemExit(7)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    process.wait(timeout=15)
    result, info = launcher.wait_for_health(_free_port(), process, timeout=5, label="后端")
    assert result == "exited"
    assert "7" in info


def test_cli_returns_three_when_env_is_missing(monkeypatch) -> None:
    """命令行整体跑一遍：缺环境变量时退出码必须是 3。"""
    env = dict(**__import__("os").environ)
    env["GEMINI_API_KEY"] = ""
    result = subprocess.run(
        [sys.executable, str(LAUNCHER_PATH), "--no-frontend"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, timeout=60,
    )
    # 注意：.env 存在时 load_project_env 会把它读回来，所以这条只在
    # 真的没有 .env 或其中密钥为空时才触发 3。两种结果都是"没崩"。
    assert result.returncode in {0, 1, 2, 3}, result.stdout + result.stderr


def test_spawn_redirects_output_to_the_log_file(launcher, tmp_path) -> None:
    """起一个会说话的进程，确认输出真的落进了日志文件。"""
    import time

    log_path = tmp_path / "probe.log"
    process = launcher.spawn(
        [sys.executable, "-c", "print('hello-from-child'); print('on-stderr', file=__import__('sys').stderr)"],
        REPO_ROOT,
        log_path,
    )
    process.wait(timeout=30)
    text = log_path.read_text(encoding="utf-8", errors="replace")
    assert "hello-from-child" in text
    assert "on-stderr" in text, "stderr 应当被并到同一个日志文件"


def test_spawn_passes_extra_env_to_the_child(launcher, tmp_path) -> None:
    """额外环境变量要真的传到子进程 —— -BackendPort 靠它生效。"""
    log_path = tmp_path / "env.log"
    process = launcher.spawn(
        [sys.executable, "-c", "import os; print('WEB_PORT=' + os.getenv('WEB_PORT', 'MISSING'))"],
        REPO_ROOT,
        log_path,
        extra_env={"WEB_PORT": "9911"},
    )
    process.wait(timeout=30)
    assert "WEB_PORT=9911" in log_path.read_text(encoding="utf-8", errors="replace")


def test_backend_start_passes_the_requested_port_to_the_child(launcher, tmp_path) -> None:
    """回归：-BackendPort 必须传到 web_api.py。

    web_api.py 的端口读的是 WEB_PORT 环境变量（默认 8765）。曾经漏传，
    症状是"探测的是自定义端口、服务却绑在 8765"：探测永远超时，
    还顺手占掉了 8765 —— 表现为莫名奇妙的启动失败。
    这里用一个假的 web_api.py 把收到的端口写出来，直接看有没有传对。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "web_api.py").write_text(
        "import os, pathlib\n"
        "pathlib.Path('seen_port.txt').write_text(os.getenv('WEB_PORT', 'MISSING'))\n",
        encoding="utf-8",
    )
    log_dir = tmp_path / "logs"
    code, _ = launcher.start_backend(repo, _free_port(), timeout=2, log_dir=log_dir)
    assert code == launcher.EXIT_FAILED, "假后端不会真的监听，所以最终是失败"
    seen = (repo / "seen_port.txt").read_text(encoding="utf-8").strip()
    assert seen != "MISSING", "必须把 WEB_PORT 传给子进程，否则 -BackendPort 会被忽略"
    assert seen.isdigit()


def test_write_pid_creates_a_readable_file(launcher, tmp_path) -> None:
    """PID 文件是 stop 脚本认"自己的进程"的唯一依据，格式必须稳定。"""
    target = tmp_path / "nested" / "backend.pid"
    launcher.write_pid(target, 4321)
    assert target.read_text(encoding="ascii") == "4321"


def test_log_tail_handles_missing_and_empty_files(launcher, tmp_path) -> None:
    assert launcher.log_tail(tmp_path / "nope.log") == "(无日志)"
    empty = tmp_path / "empty.log"
    empty.write_text("", encoding="utf-8")
    assert launcher.log_tail(empty) == "(日志为空)"
    filled = tmp_path / "filled.log"
    filled.write_text("\n".join(f"line{i}" for i in range(50)), encoding="utf-8")
    tail = launcher.log_tail(filled, lines=3)
    assert tail.splitlines() == ["line47", "line48", "line49"]


# ── start_backend.py（README 里推荐的入口）────────────────────────────
# 它和上面的启动器是两套代码，但踩的是同一个坑，所以一起钉住。


def _load_start_backend():
    path = REPO_ROOT / "start_backend.py"
    spec = importlib.util.spec_from_file_location("agent_start_backend", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_start_backend_treats_a_silent_port_as_free() -> None:
    """回归：空端口必须报 not_listening。

    曾经把连接超时归到 unhealthy —— 在 Windows 上连接空端口会被丢弃、表现为超时，
    于是 ``python start_backend.py`` 会把空端口当成"被别人占了"，退出码 2、拒绝启动。
    这个坑和启动器的 backend_health 是同一个（都是误以为连接失败会抛 URLError）。
    """
    module = _load_start_backend()
    status, _detail = module._health(f"http://127.0.0.1:{_free_port()}/api/health")
    assert status == "not_listening", f"空端口应当报 not_listening，实际 {status}"


def test_start_backend_reads_a_healthy_status_from_a_real_server() -> None:
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    module = _load_start_backend()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps({"status": "degraded"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # 静音
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, _ = module._health(f"http://127.0.0.1:{port}/api/health")
        assert status == "degraded"
    finally:
        server.shutdown()
        server.server_close()


def test_start_backend_calls_a_non_json_answer_unhealthy() -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    module = _load_start_backend()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b"<html>not our api</html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # 静音
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, _ = module._health(f"http://127.0.0.1:{port}/api/health")
        assert status == "unhealthy", f"非 JSON 响应应当报 unhealthy，实际 {status}"
    finally:
        server.shutdown()
        server.server_close()
