from __future__ import annotations

import json

import httpx
import pytest

from agent_core.legacy_code_agent.code_agent_contracts import CodeAgentBudget
from agent_core.legacy_code_agent.code_agent_loop import CodeAgentLoop
from agent_core.contracts import ActionObservation, ProviderResponse, Scope, ToolSpec
from agent_core.work_provider import WorkRequest


class Model:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, messages, *, tools=None, max_tokens=None, temperature=None):
        self.calls.append({"messages": list(messages), "tools": tools, "max_tokens": max_tokens})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class Gateway:
    def __init__(self, observations):
        self.observations = list(observations)
        self.requests = []
        self.resumes = []

    async def invoke(self, request):
        self.requests.append(request)
        return self.observations.pop(0)

    async def resume(self, approval_id, decision, *, request):
        self.resumes.append((approval_id, decision, request))
        return self.observations.pop(0)


def tool_spec(name="code__read"):
    return ToolSpec(
        name,
        {
            "type": "function",
            "function": {
                "name": name,
                "parameters": {"type": "object", "properties": {}},
            },
        },
        lambda arguments, scope: {},
        effect="read",
        allowed_origins=frozenset({"code_agent"}),
    )


def request():
    return WorkRequest(
        work_item_id="work-1",
        attempt_id="attempt-1",
        scope=Scope(owner_id="alice", channel="web", conversation_id="chat-1"),
        objective="修复失败测试",
        task_type="code.agent",
        input={"project_root": "C:/repo"},
    )


def response(message, tokens=10):
    return ProviderResponse(message=message, usage={"total_tokens": tokens})


@pytest.mark.asyncio
async def test_loop_returns_final_text_without_tools() -> None:
    model = Model([response({"role": "assistant", "content": "完成"})])
    loop = CodeAgentLoop(model, Gateway([]), [tool_spec()], CodeAgentBudget(max_steps=3))

    outcome = await loop.run(request(), type("Snapshot", (), {"snapshot_id": "snap-1"})())

    assert outcome.status == "completed"
    assert outcome.summary == "完成"
    assert outcome.verification_status == "not_run"


@pytest.mark.asyncio
async def test_loop_retries_httpx_connect_error() -> None:
    model = Model([httpx.ConnectError("DNS lookup failed"), response({"role": "assistant", "content": "完成"})])
    loop = CodeAgentLoop(model, Gateway([]), [tool_spec()], CodeAgentBudget(max_steps=3))

    outcome = await loop.run(request(), type("Snapshot", (), {"snapshot_id": "snap-1"})())

    assert outcome.status == "completed"
    assert len(model.calls) == 2


@pytest.mark.asyncio
async def test_loop_executes_tool_and_returns_observation_to_model() -> None:
    model = Model(
        [
            response(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "code__read", "arguments": json.dumps({"path": "a.py"})},
                        }
                    ],
                }
            ),
            response({"role": "assistant", "content": "已读取"}),
        ]
    )
    gateway = Gateway(
        [
            ActionObservation(
                action_id="a1",
                name="code__read",
                status="completed",
                output={"text": "x = 1"},
                origin="code_agent",
            )
        ]
    )
    loop = CodeAgentLoop(model, gateway, [tool_spec()], CodeAgentBudget(max_steps=4))

    outcome = await loop.run(request(), type("Snapshot", (), {"snapshot_id": "snap-1"})())

    assert outcome.status == "completed"
    assert gateway.requests[0].name == "code__read"
    second_messages = model.calls[1]["messages"]
    assert second_messages[-1]["role"] == "tool"
    assert second_messages[-1]["tool_call_id"] == "call-1"
    assert "x = 1" in second_messages[-1]["content"]


@pytest.mark.asyncio
async def test_loop_pauses_with_original_action_checkpoint() -> None:
    model = Model(
        [
            response(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-risk",
                            "type": "function",
                            "function": {
                                "name": "code__run",
                                "arguments": json.dumps({"runtime": "python", "args": ["danger.py"]}),
                            },
                        }
                    ],
                }
            )
        ]
    )
    gateway = Gateway(
        [
            ActionObservation(
                action_id="action-risk",
                name="code__run",
                status="waiting_approval",
                approval_id="approval-1",
                awaiting_approval=True,
                origin="code_agent",
            )
        ]
    )
    loop = CodeAgentLoop(model, gateway, [tool_spec("code__run")], CodeAgentBudget(max_steps=4))

    outcome = await loop.run(request(), type("Snapshot", (), {"snapshot_id": "snap-1"})())

    assert outcome.status == "waiting_approval"
    assert outcome.checkpoint is not None
    assert outcome.checkpoint.snapshot_id == "snap-1"
    assert outcome.checkpoint.original_action["name"] == "code__run"
    assert outcome.checkpoint.original_action["arguments"] == {
        "runtime": "python",
        "args": ["danger.py"],
    }
    assert outcome.output["approval_id"] == "approval-1"


@pytest.mark.asyncio
async def test_loop_resume_uses_checkpoint_action_and_continues() -> None:
    paused_model = Model(
        [
            response(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-risk",
                            "type": "function",
                            "function": {"name": "code__run", "arguments": "{}"},
                        }
                    ],
                }
            )
        ]
    )
    gateway = Gateway(
        [
            ActionObservation(
                action_id="action-risk",
                name="code__run",
                status="waiting_approval",
                approval_id="approval-1",
                awaiting_approval=True,
                origin="code_agent",
            )
        ]
    )
    first = CodeAgentLoop(paused_model, gateway, [tool_spec("code__run")], CodeAgentBudget())
    paused = await first.run(request(), type("Snapshot", (), {"snapshot_id": "snap-1"})())

    gateway.observations.append(
        ActionObservation(
            action_id="action-risk",
            name="code__run",
            status="completed",
            output={"exit_code": 0},
            origin="code_agent",
        )
    )
    resumed_model = Model([response({"role": "assistant", "content": "测试通过"})])
    resumed = CodeAgentLoop(resumed_model, gateway, [tool_spec("code__run")], CodeAgentBudget())
    outcome = await resumed.resume(paused.checkpoint, "confirmed")

    assert outcome.status == "completed"
    assert outcome.verification_status == "unverified"
    assert gateway.resumes[0][0:2] == ("approval-1", "confirmed")
    assert gateway.resumes[0][2].arguments == {}


@pytest.mark.asyncio
async def test_loop_does_not_trust_unobserved_test_claim() -> None:
    model = Model([response({"role": "assistant", "content": "所有测试通过"})])
    loop = CodeAgentLoop(model, Gateway([]), [tool_spec("code__run")], CodeAgentBudget())

    outcome = await loop.run(request(), type("Snapshot", (), {"snapshot_id": "snap-1"})())

    assert outcome.status == "completed"
    assert outcome.verification_status == "unverified"


@pytest.mark.asyncio
async def test_loop_does_not_call_an_arbitrary_successful_command_verification() -> None:
    model = Model([
        response(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "run-1",
                    "type": "function",
                    "function": {"name": "code__run", "arguments": "{}"},
                }],
            }
        ),
        response({"role": "assistant", "content": "验证通过"}),
    ])
    gateway = Gateway([
        ActionObservation(
            action_id="run-1",
            name="code__run",
            status="completed",
            output={"exit_code": 0},
            origin="code_agent",
        )
    ])
    loop = CodeAgentLoop(model, gateway, [tool_spec("code__run")], CodeAgentBudget())

    outcome = await loop.run(request(), type("Snapshot", (), {"snapshot_id": "snap-1"})())

    assert outcome.verification_status == "unverified"


@pytest.mark.asyncio
async def test_loop_invalidates_verification_when_snapshot_changes_after_successful_run() -> None:
    model = Model([
        response(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "run-1",
                    "type": "function",
                    "function": {"name": "code__run", "arguments": "{}"},
                }],
            }
        ),
        response(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "write-1",
                    "type": "function",
                    "function": {"name": "code__write", "arguments": "{}"},
                }],
            }
        ),
        response({"role": "assistant", "content": "测试通过"}),
    ])
    gateway = Gateway([
        ActionObservation("run-1", "code__run", "completed", output={"exit_code": 0}, origin="code_agent"),
        ActionObservation("write-1", "code__write", "completed", output={}, origin="code_agent"),
    ])
    loop = CodeAgentLoop(model, gateway, [tool_spec("code__run"), tool_spec("code__write")], CodeAgentBudget())

    outcome = await loop.run(request(), type("Snapshot", (), {"snapshot_id": "snap-1"})())

    assert outcome.verification_status == "unverified"


@pytest.mark.asyncio
async def test_loop_stops_at_step_budget() -> None:
    calls = [
        response(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call-{index}",
                        "type": "function",
                        "function": {"name": "code__read", "arguments": "{}"},
                    }
                ],
            }
        )
        for index in range(3)
    ]
    gateway = Gateway(
        [
            ActionObservation(
                action_id=f"a-{index}",
                name="code__read",
                status="completed",
                output={},
                origin="code_agent",
            )
            for index in range(3)
        ]
    )
    loop = CodeAgentLoop(Model(calls), gateway, [tool_spec()], CodeAgentBudget(max_steps=2))

    outcome = await loop.run(request(), type("Snapshot", (), {"snapshot_id": "snap-1"})())

    assert outcome.status == "budget_exhausted"
    assert "step" in (outcome.error or "").lower()
