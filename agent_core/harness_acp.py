"""ACP (Agent Client Protocol) transport: JSON-RPC 2.0 over a child process' stdio.

Scope of this module: **the wire**. It knows how to start a program, speak
newline-delimited JSON-RPC to it, route the three message shapes (response,
notification, agent→client request) and how to stop it. It knows nothing about
Work tasks, snapshots, permissions policy or the ledger — those live in
``deepseek_harness_provider``.

Three decisions worth stating, because they are the ones a re-implementation
gets wrong:

1. **Newline-delimited JSON, not Content-Length framing.** ACP runs over
   newline-delimited JSON (``ndJsonStream``); the Harness bundles that same
   framing. One message per line, UTF-8, no embedded raw newlines (``json.dumps``
   escapes them).
2. **Disconnect fails every pending call.** If the child exits, a caller blocked
   on ``request`` must not wait for its timeout — it gets
   :class:`AcpDisconnected` immediately, and the recorded exit code plus the
   stderr tail travel with the exception. "The process died" and "the process is
   slow" are different facts and must not look the same to the caller.
3. **stderr is diagnostic only.** It is drained continuously (a full pipe would
   block the child) into a bounded ring buffer used for error reporting. It is
   never parsed for protocol state.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence

#: ACP protocol version this client speaks.
ACP_PROTOCOL_VERSION = 1

#: Agent-side (server) method names, as defined by ACP.
ACP_METHOD_INITIALIZE = "initialize"
ACP_METHOD_SESSION_NEW = "session/new"
ACP_METHOD_SESSION_LOAD = "session/load"
ACP_METHOD_SESSION_RESUME = "session/resume"
ACP_METHOD_SESSION_PROMPT = "session/prompt"
ACP_METHOD_SESSION_CANCEL = "session/cancel"
ACP_METHOD_SESSION_CLOSE = "session/close"
ACP_METHOD_SESSION_UPDATE = "session/update"
ACP_METHOD_REQUEST_PERMISSION = "session/request_permission"
ACP_METHOD_FS_READ_TEXT_FILE = "fs/read_text_file"
ACP_METHOD_FS_WRITE_TEXT_FILE = "fs/write_text_file"

#: JSON-RPC error codes used by the client side of the bridge.
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INTERNAL_ERROR = -32603

#: Largest single line accepted from the agent. A runaway process must not be
#: able to exhaust Host memory before its timeout fires.
MAX_LINE_BYTES = 4 * 1024 * 1024

#: How many stderr lines to keep for diagnostics.
STDERR_TAIL_LINES = 80


class AcpError(RuntimeError):
    """Base class for every transport-level failure."""


class AcpProtocolError(AcpError):
    def __init__(self, message: str, *, code: int | None = None, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class AcpDisconnected(AcpError):
    """The child process went away, or its stdout closed."""

    def __init__(self, message: str, *, exit_code: int | None = None, stderr_tail: str = "") -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr_tail = stderr_tail


class AcpTimeout(AcpError):
    """A request outlived its deadline while the process stayed alive."""


class AcpSpawnError(AcpError):
    """The executable could not be started at all."""


@dataclass(frozen=True)
class AcpLaunchSpec:
    """Everything needed to start one agent process."""

    executable: str
    args: tuple[str, ...] = ()
    cwd: str = ""
    env: Mapping[str, str] = field(default_factory=dict)

    def resolved_env(self) -> dict[str, str]:
        """Child environment: a copy of ours plus the explicit overrides.

        Copying is deliberate — wiping the environment (``env=`` with only the
        overrides) breaks the child in ways that only show up on the user's
        machine, not in CI.
        """
        environment = dict(os.environ)
        environment.update({str(key): str(value) for key, value in (self.env or {}).items()})
        return environment


@dataclass(frozen=True)
class AcpHandshake:
    """What ``initialize`` told us, kept in the shape the harness produced."""

    protocol_version: int
    agent_capabilities: Mapping[str, Any] = field(default_factory=dict)
    agent_info: Mapping[str, Any] = field(default_factory=dict)
    auth_methods: tuple[Mapping[str, Any], ...] = ()

    @classmethod
    def from_result(cls, result: Mapping[str, Any] | None) -> "AcpHandshake":
        payload = dict(result or {})
        try:
            version = int(payload.get("protocolVersion") or 0)
        except (TypeError, ValueError):
            version = 0
        capabilities = payload.get("agentCapabilities")
        info = payload.get("agentInfo")
        methods = payload.get("authMethods")
        return cls(
            protocol_version=version,
            agent_capabilities=dict(capabilities) if isinstance(capabilities, Mapping) else {},
            agent_info=dict(info) if isinstance(info, Mapping) else {},
            auth_methods=tuple(item for item in methods or () if isinstance(item, Mapping)),
        )

    @property
    def version_text(self) -> str:
        for key in ("version", "agentVersion", "buildVersion"):
            value = self.agent_info.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @property
    def name_text(self) -> str:
        for key in ("name", "title", "agentName"):
            value = self.agent_info.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""


NotificationHandler = Callable[[str, Mapping[str, Any]], Any]
RequestHandler = Callable[[str, Mapping[str, Any]], Any]
DisconnectHandler = Callable[[AcpDisconnected], Any]


class AcpConnection:
    """One child process speaking ACP over stdio."""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        *,
        on_notification: NotificationHandler | None = None,
        on_request: RequestHandler | None = None,
        on_disconnect: DisconnectHandler | None = None,
    ) -> None:
        self._process = process
        self._on_notification = on_notification
        self._on_request = on_request
        self._on_disconnect = on_disconnect
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 1
        self._write_lock = asyncio.Lock()
        self._stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        self._closed = False
        self.exit_code: int | None = None
        self.disconnect_reason: str = ""
        self.handshake: AcpHandshake | None = None
        self._tasks: list[asyncio.Task[Any]] = []

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    @classmethod
    async def spawn(
        cls,
        spec: AcpLaunchSpec,
        *,
        on_notification: NotificationHandler | None = None,
        on_request: RequestHandler | None = None,
        on_disconnect: DisconnectHandler | None = None,
    ) -> "AcpConnection":
        if not str(spec.executable).strip():
            raise AcpSpawnError("harness executable is not configured")
        if not os.path.isfile(spec.executable):
            raise AcpSpawnError(f"harness executable not found: {spec.executable}")
        try:
            process = await asyncio.create_subprocess_exec(
                spec.executable,
                *spec.args,
                cwd=spec.cwd or None,
                env=spec.resolved_env(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:
            raise AcpSpawnError(f"cannot start harness: {type(exc).__name__}: {exc}") from exc
        connection = cls(
            process,
            on_notification=on_notification,
            on_request=on_request,
            on_disconnect=on_disconnect,
        )
        connection._tasks = [
            asyncio.create_task(connection._read_stdout(), name="acp-stdout"),
            asyncio.create_task(connection._read_stderr(), name="acp-stderr"),
            asyncio.create_task(connection._watch_exit(), name="acp-watch"),
        ]
        return connection

    @property
    def pid(self) -> int | None:
        return getattr(self._process, "pid", None)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def stderr_tail(self) -> str:
        return "".join(self._stderr_tail).strip()

    async def close(self, *, grace_seconds: float = 5.0) -> int | None:
        """Stop the child; returns its exit code when one is observable.

        ``terminate`` then ``kill``: ACP has no shutdown method, so closing the
        process is the only way to end a session, and a hung child would leak a
        process per attempt.
        """
        if not self._closed:
            self._closed = True
            self._fail_pending(
                AcpDisconnected(
                    "connection closed by host",
                    exit_code=self.exit_code,
                    stderr_tail=self.stderr_tail,
                )
            )
        process = self._process
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=max(0.1, grace_seconds))
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
        self.exit_code = process.returncode if process.returncode is not None else self.exit_code
        for task in self._tasks:
            if task is not asyncio.current_task():
                task.cancel()
        # Give the cancelled readers a chance to release the pipes; ignoring
        # their outcome here is deliberate (they may already be finished).
        await asyncio.gather(*[task for task in self._tasks if not task.done()], return_exceptions=True)
        return self.exit_code

    # ------------------------------------------------------------------ #
    # Sending
    # ------------------------------------------------------------------ #

    async def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Mapping[str, Any]:
        if self._closed:
            raise AcpDisconnected(
                "harness connection is closed",
                exit_code=self.exit_code,
                stderr_tail=self.stderr_tail,
            )
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params or {})})
            if timeout is None:
                return await future
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise AcpTimeout(f"timed out waiting for {method} after {timeout}s") from exc
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        if self._closed:
            raise AcpDisconnected(
                "harness connection is closed",
                exit_code=self.exit_code,
                stderr_tail=self.stderr_tail,
            )
        await self._write({"jsonrpc": "2.0", "method": method, "params": dict(params or {})})

    async def _write(self, message: Mapping[str, Any]) -> None:
        payload = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        stdin = self._process.stdin
        if stdin is None:
            raise AcpDisconnected("harness stdin is unavailable", exit_code=self.exit_code)
        async with self._write_lock:
            try:
                stdin.write(payload)
                await stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise AcpDisconnected(
                    f"harness stdin is closed: {type(exc).__name__}",
                    exit_code=self.exit_code,
                    stderr_tail=self.stderr_tail,
                ) from exc

    # ------------------------------------------------------------------ #
    # Receiving
    # ------------------------------------------------------------------ #

    async def _read_stdout(self) -> None:
        stdout = self._process.stdout
        assert stdout is not None
        try:
            while True:
                line = await stdout.readline()
                if not line:
                    break
                if len(line) > MAX_LINE_BYTES:
                    self._fail_pending(
                        AcpProtocolError(f"agent message exceeded {MAX_LINE_BYTES} bytes")
                    )
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except json.JSONDecodeError:
                    # A non-JSON line on stdout is a protocol violation, but
                    # killing the session over one stray line would be worse
                    # than skipping it; it is kept for diagnostics instead.
                    self._stderr_tail.append(f"[stdout] {text[:400]}\n")
                    continue
                if not isinstance(message, Mapping):
                    continue
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            self._stderr_tail.append(f"[reader] {type(exc).__name__}: {exc}\n")
        finally:
            # A closed stdout almost always means the process is exiting, but the
            # exit code is only observable after the OS reaps it. Waiting for a
            # moment here is what lets the caller report "exited with code 7"
            # instead of a bare "stream closed".
            await self._await_exit_code()
            self._mark_disconnected("harness output stream closed")

    async def _await_exit_code(self, *, timeout: float = 2.0) -> None:
        if self._process.returncode is not None:
            self.exit_code = self._process.returncode
            return
        try:
            await asyncio.wait_for(self._process.wait(), timeout=timeout)
        except (asyncio.TimeoutError, ProcessLookupError):
            return
        self.exit_code = self._process.returncode

    async def _read_stderr(self) -> None:
        stderr = self._process.stderr
        assert stderr is not None
        try:
            while True:
                line = await stderr.readline()
                if not line:
                    break
                self._stderr_tail.append(line.decode("utf-8", errors="replace"))
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive
            pass

    async def _watch_exit(self) -> None:
        await self._process.wait()
        self.exit_code = self._process.returncode
        self._mark_disconnected(f"harness exited with code {self.exit_code}")

    async def _dispatch(self, message: Mapping[str, Any]) -> None:
        message_id = message.get("id")
        method = message.get("method")
        if isinstance(message_id, int) and method is None:
            future = self._pending.pop(message_id, None)
            if future is None or future.done():
                return
            error = message.get("error")
            if isinstance(error, Mapping):
                future.set_exception(
                    AcpProtocolError(
                        str(error.get("message") or "agent error"),
                        code=error.get("code"),
                        data=error.get("data"),
                    )
                )
            else:
                future.set_result(message.get("result") or {})
            return
        if not isinstance(method, str):
            return
        params = message.get("params")
        params = dict(params) if isinstance(params, Mapping) else {}
        if message_id is None:
            await _maybe_await(self._on_notification, method, params)
            return
        await self._handle_agent_request(message_id, method, params)

    async def _handle_agent_request(self, message_id: Any, method: str, params: Mapping[str, Any]) -> None:
        try:
            result = await _maybe_await(self._on_request, method, params)
        except Exception as exc:
            await self._safe_write(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "error": {
                        "code": JSONRPC_INTERNAL_ERROR,
                        "message": f"{type(exc).__name__}: {exc}",
                    },
                }
            )
            return
        if result is None:
            await self._safe_write(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "error": {
                        "code": JSONRPC_METHOD_NOT_FOUND,
                        "message": f"unsupported client method: {method}",
                    },
                }
            )
            return
        await self._safe_write({"jsonrpc": "2.0", "id": message_id, "result": result})

    async def _safe_write(self, message: Mapping[str, Any]) -> None:
        try:
            await self._write(message)
        except AcpError:
            pass

    # ------------------------------------------------------------------ #
    # Failure propagation
    # ------------------------------------------------------------------ #

    def _mark_disconnected(self, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        self.disconnect_reason = reason
        error = AcpDisconnected(reason, exit_code=self.exit_code, stderr_tail=self.stderr_tail)
        self._fail_pending(error)
        handler = self._on_disconnect
        if handler is not None:
            try:
                result = handler(error)
                if inspect.isawaitable(result):
                    asyncio.create_task(_await_safely(result))
            except Exception:  # pragma: no cover - observer must not break teardown
                pass

    def _fail_pending(self, error: AcpError) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        self._pending.clear()


async def _maybe_await(handler: Any, *args: Any) -> Any:
    if handler is None:
        return None
    result = handler(*args)
    if inspect.isawaitable(result):
        return await result
    return result


async def _await_safely(awaitable: Awaitable[Any]) -> None:
    try:
        await awaitable
    except Exception:  # pragma: no cover - defensive
        pass


def ndjson_messages(raw: bytes | str) -> list[dict[str, Any]]:
    """Decode a raw stdio buffer into ACP messages.

    Used by tests and by the diagnostic path; production code reads line by
    line. Blank lines and unparsable lines are skipped rather than raising, so
    one malformed message cannot hide the ones around it.
    """
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    messages: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            messages.append(parsed)
    return messages


def encode_message(message: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(message), ensure_ascii=False) + "\n").encode("utf-8")


def session_update_kind(params: Mapping[str, Any] | None) -> str:
    """``session/update`` carries its discriminator one level down."""
    update = (params or {}).get("update")
    if isinstance(update, Mapping):
        return str(update.get("sessionUpdate") or "")
    return ""


def sequence_text_parts(prompt: Sequence[Mapping[str, Any]] | None) -> str:
    """Flatten ACP prompt content blocks into plain text (diagnostics only)."""
    chunks: list[str] = []
    for block in prompt or ():
        if not isinstance(block, Mapping):
            continue
        if str(block.get("type") or "") == "text":
            chunks.append(str(block.get("text") or ""))
    return "".join(chunks)
