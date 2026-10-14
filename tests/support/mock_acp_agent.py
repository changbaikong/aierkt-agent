"""A mock ACP agent used by the Harness bridge tests.

It is a real subprocess speaking real ACP over stdio (one JSON object per line),
because the thing under test is the wire: framing, request/response correlation,
notifications, agent→client requests, cancellation and process death. An
in-process fake would silently paper over all of those.

Control is by environment:

``MOCK_ACP_SCENARIO``
    JSON object, see :data:`DEFAULT_SCENARIO` for the keys.
``MOCK_ACP_REPORT``
    File the mock appends observations to (permission answers, cancel
    notifications, prompts). Tests read it to assert what the bridge *sent*.

Scenarios are intentionally small and explicit; a big state machine would be
harder to trust than the bridge it verifies.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

DEFAULT_SCENARIO = {
    # capability advertisement
    "protocol_version": 1,
    "session_capabilities": {"resume": {}, "close": {}, "list": {}},
    # behaviour
    "stop_reason": "end_turn",
    "updates": [],
    "write_files": {},
    "permission_requests": 0,
    "permission_options": [
        {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
        {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
    ],
    "ignore_cancel": False,
    "hang_prompt": False,
    "crash_after_updates": False,
    "crash_exit_code": 7,
    "resume_fails": False,
    "session_id": "mock-session-1",
    "unknown_method": False,
    "emit_unknown_update": False,
}

_write_lock = threading.Lock()

#: ACP is UTF-8 on stdio. Python's default stdio codec on Windows is the locale
#: codec (cp936 here), so the mock must go through the binary buffers: a session
#: cwd with CJK characters otherwise arrives as undecodable bytes and the
#: request silently vanishes, which looks exactly like a bridge bug.
_STDIN = sys.stdin.buffer
_STDOUT = sys.stdout.buffer


def _scenario() -> dict:
    raw = os.environ.get("MOCK_ACP_SCENARIO") or "{}"
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = {}
    merged = dict(DEFAULT_SCENARIO)
    merged.update(payload if isinstance(payload, dict) else {})
    return merged


def _send(message: dict) -> None:
    payload = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
    with _write_lock:
        _STDOUT.write(payload)
        _STDOUT.flush()


def _report(entry: dict) -> None:
    path = os.environ.get("MOCK_ACP_REPORT")
    if not path:
        return
    with _write_lock:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _expand(value):
    """Replace ``__CWD__`` in scenario strings with the session workspace."""
    if isinstance(value, str):
        return value.replace("__CWD__", os.getcwd())
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item) for item in value]
    return value


class MockAgent:
    def __init__(self, scenario: dict) -> None:
        self.scenario = scenario
        self.next_id = 1000
        self.pending: dict[int, tuple[str, threading.Event]] = {}
        self.responses: dict[int, dict] = {}
        self.lock = threading.Lock()
        self.cancelled: set[str] = set()
        self.sessions: dict[str, dict] = {}

    # -- request/response ------------------------------------------------- #

    def request(self, method: str, params: dict, timeout: float = 20.0) -> dict:
        with self.lock:
            self.next_id += 1
            request_id = self.next_id
            event = threading.Event()
            self.pending[request_id] = (method, event)
        _send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        event.wait(timeout)
        with self.lock:
            self.pending.pop(request_id, None)
            return self.responses.pop(request_id, {"error": {"message": "no response"}})

    def notify(self, method: str, params: dict) -> None:
        _send({"jsonrpc": "2.0", "method": method, "params": params})

    def _resolve(self, message: dict) -> None:
        request_id = message.get("id")
        with self.lock:
            entry = self.pending.get(request_id)
        if entry is None:
            return
        self.responses[request_id] = message
        entry[1].set()

    # -- prompt handling -------------------------------------------------- #

    def _finish_prompt(self, request_id: int, params: dict) -> None:
        result = self.handle_prompt(params)
        _send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def handle_prompt(self, params: dict) -> dict:
        session_id = str(params.get("sessionId") or "")
        prompt = params.get("prompt") or []
        _report({"event": "prompt", "session_id": session_id, "prompt": prompt})
        if self.scenario.get("hang_prompt"):
            while session_id not in self.cancelled:
                time.sleep(0.05)
            return {"stopReason": "cancelled"}
        for update in self.scenario.get("updates") or []:
            self.notify("session/update", {"sessionId": session_id, "update": update})
            time.sleep(0.01)
        if self.scenario.get("emit_unknown_update"):
            self.notify(
                "session/update",
                {"sessionId": session_id, "update": {"sessionUpdate": "brand_new_thing", "x": 1}},
            )
        if self.scenario.get("unknown_method"):
            return {"stopReason": "end_turn"}
        for _ in range(int(self.scenario.get("permission_requests") or 0)):
            answer = self.request(
                "session/request_permission",
                {
                    "sessionId": session_id,
                    "toolCall": {
                        "toolCallId": "tool-1",
                        "title": self.scenario.get("permission_tool_title") or "Write file",
                        "kind": "other",
                        # ``__CWD__`` lets a scenario name a path inside the
                        # session workspace without the test knowing it up front.
                        "rawInput": _expand(self.scenario.get("permission_raw_input") or {}),
                    },
                    "options": self.scenario.get("permission_options"),
                },
            )
            _report({"event": "permission", "answer": answer})
            if (answer.get("result") or {}).get("outcome", {}).get("outcome") == "cancelled":
                return {"stopReason": "cancelled"}
        self._write_files()
        if self.scenario.get("crash_after_updates"):
            sys.stdout.flush()
            os._exit(int(self.scenario.get("crash_exit_code") or 7))
        return {"stopReason": str(self.scenario.get("stop_reason") or "end_turn")}

    def _write_files(self) -> None:
        files = self.scenario.get("write_files") or {}
        cwd = os.getcwd()
        for relative, content in files.items():
            target = os.path.join(cwd, str(relative))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(str(content))
            _report({"event": "wrote", "path": target})

    # -- main loop -------------------------------------------------------- #

    def run(self) -> None:
        for raw in _STDIN:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            _report({"event": "received", "method": message.get("method")})
            method = message.get("method")
            if method is None and "id" in message:
                self._resolve(message)
                continue
            if method == "session/cancel":
                session_id = str((message.get("params") or {}).get("sessionId") or "")
                _report({"event": "cancel", "session_id": session_id})
                if not self.scenario.get("ignore_cancel"):
                    self.cancelled.add(session_id)
                    _send(
                        {
                            "jsonrpc": "2.0",
                            "method": "session/update",
                            "params": {
                                "sessionId": session_id,
                                "update": {
                                    "sessionUpdate": "agent_message_chunk",
                                    "content": {"type": "text", "text": "[cancelled]"},
                                },
                            },
                        }
                    )
                continue
            if method == "initialize":
                _send({"jsonrpc": "2.0", "id": message["id"], "result": self.initialize_result()})
                continue
            if method == "session/new":
                session_id = str(self.scenario.get("session_id") or "mock-session-1")
                self.sessions[session_id] = {"cwd": (message.get("params") or {}).get("cwd")}
                _report({"event": "session_new", "session_id": session_id, "params": message.get("params")})
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": {"sessionId": session_id, "configOptions": []},
                    }
                )
                continue
            if method in {"session/resume", "session/load"}:
                params = message.get("params") or {}
                if self.scenario.get("resume_fails"):
                    _send(
                        {
                            "jsonrpc": "2.0",
                            "id": message["id"],
                            "error": {"code": -32602, "message": "session is not resumable"},
                        }
                    )
                else:
                    _report({"event": "session_resume", "params": params})
                    session_id = str(params.get("sessionId") or "")
                    self.sessions.setdefault(session_id, {"cwd": params.get("cwd")})
                    _send({"jsonrpc": "2.0", "id": message["id"], "result": {"configOptions": []}})
                continue
            if method == "session/close":
                _report({"event": "session_close", "params": message.get("params")})
                _send({"jsonrpc": "2.0", "id": message["id"], "result": {}})
                continue
            if method == "session/prompt":
                params = message.get("params") or {}
                request_id = message["id"]
                # A prompt turn must run off the reader thread: the mock asks the
                # client for permission mid-turn and must keep reading stdin for
                # the answer (and for session/cancel) while it waits.
                threading.Thread(
                    target=self._finish_prompt,
                    args=(request_id, params),
                    daemon=True,
                ).start()
                continue
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "error": {"code": -32601, "message": f"method not found: {method}"},
                }
            )

    def initialize_result(self) -> dict:
        return {
            "protocolVersion": int(self.scenario.get("protocol_version") or 1),
            "agentInfo": {"name": "mock-acp", "version": "9.9.9"},
            "agentCapabilities": {
                "mcpCapabilities": {"http": False},
                "promptCapabilities": {"image": False},
                "sessionCapabilities": dict(self.scenario.get("session_capabilities") or {}),
            },
            "authMethods": [],
        }


def main() -> int:
    scenario = _scenario()
    agent = MockAgent(scenario)
    if scenario.get("no_stdout"):
        # Emulates a program that is not an ACP agent at all.
        time.sleep(float(scenario.get("no_stdout_seconds") or 5))
        return 0
    agent.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
