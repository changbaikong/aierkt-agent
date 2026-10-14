"""B 计划第六步的四段回答链路验收：主回答 → 搜索 → 核查 → 发布。

【怎么保证不碰真实数据】
把真实的网页服务起在一个**临时工作目录**里：它的相对库路径
（`agent_core.db` / `agent_work.db` / `data/memory_records.db` / `data/memory_vectors`）
全部落在那个目录里。生产记忆、向量集合、会话账本一个都不碰。

【为什么要有调用回执】
只看回答文本证明不了「主回答与核查真的收到了同一条更新后的记忆」：
回答里出现「三千」也可能是模型自己猜的。脚本因此在本地起两个**只记录、
不改写**的转发代理，把模型与搜索的请求挡一下再原样发给真实上游，
再拿请求体本身做断言 —— 上游没变，回执就是真实调用回执。

【考的是哪条链】
第 1 轮建记忆 → 第 2 轮纠正 → 换一个新会话问同一个问题：
- **主回答**：新会话拿到的回答必须用更新后的值（三千），而且主回答调用的输入里
  确实带着这条更新后的记忆；
- **搜索**：让第 4 轮必须查外部资料，看这一轮到底有没有真的发起搜索
  （以搜索请求回执为准，不以回答措辞为准）；
- **核查**：最终核查调用的输入里必须能看到同一条更新后的记忆，答案里说的值
  也要与库里「现行有效版本」一致（不是靠记忆里的措辞）；
- **发布**：回答必须真的写进会话账本（前端读的就是那份）。

退出码 0 = 四段全部拿到证据；1 = 有段落没拿到证据；3 = 服务起不来。
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_core.contracts import Scope
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_retrieval import MemoryRetriever
from agent_core.project_env import load_project_env

PYTHON = sys.executable
PROJECT_BINDING = "answer-chain-project"
ACCOUNT = "chain-user"
CONVERSATION = "chain-conv"
OTHER_CONVERSATION = "chain-conv-2"

#: 这些 system 提示是辅助调用（判定、整理、核查、提炼），不是主回答。
#: 断言「主回答收到了什么」时要把它们排除，否则会把提炼调用当成主回答。
AUXILIARY_PROMPT_PREFIXES = (
    "你是回答前的核查器",
    "你是请求意图判断器",
    "你是证据核查器",
    "你是检索材料整理器",
    "你是最终核查器",
    "你是桌宠的记忆提炼器",
    "你是记忆候选提炼器",
)


class RecordingProxy:
    """本地记录代理：请求原样转发，同时留下一份**调用回执**。

    只读回答文本证明不了「主回答与核查真的收到了同一条更新后的记忆」——
    回答里出现三千，也可能是模型自己猜的。这里把真实请求挡一下再转发：
    不改写请求、不伪造响应，只把请求体（模型消息 / 搜索查询）记下来给断言用。
    上游仍是真实服务，回执也就是真实调用回执。
    """

    def __init__(self, upstream: str, tag: str) -> None:
        self.upstream = upstream.rstrip("/")
        self.tag = tag
        self.calls: list[dict] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # -- 生命周期 ---------------------------------------------------
    def start(self) -> "RecordingProxy":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # -- 转发 -------------------------------------------------------
    def _handler(self):
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: ANN002 - 代理自己不需要访问日志
                return

            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 的接口名
                proxy._forward(self)

            def do_GET(self):  # noqa: N802
                proxy._forward(self)

        return Handler

    def _forward(self, request: BaseHTTPRequestHandler) -> None:
        """读请求 → 记回执 → 原样转发 → 原样回传。

        代理自己出错时必须**回一个明确的 HTTP 错误**，不能直接断开连接：
        断开会让上游客户端报一句「服务器没响应」，看不出是代理的问题。
        """
        try:
            length = int(request.headers.get("Content-Length") or 0)
            body = request.rfile.read(length) if length else b""
            record: dict = {"tag": self.tag, "path": request.path, "status": None, "body": None}
            if body:
                try:
                    record["body"] = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    record["body"] = body[:800].decode("utf-8", "replace")
            headers = {
                name: value
                for name, value in request.headers.items()
                if name.lower()
                not in ("host", "content-length", "accept-encoding", "connection")
            }
            upstream = urllib.request.Request(
                f"{self.upstream}{request.path}",
                data=body or None,
                headers=headers,
                method=request.command,
            )
            try:
                with self._opener.open(upstream, timeout=300) as response:
                    payload = response.read()
                    status = response.status
                    content_type = response.headers.get("Content-Type", "application/json")
            except urllib.error.HTTPError as exc:
                payload = exc.read()
                status = exc.code
                content_type = (
                    exc.headers.get("Content-Type", "application/json")
                    if exc.headers
                    else "application/json"
                )
            except Exception as exc:  # noqa: BLE001 - 上游失败如实回给客户端
                payload = json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode("utf-8")
                status = 502
                content_type = "application/json"
            record["status"] = status
            self.calls.append(record)
        except Exception as exc:  # noqa: BLE001 - 代理自身出错也要有一个明确回应
            payload = json.dumps({"proxy_error": f"{type(exc).__name__}: {exc}"}).encode("utf-8")
            status, content_type = 502, "application/json"
        request.send_response(status)
        request.send_header("Content-Type", content_type)
        request.send_header("Content-Length", str(len(payload)))
        request.end_headers()
        request.wfile.write(payload)


def call_messages(call: dict) -> list[dict]:
    body = call.get("body")
    if not isinstance(body, dict):
        return []
    return [item for item in body.get("messages") or [] if isinstance(item, dict)]


def call_text(call: dict) -> str:
    """一次模型调用的完整输入文本（用来核对它到底收到了什么）。"""
    return json.dumps(call_messages(call), ensure_ascii=False)


def system_text(call: dict) -> str:
    for message in call_messages(call):
        if message.get("role") == "system":
            return str(message.get("content") or "")
    return ""


def is_main_answer_call(call: dict) -> bool:
    system = system_text(call)
    return bool(system) and not system.startswith(AUXILIARY_PROMPT_PREFIXES)


def is_final_review_call(call: dict) -> bool:
    return system_text(call).startswith("你是最终核查器")


def search_queries(proxy: RecordingProxy) -> list[dict]:
    """搜索提交回执：查了什么、上游回了什么。"""
    found = []
    for call in proxy.calls:
        body = call.get("body")
        query = ""
        if isinstance(body, dict):
            query = str(body.get("query") or "")
        found.append({"path": call["path"], "query": query, "status": call["status"]})
    return found


def _redact(call: dict) -> dict:
    """给报告留一份不含密钥的摘要。"""
    body = call.get("body")
    model = body.get("model") if isinstance(body, dict) else None
    return {"tag": call.get("tag"), "path": call.get("path"), "status": call.get("status"), "model": model}



class Embeddings:
    def embed(self, text):
        return [0.1, 0.2, 0.3]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def build_env(
    work: Path,
    port: int,
    *,
    model_base_url: str | None = None,
    search_base_url: str | None = None,
) -> dict:
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(ROOT),
            "PYTHONIOENCODING": "utf-8",
            "WEB_PORT": str(port),
            "WEB_HOST": "127.0.0.1",
            # 只影响这次临时运行：新版语义记忆与内容维护都打开，处理端指定本地。
            "MEMORY_SEMANTIC_ENABLED": "true",
            "MEMORY_PROCESSING_ENDPOINT": "local",
            "MEMORY_CONTENT_MAINTENANCE_ENABLED": "true",
            "MEMORY_CLOUD_SYNC_ENABLED": "false",
            "AGENT_MEMORY_PROJECT": PROJECT_BINDING,
            "TEMP": str(work),
            "TMP": str(work),
            "TMPDIR": str(work),
        }
    )
    # 模型与搜索都改走本地记录代理：上游不变，只是留一份调用回执。
    if model_base_url:
        env["GOOGLE_GEMINI_BASE_URL"] = model_base_url
    if search_base_url:
        env["BOCHA_BASE_URL"] = search_base_url
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        env.pop(name, None)
    return env


def http(port: int, path: str, payload: dict | None = None, *, timeout: float = 30.0):
    url = f"http://127.0.0.1:{port}{path}"
    if payload is None:
        request = urllib.request.Request(url)
    else:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
    return response.status, body


def wait_ready(port: int, *, seconds: float = 120.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        for path in ("/api/health", "/api/settings"):
            try:
                status, _ = http(port, path, timeout=4.0)
                if status < 500:
                    return True
            except Exception:  # noqa: BLE001 - 还没起来，继续等
                continue
        time.sleep(1.0)
    return False


def send(port: int, message: str, conversation: str) -> dict:
    status, body = http(
        port,
        "/api/chat",
        {"message": message, "account_id": ACCOUNT, "conversation_id": conversation},
    )
    return {"status": status, "body": json.loads(body or "{}")}


def wait_for(condition, *, seconds: float = 90.0, interval: float = 2.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(interval)
    return None


def assistant_replies(core_db: Path, conversation: str, *, after_rowid: int = 0):
    if not core_db.exists():
        return []
    try:
        with sqlite3.connect(core_db) as db:
            rows = db.execute(
                "SELECT rowid, role, content FROM agent_core_conversation_messages "
                "WHERE conversation_id=? AND rowid>? ORDER BY rowid",
                (conversation, after_rowid),
            ).fetchall()
    except sqlite3.Error:
        return []
    return [
        {"rowid": row[0], "role": row[1], "content": row[2]}
        for row in rows
        if row[1] == "assistant"
    ]


def max_rowid(core_db: Path) -> int:
    if not core_db.exists():
        return 0
    try:
        with sqlite3.connect(core_db) as db:
            return int(db.execute("SELECT COALESCE(MAX(rowid),0) FROM agent_core_conversation_messages").fetchone()[0])
    except sqlite3.Error:
        return 0


def run_tool_text(core_db: Path) -> str:
    """运行账本里记下的工具终局状态（有没有真的调搜索，一目了然）。"""
    if not core_db.exists():
        return ""
    try:
        with sqlite3.connect(core_db) as db:
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(agent_core_runs)")
            }
            if "tool_statuses" not in columns:
                return ""
            rows = db.execute(
                "SELECT tool_statuses FROM agent_core_runs WHERE tool_statuses IS NOT NULL"
            ).fetchall()
    except sqlite3.Error:
        return ""
    return "\n".join(str(row[0] or "") for row in rows)


def _stop(server: subprocess.Popen, log) -> None:
    """停服务并等它真的退出：向量库是单进程独占的，没退干净后面进不去。"""
    if server.poll() is not None:
        return
    server.terminate()
    try:
        server.wait(timeout=20)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait(timeout=10)
    log.flush()


def memory_state(memory_db: Path) -> dict:
    if not memory_db.exists():
        return {"records": [], "jobs": []}
    with sqlite3.connect(memory_db) as db:
        db.row_factory = sqlite3.Row
        records = [
            dict(row)
            for row in db.execute(
                "SELECT id,content,kind,content_status,status,layer,record_version,"
                "effective_record_id,semantics_version,scope_kind,project_id "
                "FROM memory_records ORDER BY created_at"
            ).fetchall()
        ]
        jobs = [
            dict(row)
            for row in db.execute(
                "SELECT job_id,turn_id,status,attempts,kind,last_error,saved_ids "
                "FROM memory_candidate_jobs ORDER BY created_at"
            ).fetchall()
        ]
    return {"records": records, "jobs": jobs}


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="answer-chain-"))
    port = free_port()
    # 先取真实上游地址，再把子进程的两处地址换成记录代理 —— 上游不变，
    # 只是经过我们这里留一份调用回执。
    load_project_env()
    model_upstream = os.environ.get("GOOGLE_GEMINI_BASE_URL", "").strip()
    search_upstream = os.environ.get("BOCHA_BASE_URL", "").strip() or "https://api.bochaai.com/v1"
    model_proxy = RecordingProxy(model_upstream, "model").start() if model_upstream else None
    search_proxy = RecordingProxy(search_upstream, "search").start()
    proxies = [item for item in (model_proxy, search_proxy) if item is not None]
    env = build_env(
        work,
        port,
        model_base_url=model_proxy.url if model_proxy else None,
        search_base_url=search_proxy.url,
    )
    core_db = work / "agent_core.db"
    memory_db = work / "data" / "memory_records.db"
    log_path = work / "server.log"
    log = log_path.open("w", encoding="utf-8")
    server = subprocess.Popen(
        [PYTHON, "-m", "uvicorn", "web_api:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(work),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    checks: list[dict] = []

    def check(name: str, passed: bool, actual) -> None:
        checks.append({"check": name, "passed": bool(passed), "actual": actual})

    try:
        if not wait_ready(port):
            print(json.dumps({"ok": False, "reason": "服务没能起来", "work": str(work)}, ensure_ascii=False))
            return 3

        # ---- 1. 建记忆：正常对话 -------------------------------------
        before = max_rowid(core_db)
        check("回合被服务接受（主回答已入队）", send(port, "记住：这个项目的预算上限是五千", CONVERSATION)["status"] == 202, {})
        first = wait_for(lambda: assistant_replies(core_db, CONVERSATION, after_rowid=before), seconds=120)
        check(
            "发布：回答写进了会话账本",
            bool(first),
            {"replies": [item["content"][:60] for item in (first or [])]},
        )
        stored = wait_for(
            lambda: [row for row in memory_state(memory_db)["records"] if row["semantics_version"] >= 1],
            seconds=120,
        )
        check(
            "保存：后台队列把这一轮落成了新语义记录",
            bool(stored),
            {"records": [f"{row['kind']}/{row['content_status']}" for row in (stored or [])]},
        )

        # ---- 2. 新信息：纠正 -----------------------------------------
        before = max_rowid(core_db)
        send(port, "不是五千，是三千", CONVERSATION)
        wait_for(lambda: assistant_replies(core_db, CONVERSATION, after_rowid=before), seconds=120)
        corrected = wait_for(
            lambda: [
                row
                for row in memory_state(memory_db)["records"]
                if "三千" in (row["content"] or "")
            ],
            seconds=120,
        )
        check(
            "更新：纠正落成了新版本",
            bool(corrected),
            {"records": [row["content"] for row in (corrected or [])]},
        )

        # ---- 3. 跨会话召回 + 主回答 ----------------------------------
        before = max_rowid(core_db)
        send(port, "这个项目的预算上限是多少？只答数字。", OTHER_CONVERSATION)
        answer_rows = wait_for(
            lambda: assistant_replies(core_db, OTHER_CONVERSATION, after_rowid=before), seconds=120
        )
        answer = (answer_rows or [{}])[-1].get("content", "")
        check(
            "主回答：新会话用的是更新后的值",
            "三千" in answer and "五千" not in answer,
            {"answer": answer[:160]},
        )
        # 调用回执：主回答**实际收到的输入**里有没有更新后的记忆。
        # 只看回答文本证明不了这件事 —— 模型也可能自己猜出三千。
        answer_calls = (
            [
                call
                for call in (model_proxy.calls if model_proxy else [])
                if is_main_answer_call(call) and "预算上限" in call_text(call)
            ]
            if model_proxy
            else []
        )
        answer_receipt_text = "\n".join(call_text(call) for call in answer_calls)
        check(
            "主回答回执：主回答调用实际收到了更新后的有效记忆",
            bool(answer_calls) and "三千" in answer_receipt_text,
            {
                "main_answer_calls": len(answer_calls),
                "received_updated_value": "三千" in answer_receipt_text,
                "received_stale_value": "五千" in answer_receipt_text,
            },
        )

        # ---- 4. 搜索：必须查外部资料的一轮 ---------------------------
        before = max_rowid(core_db)
        send(port, "帮我查一下 DeepSeek 官方现在最新的模型价格，查完再确认一次项目预算上限。", OTHER_CONVERSATION)
        wait_for(
            lambda: assistant_replies(core_db, OTHER_CONVERSATION, after_rowid=before), seconds=240
        )
        # 搜索是异步的：回答可能先发一句「还在查」。再等一会儿看有没有后续。
        time.sleep(60)
        search_rows = assistant_replies(core_db, OTHER_CONVERSATION, after_rowid=before) or []
        search_answer = "\n".join(item["content"] for item in search_rows)
        tool_text = run_tool_text(core_db)
        # 两条证据任取其一：运行账本里记了搜索工具；或者回答里出现了只有检索
        # 才能拿到的外部材料（价格数字、来源措辞）。只看账本会漏掉「账本没记
        # 这条路」，只看回答又可能把模型自己编的数字当成检索结果。
        search_in_answer = any(
            marker in search_answer
            for marker in ("元/百万", "搜到", "查到", "外部报道", "官方")
        )
        search_in_ledger = "search" in tool_text.lower()
        check(
            "搜索：这一轮真的发起了外部检索",
            search_in_answer or search_in_ledger,
            {
                "answer": search_answer[:200],
                "tool_statuses": tool_text[:120],
                "search_in_answer": search_in_answer,
                "search_in_ledger": search_in_ledger,
            },
        )

        # 搜索执行回执：代理挡住的是**真实搜索请求**，转发到真实上游。
        queries = search_queries(search_proxy) if search_proxy else []
        ok_queries = [item for item in queries if item["status"] == 200 and item["query"]]
        check(
            "搜索回执：外部检索请求真的发出去了并拿到了结果",
            bool(ok_queries),
            {"calls": queries[:5], "successful": len(ok_queries)},
        )

        # 核查回执：最终核查调用的输入里有没有同一条更新后的记忆。
        review_calls = (
            [call for call in (model_proxy.calls if model_proxy else []) if is_final_review_call(call)]
            if model_proxy
            else []
        )
        review_receipt_text = "\n".join(call_text(call) for call in review_calls)
        check(
            "核查回执：最终核查调用实际收到了同一条更新后的记忆",
            bool(review_calls) and "三千" in review_receipt_text,
            {
                "final_review_calls": len(review_calls),
                "received_updated_value": "三千" in review_receipt_text,
                "received_stale_value": "五千" in review_receipt_text,
            },
        )

        # ---- 5. 核查：答案里的值 == 库里现行有效版本 ------------------
        # 先把队列排空再停服务：候选工人 30 秒轮一次，刚发布的回合可能还没被领走。
        wait_for(
            lambda: all(
                row["status"] in ("completed", "skipped")
                for row in memory_state(memory_db)["jobs"]
            ),
            seconds=180,
            interval=5.0,
        )
        _stop(server, log)
        server = None
        state = memory_state(memory_db)
        facts = [
            row
            for row in state["records"]
            if row["semantics_version"] >= 1 and row["kind"] == "fact"
        ]
        effective = [row for row in facts if row["status"] == "active"]
        archived = [row for row in facts if row["status"] == "archived"]
        # 只盯「预算」这一条：这一轮检索还会落进两三条外部价格事实，它们跟预算
        # 无关，拿它们一起断言会把「检查写得过宽」当成「系统出错」。
        effective_budget = [row for row in effective if "三千" in (row["content"] or "")]
        # 「还不是现行版本」的判据要**同时**看两个数字：纠正那条记录自己就会写
        # 「不是五千，是三千」，只按「含五千」筛会把新版本当成漏改的旧版本。
        stale_active = [
            row
            for row in effective
            if "五千" in (row["content"] or "") and "三千" not in (row["content"] or "")
        ]
        stale_archived = [row for row in archived if "五千" in (row["content"] or "")]
        check(
            "核查：答案里的值就是库里现行的有效版本",
            bool(effective_budget) and not stale_active and bool(stale_archived),
            {
                "effective_budget": [row["content"] for row in effective_budget],
                "stale_still_active": [row["content"] for row in stale_active],
                "stale_archived": [row["content"] for row in stale_archived],
                "answer": answer[:120],
            },
        )

        check(
            "队列已排空（没有卡住的作业）",
            all(row["status"] in ("completed", "skipped") for row in state["jobs"]),
            {"jobs": [(row["kind"], row["status"], row["attempts"], row["last_error"]) for row in state["jobs"]]},
        )
        report = {
            "ok": all(item["passed"] for item in checks),
            "passed": sum(item["passed"] for item in checks),
            "total": len(checks),
            "checks": checks,
            "jobs": state["jobs"],
            "records": [
                {
                    "content": row["content"][:40],
                    "kind": row["kind"],
                    "content_status": row["content_status"],
                    "status": row["status"],
                    "version": row["record_version"],
                }
                for row in state["records"]
            ],
            "temp_workdir": str(work),
            "production_memory_touched": False,
            "call_receipts": {
                "model_calls": len(model_proxy.calls) if model_proxy else 0,
                "main_answer_calls": len(answer_calls),
                "final_review_calls": len(review_calls),
                "search_requests": len(queries),
                "successful_search_requests": len(ok_queries),
                "proxied": [_redact(call) for call in (model_proxy.calls[:3] if model_proxy else [])],
                "search_queries": queries[:5],
            },
            "receipt_limits": [
                "回执来自只转发不改写的本地代理：上游是真实模型与真实搜索服务",
                "回执只能证明「这次调用收到了什么」，不能证明模型每次都按它回答",
            ],
        }
    finally:
        if server is not None:
            _stop(server, log)
        log.close()
        for proxy in proxies:
            try:
                proxy.stop()
            except Exception:  # noqa: BLE001 - 关代理失败不影响验收结论
                pass

    target = ROOT / "docs" / "2026-10-11-memory-answer-chain-evidence.json"
    # 目录可能还不存在（新机器上第一次跑）：先建，别在四段都跑通之后
    # 因为写不进证据文件而报失败。
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
