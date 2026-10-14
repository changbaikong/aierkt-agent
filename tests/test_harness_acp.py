"""Transport-level tests for the ACP client, against the mock ACP subprocess."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from agent_core.harness_acp import (
    ACP_METHOD_INITIALIZE,
    ACP_METHOD_SESSION_NEW,
    ACP_METHOD_SESSION_PROMPT,
    AcpConnection,
    AcpDisconnected,
    AcpLaunchSpec,
    AcpProtocolError,
    AcpSpawnError,
    AcpTimeout,
    encode_message,
    ndjson_messages,
    session_update_kind,
)

MOCK_AGENT = Path(__file__).parent / "support" / "mock_acp_agent.py"


def spec(scenario: dict, *, report: Path | None = None, cwd: Path) -> AcpLaunchSpec:
    environment = {"MOCK_ACP_SCENARIO": json.dumps(scenario)}
    if report is not None:
        environment["MOCK_ACP_REPORT"] = str(report)
    return AcpLaunchSpec(
        executable=sys.executable,
        args=(str(MOCK_AGENT), "--profile", "acp"),
        cwd=str(cwd),
        env=environment,
    )


def read_report(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_initialize_and_session_new_round_trip(tmp_path: Path) -> None:
    connection = await AcpConnection.spawn(spec({"session_id": "s-1"}, cwd=tmp_path))
    try:
        handshake = await connection.request(
            ACP_METHOD_INITIALIZE, {"protocolVersion": 1, "clientCapabilities": {}}, timeout=10
        )
        assert handshake["protocolVersion"] == 1
        assert handshake["agentInfo"]["name"] == "mock-acp"
        assert "resume" in handshake["agentCapabilities"]["sessionCapabilities"]

        created = await connection.request(
            ACP_METHOD_SESSION_NEW, {"cwd": str(tmp_path), "mcpServers": []}, timeout=10
        )
        assert created["sessionId"] == "s-1"
    finally:
        await connection.close(grace_seconds=5)


@pytest.mark.asyncio
async def test_notifications_and_agent_requests_are_routed(tmp_path: Path) -> None:
    report = tmp_path / "report.jsonl"
    updates: list[tuple[str, dict]] = []
    permission_answers: list[dict] = []

    async def on_notification(method: str, params: dict) -> None:
        updates.append((method, dict(params)))

    async def on_request(method: str, params: dict) -> dict:
        assert method == "session/request_permission"
        answer = {"outcome": {"outcome": "selected", "optionId": "allow-once"}}
        permission_answers.append({"params": dict(params), "answer": answer})
        return answer

    scenario = {
        "permission_requests": 1,
        "updates": [
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}}
        ],
    }
    connection = await AcpConnection.spawn(
        spec(scenario, report=report, cwd=tmp_path),
        on_notification=on_notification,
        on_request=on_request,
    )
    try:
        await connection.request(ACP_METHOD_INITIALIZE, {"protocolVersion": 1}, timeout=10)
        created = await connection.request(
            ACP_METHOD_SESSION_NEW, {"cwd": str(tmp_path), "mcpServers": []}, timeout=10
        )
        result = await connection.request(
            ACP_METHOD_SESSION_PROMPT,
            {"sessionId": created["sessionId"], "prompt": [{"type": "text", "text": "go"}]},
            timeout=15,
        )
    finally:
        await connection.close(grace_seconds=5)

    assert result["stopReason"] == "end_turn"
    assert [item[0] for item in updates] == ["session/update"]
    assert session_update_kind(updates[0][1]) == "agent_message_chunk"
    assert permission_answers and permission_answers[0]["answer"]["outcome"]["optionId"] == "allow-once"
    reported = read_report(report)
    assert any(item["event"] == "permission" for item in reported)


@pytest.mark.asyncio
async def test_concurrent_requests_are_correlated(tmp_path: Path) -> None:
    connection = await AcpConnection.spawn(spec({}, cwd=tmp_path))
    try:
        first, second = await asyncio.gather(
            connection.request(ACP_METHOD_INITIALIZE, {"protocolVersion": 1}, timeout=10),
            connection.request(ACP_METHOD_SESSION_NEW, {"cwd": str(tmp_path), "mcpServers": []}, timeout=10),
        )
        assert first["agentInfo"]["name"] == "mock-acp"
        assert second["sessionId"]
    finally:
        await connection.close(grace_seconds=5)


@pytest.mark.asyncio
async def test_process_death_fails_pending_request_with_exit_code(tmp_path: Path) -> None:
    scenario = {
        "hang_prompt": False,
        "updates": [{"sessionUpdate": "agent_thought_chunk"}],
        "crash_after_updates": True,
        "crash_exit_code": 7,
    }
    connection = await AcpConnection.spawn(spec(scenario, cwd=tmp_path))
    try:
        await connection.request(ACP_METHOD_INITIALIZE, {"protocolVersion": 1}, timeout=10)
        created = await connection.request(
            ACP_METHOD_SESSION_NEW, {"cwd": str(tmp_path), "mcpServers": []}, timeout=10
        )
        with pytest.raises(AcpDisconnected) as caught:
            await connection.request(
                ACP_METHOD_SESSION_PROMPT,
                {"sessionId": created["sessionId"], "prompt": [{"type": "text", "text": "go"}]},
                timeout=15,
            )
        assert caught.value.exit_code == 7
    finally:
        await connection.close(grace_seconds=5)


@pytest.mark.asyncio
async def test_request_timeout_does_not_kill_connection(tmp_path: Path) -> None:
    connection = await AcpConnection.spawn(spec({"hang_prompt": True}, cwd=tmp_path))
    try:
        await connection.request(ACP_METHOD_INITIALIZE, {"protocolVersion": 1}, timeout=10)
        created = await connection.request(
            ACP_METHOD_SESSION_NEW, {"cwd": str(tmp_path), "mcpServers": []}, timeout=10
        )
        with pytest.raises(AcpTimeout):
            await connection.request(
                ACP_METHOD_SESSION_PROMPT,
                {"sessionId": created["sessionId"], "prompt": [{"type": "text", "text": "go"}]},
                timeout=1,
            )
        # Still usable afterwards: a timeout is not a disconnect.
        assert (
            await connection.request("session/close", {"sessionId": created["sessionId"]}, timeout=10)
            == {}
        )
    finally:
        await connection.close(grace_seconds=5)


@pytest.mark.asyncio
async def test_unknown_method_returns_protocol_error(tmp_path: Path) -> None:
    connection = await AcpConnection.spawn(spec({}, cwd=tmp_path))
    try:
        with pytest.raises(AcpProtocolError) as caught:
            await connection.request("session/does_not_exist", {}, timeout=10)
        assert caught.value.code == -32601
    finally:
        await connection.close(grace_seconds=5)


@pytest.mark.asyncio
async def test_missing_executable_is_reported_before_spawn(tmp_path: Path) -> None:
    with pytest.raises(AcpSpawnError):
        await AcpConnection.spawn(
            AcpLaunchSpec(executable=str(tmp_path / "nope.exe"), args=(), cwd=str(tmp_path), env={})
        )


@pytest.mark.asyncio
async def test_cancel_notification_reaches_agent(tmp_path: Path) -> None:
    report = tmp_path / "report.jsonl"
    connection = await AcpConnection.spawn(spec({"hang_prompt": True}, report=report, cwd=tmp_path))
    try:
        await connection.request(ACP_METHOD_INITIALIZE, {"protocolVersion": 1}, timeout=10)
        created = await connection.request(
            ACP_METHOD_SESSION_NEW, {"cwd": str(tmp_path), "mcpServers": []}, timeout=10
        )
        pending = asyncio.ensure_future(
            connection.request(
                ACP_METHOD_SESSION_PROMPT,
                {"sessionId": created["sessionId"], "prompt": [{"type": "text", "text": "go"}]},
                timeout=15,
            )
        )
        await asyncio.sleep(0.4)
        await connection.notify("session/cancel", {"sessionId": created["sessionId"]})
        result = await pending
        assert result["stopReason"] == "cancelled"
    finally:
        await connection.close(grace_seconds=5)
    assert any(item["event"] == "cancel" for item in read_report(report))


def test_ndjson_helpers_skip_noise() -> None:
    payload = encode_message({"jsonrpc": "2.0", "id": 1, "result": {}})
    assert payload.endswith(b"\n")
    messages = ndjson_messages(b"\nnot json\n" + payload + b'{"jsonrpc":"2.0","method":"session/update"}\n')
    assert [item.get("id") or item.get("method") for item in messages] == [1, "session/update"]
    assert session_update_kind({"update": {"sessionUpdate": "tool_call"}}) == "tool_call"
    assert session_update_kind({}) == ""
