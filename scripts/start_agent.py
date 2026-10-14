r"""本地服务启动器：后端（web_api.py）+ 前端（Vite）。

为什么启动逻辑放在 Python 而不是 PowerShell：

1. **编码**。Windows PowerShell 5.1 按系统 ANSI 代码页读 `.ps1`，
   而用户目录可能含中文（例：`C:\\Users\\<中文用户名>\...`）。脚本里凡是要拼一条
   带中文路径的命令行，都会在传递途中被重新编码 —— 实测症状是
   「系统找不到指定的文件」，看着像文件不存在，其实是路径被读坏了。
   Python 全程用 Unicode 处理路径，没有这个问题。

2. **可测**。这里的每个分支（端口占用 / 提前退出 / 缺环境变量 / 超时）
   都能用假进程和假 HTTP 直接测；PowerShell 里做不到。

`scripts/start_agent.ps1` 只是它的薄入口，负责给出友好的中文提示。

退出码：

* 0 —— 全部就绪（含"复用了已有服务"）
* 1 —— 启动失败（进程提前退出 / 超时 / 前端目录或依赖缺失）
* 2 —— 端口被别的程序占用，或端口上有响应但不是可用的后端
* 3 —— 缺少必需的环境变量配置
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent

# 本脚本在 scripts/ 子目录里，直接 `python scripts/start_agent.py` 跑时
# 仓库根不在 sys.path 上，import agent_core 会失败。显式补上。
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: 健康检查判为"可用"的状态。degraded 表示部分能力没配好（例如某个外部
#: 探测超时），但服务本身能用 —— 仍然复用，不去重启它。
HEALTHY_STATUSES = {"healthy", "degraded"}

#: 后端冷启动的默认等待上限（秒）。实测本机从进程起到监听要 ~40 秒：
#: 启动阶段要加载记忆库、向量库并做一轮能力探测。给 120 秒是留足余量，
#: 同时仍然有上限 —— 脚本不该无限等下去。
DEFAULT_BACKEND_TIMEOUT = 120.0

#: 前端 dev server 的默认等待上限。Vite 通常几秒就绪，
#: 首次要预构建依赖时会慢一些。
DEFAULT_FRONTEND_TIMEOUT = 90.0

#: 启动前必须存在的环境变量（值不读出来，只确认非空）。
REQUIRED_ENV_KEYS = ("GEMINI_API_KEY",)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_PORT_TAKEN = 2
EXIT_ENV_MISSING = 3


def log(message: str) -> None:
    print(f"[start_agent] {message}", flush=True)


# ── 环境变量 ────────────────────────────────────────────────────────
def missing_env_keys(repo_root: Path) -> list[str]:
    """检查 .env 里必需项是否都配了。只判"有没有值"，不读出值。"""
    env_file = repo_root / ".env"
    if not env_file.exists():
        return ["(.env 文件不存在)"]

    from agent_core.project_env import load_project_env

    load_project_env()
    return [key for key in REQUIRED_ENV_KEYS if not str(os.getenv(key, "")).strip()]


# ── 端口与健康 ──────────────────────────────────────────────────────
def port_listening(port: int, host: str = "127.0.0.1", timeout: float = 0.6) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(timeout)
        return probe.connect_ex((host, port)) == 0


def backend_health(port: int, timeout: float = 2.0) -> tuple[str, dict]:
    """探测 /api/health。

    返回 (state, detail)，state 取值：
    * ``healthy``   —— 已经有可用的后端，复用；
    * ``unhealthy`` —— 有响应但不是可用后端（对方可能是别的程序）；
    * ``none``      —— 连不上：要么端口空着，要么被不回应 HTTP 的程序占着。

    ``none`` 把"空着"和"被外人占着"合并成一种情况，是有意为之：
    本机上（实测）连接一个空端口会被直接丢弃、表现为超时，与"对方在监听但不回应"
    的超时完全一样，用户态分不出来。所以这里不下结论，交给真正绑定端口的子进程裁决——
    详见 start_backend。
    """
    url = f"http://127.0.0.1:{port}/api/health"
    try:
        with urlopen(url, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
        payload = json.loads(body)
        status = str(payload.get("status") or "unknown")
        if status in HEALTHY_STATUSES:
            return "healthy", payload
        return "unhealthy", payload
    except json.JSONDecodeError as exc:
        # 端口在监听、也回了 HTTP，但内容不是我们的 JSON —— 是别的服务。
        return "unhealthy", {"error": f"响应不是 JSON：{exc}"}
    except Exception as exc:
        # 注意 urllib 的连接失败抛的是 TimeoutError / ConnectionError（OSError 家族），
        # 不是 URLError，所以不能靠 except URLError 来分类。
        return "none", {"error": f"{type(exc).__name__}: {exc}"}


def wait_for_health(
    port: int,
    process: subprocess.Popen | None,
    timeout: float,
    *, label: str,
) -> tuple[str, str]:
    """有上限地等待健康检查通过。

    返回 (result, detail)：
    * ``ready``         —— 就绪；
    * ``exited``        —— 进程提前退出（detail 是日志尾部）；
    * ``timeout``       —— 超时（detail 是日志尾部）；
    * ``not_ours``      —— 有响应但不是可用后端。
    """
    deadline = time.monotonic() + max(0.1, timeout)
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            return "exited", f"退出码 {process.returncode}"
        state, detail = backend_health(port)
        if state == "healthy":
            return "ready", str(detail.get("status"))
        if state == "unhealthy":
            return "not_ours", str(detail)
        time.sleep(0.4)
    return "timeout", ""


def log_tail(path: Path, lines: int = 25) -> str:
    if not path.exists():
        return "(无日志)"
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        return f"(读日志失败：{type(exc).__name__})"
    if not content:
        return "(日志为空)"
    return "\n".join(content[-lines:])


#: 子进程绑不上端口时，日志里一定会出现的特征串。
#: Windows 是 ``[Errno 10048]`` / 中文"只允许使用一次"，Linux 是 ``[Errno 98]``。
_BIND_CONFLICT_MARKERS = (
    "errno 10048",
    "errno 98",
    "only one usage of each socket address",
    "address already in use",
    "只允许使用一次",
)


def _log_shows_bind_conflict(tail: str) -> bool:
    """从子进程日志判断它是不是"端口被别人占了绑不上"。

    用日志而不是直接探测端口，是因为本机分不出"空端口"和"被占但不回应 HTTP 的端口"
    （见 backend_health 的说明），而子进程报的绑定错误是明确无误的。
    """
    lowered = tail.lower()
    return any(marker in lowered for marker in _BIND_CONFLICT_MARKERS)


# ── 子进程 ──────────────────────────────────────────────────────────
def spawn(
    argv: list[str],
    cwd: Path,
    log_path: Path,
    *,
    extra_env: dict[str, str] | None = None,
) -> subprocess.Popen:
    """起一个后台子进程，stdout/stderr 并到同一个日志文件。

    用 ``subprocess.Popen`` + 文件句柄，而不是 shell 重定向：
    日志文件由我们自己创建，句柄直接交给子进程，没有任何一层需要解析
    命令行里的引号与重定向符号 —— 那正是前面几轮栽跟头的地方。

    子进程必须**脱离父进程**：本脚本跑完就退出，如果子进程还挂在父进程的
    控制台上，父进程一退它就被一起带走（实测症状是"报告就绪后服务立刻消失"，
    第二次运行又去起一个新的）。做法是：

    * Windows：``DETACHED_PROCESS`` + ``CREATE_NEW_PROCESS_GROUP``，
      让它有自己的控制台、不收到父进程的 Ctrl+C / 终止信号；
    * POSIX：``start_new_session=True``。

    注意 ``CREATE_NO_WINDOW`` 与 ``DETACHED_PROCESS`` 互斥，同时给会让
    CreateProcess 报错，所以这里只用 DETACHED_PROCESS ——
    它本来就不弹窗。
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log_path, "w", encoding="utf-8", errors="replace")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items()})

    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
    else:
        kwargs["start_new_session"] = True

    try:
        return subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            **kwargs,
        )
    finally:
        # 父进程这一侧的句柄可以立刻关掉：子进程已经继承了它。
        handle.close()


def write_pid(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pid), encoding="ascii")


# ── 主流程 ──────────────────────────────────────────────────────────
def start_backend(repo_root: Path, port: int, timeout: float, log_dir: Path) -> tuple[int, subprocess.Popen | None]:
    state, detail = backend_health(port)
    if state == "healthy":
        log(f"后端已在 127.0.0.1:{port} 运行（{detail.get('status')}），复用现有进程。")
        return EXIT_OK, None
    if state == "unhealthy":
        log(f"端口 {port} 上有服务响应，但健康状态是 {detail.get('status')}，不是可用的后端。")
        log("本脚本不会去动别人的进程；请先自行排查，或用 -BackendPort 换一个端口。")
        return EXIT_PORT_TAKEN, None

    # ``backend_health`` 无法区分空端口和静默监听者。先做一次 TCP 预检，
    # 避免为一个已经被占用的端口启动后端，再等它完成昂贵的初始化后才在绑定处失败。
    if port_listening(port):
        log(f"端口 {port} 已被占用，但占用方没有返回可用的后端健康响应。")
        log("本脚本不会去动别人的进程；请先自行排查，或用 -BackendPort 换一个端口。")
        return EXIT_PORT_TAKEN, None

    # state == "none"：连不上。可能是端口空着（正常），也可能是被别的程序占着但不回应
    # HTTP。本机（Windows）实测分不出这两种，所以不预先下结论，直接去起：
    # 端口真空着 → 起得来；被占着 → 子进程会在绑定时报错退出，由下面的日志判据区分。
    python = os.getenv("AGENT_PYTHON", "").strip() or sys.executable
    log_path = log_dir / "backend.log"
    log(f"启动后端：{python} web_api.py（端口 {port}）")
    # web_api.py 的端口读的是 WEB_PORT 环境变量（默认 8765）。必须显式传进去，
    # 否则 -BackendPort 会被静默忽略：探测的是自定义端口，服务却绑在 8765 上。
    process = spawn(
        [python, str(repo_root / "web_api.py")],
        repo_root,
        log_path,
        extra_env={"WEB_PORT": str(port)},
    )

    result, info = wait_for_health(port, process, timeout, label="后端")
    if result == "ready":
        log(f"后端就绪：http://127.0.0.1:{port}")
        write_pid(log_dir / "backend.pid", process.pid)
        return EXIT_OK, process
    if result == "not_ours":
        log(f"端口 {port} 被占用：{info}")
        _terminate(process)
        return EXIT_PORT_TAKEN, None

    # 起不来。用日志区分「端口被别人占着」和「其他启动失败」——两种都得报对，否则
    # 用户拿到 EXIT_FAILED 会去查配置，实际却只是端口撞了。
    tail = log_tail(log_path)
    if result == "exited":
        log(f"后端进程提前退出（{info}）。")
    else:
        log(f"后端在 {timeout:.0f} 秒内没有就绪。")
    log("日志尾部：")
    log(tail)
    log(f"完整日志：{log_path}")
    _terminate(process)
    if _log_shows_bind_conflict(tail):
        log(f"判断原因：端口 {port} 被其他程序占用。换端口用 -BackendPort <端口>。")
        return EXIT_PORT_TAKEN, None
    return EXIT_FAILED, None


def start_frontend(repo_root: Path, port: int, timeout: float, log_dir: Path) -> int:
    frontend_dir = repo_root / "fronted" / "arcueid_daily_50" / "fronteddd"
    if not frontend_dir.is_dir():
        log(f"前端目录不存在：{frontend_dir}")
        return EXIT_FAILED

    if port_listening(port):
        # 只确认"有人在监听"：去猜它是不是同一个项目可能猜错，
        # 猜错会启动出第二个 dev server，比复用更糟。
        log(f"前端端口 {port} 已有服务在监听，复用。")
        return EXIT_OK

    if not (frontend_dir / "node_modules").is_dir():
        log(f"前端依赖未安装，请先在 {frontend_dir} 运行：npm install")
        return EXIT_FAILED

    import shutil

    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        log("找不到 npm，请确认 Node.js 已安装并在 PATH 中。")
        return EXIT_FAILED

    log_path = log_dir / "frontend.log"
    log(f"启动前端：npm run dev（端口 {port}）")
    process = spawn(
        [npm, "run", "dev", "--", "--port", str(port), "--strictPort"],
        frontend_dir,
        log_path,
    )

    deadline = time.monotonic() + max(0.1, timeout)
    while time.monotonic() < deadline:
        if process.poll() is not None:
            log(f"前端进程提前退出（退出码 {process.returncode}）。")
            log("日志尾部：")
            log(log_tail(log_path))
            return EXIT_FAILED
        if port_listening(port):
            log(f"前端就绪：http://127.0.0.1:{port}")
            write_pid(log_dir / "frontend.pid", process.pid)
            return EXIT_OK
        time.sleep(0.4)

    log(f"前端在 {timeout:.0f} 秒内没有监听 {port}。")
    log("日志尾部：")
    log(log_tail(log_path))
    _terminate(process)
    return EXIT_FAILED


def _terminate(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动本地 Agent（后端 + 前端）")
    parser.add_argument("--backend-port", type=int, default=8765)
    parser.add_argument("--frontend-port", type=int, default=5173)
    parser.add_argument(
        "--timeout", type=float, default=None,
        help="健康检查等待上限（秒）。不给则后端用 120、前端用 90。",
    )
    parser.add_argument("--no-frontend", action="store_true")
    parser.add_argument("--log-dir", default=str(REPO_ROOT / "logs"))
    args = parser.parse_args(argv)

    backend_timeout = args.timeout or DEFAULT_BACKEND_TIMEOUT
    frontend_timeout = args.timeout or DEFAULT_FRONTEND_TIMEOUT

    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    missing = missing_env_keys(REPO_ROOT)
    if missing:
        log("缺少必需的环境变量配置：")
        for key in missing:
            log(f"  - {key}")
        log(f"请在 {REPO_ROOT / '.env'} 里补齐后重试。")
        return EXIT_ENV_MISSING

    code, _ = start_backend(REPO_ROOT, args.backend_port, backend_timeout, log_dir)
    if code != EXIT_OK:
        return code

    if args.no_frontend:
        log("按参数跳过前端。")
    else:
        code = start_frontend(REPO_ROOT, args.frontend_port, frontend_timeout, log_dir)
        if code != EXIT_OK:
            return code

    log("全部就绪。")
    print("", flush=True)
    print(f"  后端  http://127.0.0.1:{args.backend_port}", flush=True)
    if not args.no_frontend:
        print(f"  前端  http://127.0.0.1:{args.frontend_port}", flush=True)
    print(f"  日志  {log_dir}", flush=True)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
