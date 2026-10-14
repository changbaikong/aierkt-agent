"""Bounded model/tool loop used only by background code tasks."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict
from typing import Any, Mapping

from .code_agent_contracts import CodeAgentBudget, CodeAgentCheckpoint, CodeAgentOutcome
from ..contracts import ActionObservation, ActionRequest, Scope, ToolSpec
from ..work_provider import WorkRequest


_TEST_CLAIMS = ("tests pass", "test passed", "all tests pass", "测试通过", "所有测试通过", "全部通过")


class CodeAgentLoop:
    def __init__(
        self,
        model: Any,
        gateway: Any,
        tools: list[ToolSpec],
        budget: CodeAgentBudget,
    ) -> None:
        self.model = model
        self.gateway = gateway
        self.tools = tuple(tools)
        self.budget = budget
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    async def run(self, request: WorkRequest, snapshot: Any) -> CodeAgentOutcome:
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a bounded code agent. Work only inside the provided snapshot. "
                    "Use tools for every read, write, command, knowledge, or network action. "
                    "A successful code__run does not certify that tests passed. "
                    "Return a concise final summary when finished."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "objective": request.objective,
                        "snapshot_id": snapshot.snapshot_id,
                        "project_root_label": str(request.task_input.get("project_root") or ""),
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        return await self._drive(
            messages,
            scope=request.scope,
            attempt_id=request.attempt_id,
            snapshot_id=snapshot.snapshot_id,
            steps_used=0,
            tokens_used=0,
            evidence=[],
        )

    async def resume(
        self,
        checkpoint: CodeAgentCheckpoint | None,
        decision: str,
    ) -> CodeAgentOutcome:
        if checkpoint is None:
            return CodeAgentOutcome("failed", error="checkpoint is required")
        action = dict(checkpoint.original_action)
        try:
            scope = Scope(**dict(action["scope"]))
            request = ActionRequest(
                action_id=str(action["action_id"]),
                name=str(action["name"]),
                arguments=dict(action["arguments"]),
                scope=scope,
                origin="code_agent",
                run_id=str(action.get("run_id") or ""),
                turn_id=str(action.get("turn_id") or ""),
                tool_call_id=str(action["call_id"]),
            )
            approval_id = str(action["approval_id"])
        except Exception as exc:
            return CodeAgentOutcome("failed", error=f"invalid checkpoint action: {exc}")
        observation = await self.gateway.resume(approval_id, decision, request=request)
        messages = [dict(item) for item in checkpoint.messages]
        messages.append(self._tool_message(request.tool_call_id, observation))
        evidence = [self._evidence(observation)]
        return await self._drive(
            messages,
            scope=scope,
            attempt_id=str(action.get("turn_id") or ""),
            snapshot_id=checkpoint.snapshot_id,
            steps_used=checkpoint.steps_used,
            tokens_used=checkpoint.tokens_used,
            evidence=evidence,
        )

    async def _drive(
        self,
        messages: list[dict[str, Any]],
        *,
        scope: Scope,
        attempt_id: str,
        snapshot_id: str,
        steps_used: int,
        tokens_used: int,
        evidence: list[Mapping[str, Any]],
    ) -> CodeAgentOutcome:
        deadline = time.monotonic() + self.budget.timeout_seconds
        tool_schemas = [dict(spec.schema) for spec in self.tools]
        while True:
            if self._cancelled:
                return CodeAgentOutcome(
                    "cancelled",
                    evidence=tuple(evidence),
                    error="code agent cancelled",
                )
            if steps_used >= self.budget.max_steps:
                return CodeAgentOutcome(
                    "budget_exhausted",
                    evidence=tuple(evidence),
                    error="step budget exhausted",
                )
            if tokens_used >= self.budget.max_tokens:
                return CodeAgentOutcome(
                    "budget_exhausted",
                    evidence=tuple(evidence),
                    error="token budget exhausted",
                )
            if time.monotonic() >= deadline:
                return CodeAgentOutcome(
                    "budget_exhausted",
                    evidence=tuple(evidence),
                    error="time budget exhausted",
                )

            remaining_tokens = max(1, self.budget.max_tokens - tokens_used)
            try:
                response = await self._complete(
                    messages,
                    tools=tool_schemas,
                    max_tokens=remaining_tokens,
                )
            except Exception as exc:
                return CodeAgentOutcome(
                    "failed",
                    evidence=tuple(evidence),
                    error=f"model call failed: {type(exc).__name__}: {exc}",
                )
            steps_used += 1
            tokens_used += self._usage_tokens(response.usage)
            message = dict(response.message or {})
            message.setdefault("role", "assistant")
            calls = message.get("tool_calls") or []
            messages.append(message)
            if not calls:
                summary = str(message.get("content") or "").strip()
                verification = self._verification_status(summary, messages, evidence)
                return CodeAgentOutcome(
                    "completed",
                    summary=summary,
                    evidence=tuple(evidence),
                    verification_status=verification,
                    output={
                        "steps_used": steps_used,
                        "tokens_used": tokens_used,
                        "verification_status": verification,
                    },
                )

            for raw_call in calls:
                call_id = ""
                try:
                    call_id = str(raw_call["id"])
                    function = raw_call["function"]
                    name = str(function["name"])
                    raw_arguments = function.get("arguments") or "{}"
                    arguments = (
                        json.loads(raw_arguments)
                        if isinstance(raw_arguments, str)
                        else dict(raw_arguments)
                    )
                    if not isinstance(arguments, dict):
                        raise ValueError("tool arguments must be an object")
                    action_request = ActionRequest(
                        action_id=f"code-{attempt_id}-{steps_used}-{call_id}",
                        name=name,
                        arguments=arguments,
                        scope=scope,
                        origin="code_agent",
                        run_id=attempt_id,
                        turn_id=attempt_id,
                        tool_call_id=call_id,
                    )
                except Exception as exc:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": json.dumps(
                                {
                                    "status": "failed",
                                    "error": f"invalid tool call: {type(exc).__name__}: {exc}",
                                },
                                ensure_ascii=False,
                            ),
                        }
                    )
                    continue

                observation = await self.gateway.invoke(action_request)
                evidence.append(self._evidence(observation))
                if observation.status == "waiting_approval":
                    checkpoint = CodeAgentCheckpoint(
                        snapshot_id=snapshot_id,
                        messages=tuple(dict(item) for item in messages),
                        steps_used=steps_used,
                        tokens_used=tokens_used,
                        original_action={
                            "approval_id": observation.approval_id,
                            "action_id": action_request.action_id,
                            "call_id": call_id,
                            "name": name,
                            "arguments": dict(arguments),
                            "scope": asdict(scope),
                            "run_id": action_request.run_id,
                            "turn_id": action_request.turn_id,
                        },
                    )
                    return CodeAgentOutcome(
                        "waiting_approval",
                        checkpoint=checkpoint,
                        evidence=tuple(evidence),
                        output={"approval_id": observation.approval_id},
                    )
                messages.append(self._tool_message(call_id, observation))

    async def _complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                return await self.model.complete(messages, temperature=0.1, **kwargs)
            except Exception as exc:
                if not (
                    isinstance(exc, (TimeoutError, asyncio.TimeoutError, ConnectionError))
                    or type(exc).__name__ in {
                        "ConnectError",
                        "ConnectTimeout",
                        "PoolTimeout",
                        "ReadError",
                        "WriteError",
                        "RemoteProtocolError",
                    }
                ):
                    raise
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0)
        if last_error is not None:
            raise last_error
        raise RuntimeError("model completion failed")

    @staticmethod
    def _usage_tokens(usage: Mapping[str, Any]) -> int:
        if "total_tokens" in usage:
            return max(0, int(usage.get("total_tokens") or 0))
        return max(
            0,
            int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
            + int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
        )

    @staticmethod
    def _evidence(observation: ActionObservation) -> dict[str, Any]:
        return {
            "action_id": observation.action_id,
            "name": observation.name,
            "status": observation.status,
            "output": dict(observation.output or {}),
            "error": observation.error,
            "error_kind": observation.error_kind,
            "approval_id": observation.approval_id,
        }

    @classmethod
    def _tool_message(cls, call_id: str, observation: ActionObservation) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(cls._evidence(observation), ensure_ascii=False),
        }

    @staticmethod
    def _verification_status(
        summary: str,
        messages: list[dict[str, Any]],
        evidence: list[Mapping[str, Any]],
    ) -> str:
        successful_run = any(
            item.get("name") == "code__run"
            and item.get("status") == "completed"
            and (item.get("output") or {}).get("exit_code") == 0
            for item in evidence
        )
        if not successful_run:
            for message in messages:
                if message.get("role") != "tool":
                    continue
                try:
                    item = json.loads(str(message.get("content") or "{}"))
                except json.JSONDecodeError:
                    continue
                if (
                    item.get("name") == "code__run"
                    and item.get("status") == "completed"
                    and (item.get("output") or {}).get("exit_code") == 0
                ):
                    successful_run = True
                    break
        lowered = summary.casefold()
        if successful_run or any(claim.casefold() in lowered for claim in _TEST_CLAIMS):
            return "unverified"
        return "not_run"
