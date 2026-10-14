import asyncio
import time

import pytest

from agent_core.contracts import Scope
from agent_core.control_plan import PlanValidationError, validate_plan_definition
from agent_core.work_host import WorkHost
from agent_core.work_provider import (
    WORK_TASK_TYPES,
    WorkArtifact,
    WorkProviderCapabilities,
    WorkResult,
)


class PlanProvider:
    """Deterministic provider covering the three acceptance steps."""

    capabilities = WorkProviderCapabilities(
        "plan-test", tuple(sorted(WORK_TASK_TYPES)), supports_cancel=True, supports_resume=False
    )

    def __init__(self, *, artifact_dir, fail_types=(), gates=None, moves=None):
        self.artifact_dir = artifact_dir
        self.fail_types = set(fail_types)
        self.gates = dict(gates or {})
        self.moves = list(moves or [{"source": "a.txt", "destination": "txt/a.txt"}])
        self.applied = []
        self.calls = []
        self.cancelled = set()

    async def cancel(self, attempt_id):
        self.cancelled.add(attempt_id)
        return True

    async def resume(self, request, checkpoint, decision, emit):
        return WorkResult("interrupted", error="plan-test provider never resumes in place")

    async def execute(self, request, emit):
        self.calls.append((request.task_type, dict(request.task_input)))
        gate = self.gates.get(request.task_type)
        if gate is not None:
            await gate.wait()
        if request.attempt_id in self.cancelled:
            return WorkResult("cancelled", error="cancelled before execution")
        if request.task_type in self.fail_types:
            return WorkResult("failed", error=f"boom:{request.task_type}")
        task_input = dict(request.task_input)
        if (
            request.task_type in {"screen.capture", "system.volume.adjust"}
            and task_input.get("_requires_approval")
            and not request.approval_granted
        ):
            return WorkResult("waiting_approval", {"reason": "explicit approval required"})

        if request.task_type == "file.list":
            directory = task_input.get("directory")
            return WorkResult(
                "completed",
                {"directory": directory, "total": 2, "files": ["a.txt", "b.txt"]},
            )
        if request.task_type == "file.organize":
            directory = task_input.get("directory")
            if request.approval_manifest:
                moves = [dict(item) for item in request.approval_manifest]
                self.applied.extend(moves)
                return WorkResult("completed", {"directory": directory, "applied": moves, "status": "completed"})
            moves = [{"source": f"{directory}/{item['source']}", "destination": f"{directory}/{item['destination']}"} for item in self.moves]
            return WorkResult("waiting_approval", {"directory": directory, "moves": moves, "total": len(moves)})
        if request.task_type == "screen.capture":
            path = self.artifact_dir / "shot.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\x89PNG\r\n\x1a\nplan")
            artifact = WorkArtifact(
                artifact_id="shot.png",
                attempt_id=request.attempt_id,
                media_type="image/png",
                size=path.stat().st_size,
                path=str(path),
                expires_at="2999-01-01T00:00:00+00:00",
            )
            return WorkResult(
                "completed",
                {"artifact_path": artifact.path, "media_type": "image/png", "size": artifact.size},
                artifacts=[artifact],
            )
        if request.task_type == "system.volume.adjust":
            return WorkResult("completed", {"percent": task_input.get("percent"), "status": "ok"})
        return WorkResult("failed", error=f"unsupported task_type: {request.task_type}")


def _scope(owner="alice", conversation="chat"):
    return Scope(owner, "web", conversation, "work")


async def _wait_for(predicate, *, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.01)
    return predicate()


def _plan_steps():
    return [
        {
            "step_id": "organize",
            "task_type": "file.organize",
            "objective": "按扩展名整理目录",
            "input": {"directory": "D:/Downloads"},
            "requires_approval": True,
        },
        {
            "step_id": "capture",
            "task_type": "screen.capture",
            "objective": "整理完成后截图",
            "input": {},
            "depends_on": ["organize"],
        },
        {
            "step_id": "volume",
            "task_type": "system.volume.adjust",
            "objective": "调低音量",
            "input": {"percent": -10},
            "depends_on": ["capture"],
        },
    ]


def test_submit_plan_persists_whole_plan_and_only_starts_independent_steps(tmp_path):
    async def scenario():
        gate = asyncio.Event()
        host = WorkHost(
            tmp_path / "work.db",
            provider=PlanProvider(artifact_dir=tmp_path, gates={"file.list": gate}),
            max_workers=2,
        )
        await host.start()
        try:
            scope = _scope()
            result = await host.submit_plan(
                scope,
                "先列出目录再截图",
                [
                    {"step_id": "list", "task_type": "file.list", "objective": "列出目录", "input": {"directory": "D:/Downloads"}},
                    {"step_id": "shot", "task_type": "screen.capture", "objective": "截图", "depends_on": ["list"]},
                ],
            )
            assert result["status"] == "queued"
            assert result["step_count"] == 2
            assert result["ready_steps"] == ["list"]
            detail = host.get_plan(result["plan_id"], scope)
            assert [step["status"] for step in detail["steps"]] == ["ready", "pending"]
            assert detail["steps"][1]["depends_on"] == ["list"]
        finally:
            gate.set()
            await host.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "steps, message",
    [
        ([{"step_id": "x", "task_type": "shell.exec", "objective": "跑命令"}], "unknown task_type"),
        (
            [{"step_id": "x", "task_type": "file.list", "objective": "列出", "depends_on": ["ghost"]}],
            "unknown step",
        ),
        (
            [
                {"step_id": "a", "task_type": "file.list", "objective": "a", "depends_on": ["b"]},
                {"step_id": "b", "task_type": "file.list", "objective": "b", "depends_on": ["a"]},
            ],
            "cycle",
        ),
        (
            [
                {
                    "step_id": "a",
                    "task_type": "file.organize",
                    "objective": "整理",
                    "input": {"approved_moves": [{"source": "a", "destination": "b"}]},
                }
            ],
            "Host-owned",
        ),
        (
            [
                {
                    "step_id": "a",
                    "task_type": "file.list",
                    "objective": "列出",
                    "input": {"authorization": "granted"},
                }
            ],
            "Host-owned",
        ),
        (
            [
                {
                    "step_id": "a",
                    "task_type": "file.list",
                    "objective": "列出",
                    "input": {"directory": "D:/", "allowed_roots": "C:/"},
                }
            ],
            "scope",
        ),
        (
            [
                {
                    "step_id": "a",
                    "task_type": "file.list",
                    "objective": "列出",
                    "input": {"directory": "D:/"},
                },
                {
                    "step_id": "b",
                    "task_type": "file.list",
                    "objective": "越权引用",
                    "input": {"directory": "${step:a.output.directory}"},
                },
            ],
            "depends_on",
        ),
        (
            [{"step_id": "a", "task_type": "file.list", "objective": "列出", "failure_policy": "explode"}],
            "failure_policy",
        ),
    ],
)
def test_plan_submission_is_rejected_before_any_persistence(tmp_path, steps, message):
    async def scenario():
        provider = PlanProvider(artifact_dir=tmp_path)
        host = WorkHost(tmp_path / "work.db", provider=provider, max_workers=1)
        await host.start()
        try:
            with pytest.raises(PlanValidationError) as error:
                await host.submit_plan(_scope(), "候选计划", steps)
            assert message in str(error.value)
            assert host.list_plans(_scope()) == []
            assert provider.calls == []
        finally:
            await host.stop()

    asyncio.run(scenario())


def test_plan_rejects_work_roots_tampering_and_duplicate_step_ids(tmp_path):
    async def scenario():
        host = WorkHost(tmp_path / "work.db", provider=PlanProvider(artifact_dir=tmp_path), max_workers=1)
        await host.start()
        try:
            with pytest.raises(PlanValidationError, match="AGENT_WORK_ROOTS"):
                await host.submit_plan(
                    _scope(),
                    "越权",
                    [
                        {
                            "step_id": "a",
                            "task_type": "file.list",
                            "objective": "列出",
                            "input": {"directory": "D:/", "env": {"AGENT_WORK_ROOTS": "C:/"}},
                        }
                    ],
                )
            with pytest.raises(PlanValidationError, match="duplicate step_id"):
                await host.submit_plan(
                    _scope(),
                    "重复",
                    [
                        {"step_id": "a", "task_type": "file.list", "objective": "一"},
                        {"step_id": "a", "task_type": "file.list", "objective": "二"},
                    ],
                )
        finally:
            await host.stop()

    asyncio.run(scenario())


def test_plan_is_isolated_by_scope(tmp_path):
    async def scenario():
        host = WorkHost(tmp_path / "work.db", provider=PlanProvider(artifact_dir=tmp_path), max_workers=1)
        await host.start()
        try:
            result = await host.submit_plan(_scope("alice"), "列出目录", [
                {"step_id": "list", "task_type": "file.list", "objective": "列出", "input": {"directory": "D:/"}}
            ])
            plan_id = result["plan_id"]
            await _wait_for(lambda: host.get_plan(plan_id, _scope("alice"))["status"] == "completed")
            assert host.get_plan(plan_id, _scope("alice"))["status"] == "completed"
            with pytest.raises(KeyError):
                host.get_plan(plan_id, _scope("bob"))
            with pytest.raises(KeyError):
                host.plan_events(plan_id, _scope("bob"))
            assert host.list_plans(_scope("bob")) == []
        finally:
            await host.stop()

    asyncio.run(scenario())


def test_dependent_step_unlocks_after_predecessor_completes(tmp_path):
    async def scenario():
        host = WorkHost(tmp_path / "work.db", provider=PlanProvider(artifact_dir=tmp_path), max_workers=2)
        await host.start()
        try:
            scope = _scope()
            result = await host.submit_plan(scope, "整理后截图并调低音量", _plan_steps())
            plan_id = result["plan_id"]
            assert result["ready_steps"] == ["organize"]

            detail = await _wait_for(
                lambda: (lambda d: d if d["status"] == "waiting_approval" else None)(host.get_plan(plan_id, scope))
            )
            assert detail["status"] == "waiting_approval"
            assert [step["status"] for step in detail["steps"]] == ["waiting_approval", "pending", "pending"]
            assert len(detail["pending_approvals"]) == 1
            assert detail["steps"][0]["preview"]["moves"]

            approval_id = detail["pending_approvals"][0]["approval_id"]
            await host.decide_approval(approval_id, scope, "approved")
            # 三步都要人工确认：organize 是因为要真的移动文件；capture / volume
            # 是本轮强制的 —— 会改本机状态的步骤不能由候选计划自己决定免确认。
            for _ in range(4):
                # 判据要带上"有待审批项"：刚 decide 完的那一瞬间，plan.status
                # 还是 waiting_approval，但审批项已经被消费掉了，空等会误判。
                detail = await _wait_for(
                    lambda: (lambda d: d if d["status"] == "completed" or d["pending_approvals"] else None)(
                        host.get_plan(plan_id, scope)
                    )
                )
                if detail["status"] == "completed":
                    break
                await host.decide_approval(detail["pending_approvals"][0]["approval_id"], scope, "approved")
            detail = host.get_plan(plan_id, scope)
            assert detail["status"] == "completed"
            assert [step["status"] for step in detail["steps"]] == ["completed", "completed", "completed"]
            assert detail["steps"][0]["output"]["applied"]
            assert detail["artifact_summaries"]
            assert host.get_plan(plan_id, scope)["objective"] == "整理后截图并调低音量"
        finally:
            await host.stop()

    asyncio.run(scenario())


def test_failed_step_blocks_dependents_and_fails_the_plan(tmp_path):
    async def scenario():
        host = WorkHost(
            tmp_path / "work.db",
            provider=PlanProvider(artifact_dir=tmp_path, fail_types={"file.organize"}),
            max_workers=2,
        )
        await host.start()
        try:
            scope = _scope()
            result = await host.submit_plan(
                scope,
                "整理失败后停止",
                [
                    {
                        "step_id": "organize",
                        "task_type": "file.organize",
                        "objective": "整理",
                        "input": {"directory": "D:/"},
                    },
                    {"step_id": "capture", "task_type": "screen.capture", "objective": "截图", "depends_on": ["organize"]},
                    {"step_id": "volume", "task_type": "system.volume.adjust", "objective": "音量", "input": {"percent": -10}, "depends_on": ["capture"]},
                ],
            )
            plan_id = result["plan_id"]
            detail = await _wait_for(
                lambda: (lambda d: d if d["status"] == "failed" else None)(host.get_plan(plan_id, scope))
            )
            assert [step["status"] for step in detail["steps"]] == ["failed", "blocked", "blocked"]
        finally:
            await host.stop()

    asyncio.run(scenario())


def test_rejected_approval_ends_the_plan(tmp_path):
    async def scenario():
        host = WorkHost(tmp_path / "work.db", provider=PlanProvider(artifact_dir=tmp_path), max_workers=2)
        await host.start()
        try:
            scope = _scope()
            result = await host.submit_plan(scope, "整理后截图", _plan_steps())
            plan_id = result["plan_id"]
            detail = await _wait_for(
                lambda: (lambda d: d if d["status"] == "waiting_approval" else None)(host.get_plan(plan_id, scope))
            )
            await host.decide_approval(detail["pending_approvals"][0]["approval_id"], scope, "rejected")
            detail = await _wait_for(
                lambda: (lambda d: d if d["status"] == "cancelled" else None)(host.get_plan(plan_id, scope))
            )
            assert detail["status"] == "cancelled"
            assert [step["status"] for step in detail["steps"]] == ["cancelled", "blocked", "blocked"]
        finally:
            await host.stop()

    asyncio.run(scenario())


def test_cancelling_a_plan_keeps_completed_file_moves(tmp_path):
    async def scenario():
        gate = asyncio.Event()
        provider = PlanProvider(artifact_dir=tmp_path, gates={"screen.capture": gate})
        host = WorkHost(tmp_path / "work.db", provider=provider, max_workers=2)
        await host.start()
        try:
            scope = _scope()
            result = await host.submit_plan(scope, "整理后截图", _plan_steps())
            plan_id = result["plan_id"]
            detail = await _wait_for(
                lambda: (lambda d: d if d["status"] == "waiting_approval" else None)(host.get_plan(plan_id, scope))
            )
            await host.decide_approval(detail["pending_approvals"][0]["approval_id"], scope, "approved")
            detail = await _wait_for(
                lambda: (lambda d: d if d["steps"][0]["status"] == "completed" else None)(host.get_plan(plan_id, scope))
            )
            assert provider.applied, "approved moves must have been applied before cancellation"

            cancelled = await host.cancel_plan(plan_id, scope)
            assert cancelled["cancelled"] is True
            detail = host.get_plan(plan_id, scope)
            assert detail["status"] == "cancelled"
            assert detail["steps"][0]["status"] == "completed"
            assert detail["steps"][0]["output"]["applied"] == provider.applied
            assert [step["status"] for step in detail["steps"][1:]] == ["cancelled", "cancelled"]
        finally:
            gate.set()
            await host.stop()

    asyncio.run(scenario())


def test_plan_retry_keeps_the_plan_and_creates_a_new_attempt(tmp_path):
    async def scenario():
        provider = PlanProvider(artifact_dir=tmp_path, fail_types={"file.list"})
        host = WorkHost(tmp_path / "work.db", provider=provider, max_workers=1)
        await host.start()
        try:
            scope = _scope()
            result = await host.submit_plan(scope, "列出目录", [
                {"step_id": "list", "task_type": "file.list", "objective": "列出", "input": {"directory": "D:/"}}
            ])
            plan_id = result["plan_id"]
            detail = await _wait_for(
                lambda: (lambda d: d if d["status"] == "failed" else None)(host.get_plan(plan_id, scope))
            )
            work_item_id = detail["steps"][0]["work_item_id"]
            assert len(detail["steps"][0]["task"]["attempts"]) == 1

            provider.fail_types.clear()
            retried = await host.retry_plan(plan_id, scope)
            assert retried["plan_id"] == plan_id
            assert retried["retried_steps"] == ["list"]
            detail = await _wait_for(
                lambda: (lambda d: d if d["status"] == "completed" else None)(host.get_plan(plan_id, scope))
            )
            assert detail["steps"][0]["work_item_id"] == work_item_id
            assert len(detail["steps"][0]["task"]["attempts"]) == 2
        finally:
            await host.stop()

    asyncio.run(scenario())


def test_step_output_is_passed_to_the_next_step(tmp_path):
    async def scenario():
        provider = PlanProvider(artifact_dir=tmp_path)
        host = WorkHost(tmp_path / "work.db", provider=provider, max_workers=1)
        await host.start()
        try:
            scope = _scope()
            result = await host.submit_plan(
                scope,
                "列出目录再整理",
                [
                    {"step_id": "list", "task_type": "file.list", "objective": "列出", "input": {"directory": "D:/Downloads"}},
                    {
                        "step_id": "organize",
                        "task_type": "file.organize",
                        "objective": "整理",
                        "input": {"directory": "${step:list.output.directory}"},
                        "depends_on": ["list"],
                    },
                ],
            )
            plan_id = result["plan_id"]
            detail = await _wait_for(
                lambda: (lambda d: d if d["status"] == "waiting_approval" else None)(host.get_plan(plan_id, scope))
            )
            assert detail["steps"][0]["output"]["directory"] == "D:/Downloads"
            assert [call for call in provider.calls if call[0] == "file.organize"][0][1]["directory"] == "D:/Downloads"
            assert detail["steps"][1]["task"]["input"]["directory"] == "D:/Downloads"
        finally:
            await host.stop()

    asyncio.run(scenario())


def test_plan_events_are_sequential_and_replayable(tmp_path):
    async def scenario():
        host = WorkHost(tmp_path / "work.db", provider=PlanProvider(artifact_dir=tmp_path), max_workers=1)
        await host.start()
        try:
            scope = _scope()
            result = await host.submit_plan(scope, "列出目录", [
                {"step_id": "list", "task_type": "file.list", "objective": "列出", "input": {"directory": "D:/"}}
            ])
            plan_id = result["plan_id"]
            await _wait_for(lambda: host.get_plan(plan_id, scope)["status"] == "completed")
            events = host.plan_events(plan_id, scope)
            types = [event["type"] for event in events]
            assert types[0] == "plan.created"
            assert "plan.started" in types
            assert "step.ready" in types
            assert "step.started" in types
            assert "step.completed" in types
            assert types[-1] == "plan.completed"
            assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
            assert all(event["plan_id"] == plan_id for event in events)

            replay = host.plan_events(plan_id, scope, after_sequence=2)
            assert [event["sequence"] for event in replay] == list(range(3, len(events) + 1))
        finally:
            await host.stop()

    asyncio.run(scenario())


def test_restart_marks_running_plan_interrupted_without_replaying(tmp_path):
    async def scenario():
        scope = _scope()
        provider = PlanProvider(artifact_dir=tmp_path)
        crashed = WorkHost(tmp_path / "work.db", provider=provider, max_workers=1)
        plan = crashed.ledger.create_plan(
            scope=scope,
            objective="整理后截图",
            steps=crashed.ledger._definitions_for_tests() if False else __import__(
                "agent_core.control_plan", fromlist=["validate_plan_definition"]
            ).validate_plan_definition("整理后截图", _plan_steps(), provider_task_types=provider.capabilities.task_types),
        )
        crashed.ledger.update_plan_status(plan.plan_id, "running")
        crashed.ledger.update_step_status(plan.plan_id, "organize", "running")
        crashed.ledger.update_step_status(plan.plan_id, "capture", "ready")

        restarted_provider = PlanProvider(artifact_dir=tmp_path)
        host = WorkHost(tmp_path / "work.db", provider=restarted_provider, max_workers=1)
        await host.start()
        try:
            detail = host.get_plan(plan.plan_id, scope)
            assert detail["status"] == "interrupted"
            assert [step["status"] for step in detail["steps"]] == ["interrupted", "interrupted", "pending"]
            assert restarted_provider.calls == [], "an interrupted plan must never replay unknown side effects"
            await asyncio.sleep(0.05)
            assert restarted_provider.calls == []
            assert [event["type"] for event in detail["events"]][-1] == "plan.interrupted"
        finally:
            await host.stop()

    asyncio.run(scenario())


def test_step_retry_policy_creates_a_successor_attempt(tmp_path):
    async def scenario():
        provider = PlanProvider(artifact_dir=tmp_path)
        host = WorkHost(tmp_path / "work.db", provider=provider, max_workers=1)
        await host.start()
        try:
            scope = _scope()

            async def flaky(request, emit):
                flaky_calls.append(1)
                if len(flaky_calls) < 2:
                    return WorkResult("failed", error="transient")
                return WorkResult("completed", {"directory": request.task_input.get("directory")})

            flaky_calls: list[int] = []
            provider.execute = flaky
            result = await host.submit_plan(scope, "重试后成功", [
                {
                    "step_id": "list",
                    "task_type": "file.list",
                    "objective": "列出",
                    "input": {"directory": "D:/"},
                    "failure_policy": "retry",
                }
            ])
            plan_id = result["plan_id"]
            detail = await _wait_for(
                lambda: (lambda d: d if d["status"] == "completed" else None)(host.get_plan(plan_id, scope))
            )
            assert len(flaky_calls) == 2
            assert "step.retrying" in [event["type"] for event in detail["events"]]
        finally:
            await host.stop()

    asyncio.run(scenario())


def test_real_local_worker_runs_the_acceptance_plan_on_real_files(tmp_path):
    """End-to-end with the production Provider: list -> approve -> real moves."""

    async def scenario():
        from agent_core.local_worker_provider import LocalWorkerProvider

        downloads = tmp_path / "Downloads"
        downloads.mkdir()
        (downloads / "a.txt").write_text("a", encoding="utf-8")
        (downloads / "b.pdf").write_text("b", encoding="utf-8")
        provider = LocalWorkerProvider(
            allowed_roots=[str(tmp_path)], artifact_dir=tmp_path / "artifacts"
        )
        host = WorkHost(tmp_path / "work.db", provider=provider, max_workers=2)
        await host.start()
        try:
            scope = _scope()
            result = await host.submit_plan(
                scope,
                "整理下载目录",
                [
                    {
                        "step_id": "list",
                        "task_type": "file.list",
                        "objective": "列出目录",
                        "input": {"directory": str(downloads)},
                    },
                    {
                        "step_id": "organize",
                        "task_type": "file.organize",
                        "objective": "按扩展名整理",
                        "input": {"directory": "${step:list.output.directory}"},
                        "depends_on": ["list"],
                        "requires_approval": True,
                    },
                ],
            )
            plan_id = result["plan_id"]
            detail = await _wait_for(
                lambda: (lambda d: d if d["status"] == "waiting_approval" else None)(host.get_plan(plan_id, scope))
            )
            assert detail["status"] == "waiting_approval"
            assert [step["status"] for step in detail["steps"]] == ["completed", "waiting_approval"]
            # The plan keeps the declared template; the Host resolves it onto the WorkItem.
            assert detail["steps"][1]["input"]["directory"] == "${step:list.output.directory}"
            assert detail["steps"][1]["task"]["input"]["directory"] == str(downloads.resolve())
            # Nothing has moved before the user approves.
            assert (downloads / "a.txt").is_file()
            assert not (downloads / "txt").exists()

            await host.decide_approval(detail["pending_approvals"][0]["approval_id"], scope, "approved")
            detail = await _wait_for(
                lambda: (lambda d: d if d["status"] == "completed" else None)(host.get_plan(plan_id, scope))
            )
            assert detail["status"] == "completed"
            assert (downloads / "txt" / "a.txt").is_file()
            assert (downloads / "pdf" / "b.pdf").is_file()
            assert not (downloads / "a.txt").exists()
        finally:
            await host.stop()

    asyncio.run(scenario())
