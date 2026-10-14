"""Web contract for control plans: submission, detail, scope and artifacts."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

from agent_core.work_provider import (
    WORK_TASK_TYPES,
    WorkArtifact,
    WorkProviderCapabilities,
    WorkResult,
)


class WebPlanProvider:
    capabilities = WorkProviderCapabilities(
        "web-plan-test", tuple(sorted(WORK_TASK_TYPES)), supports_cancel=True, supports_resume=False
    )

    def __init__(self, *, artifact_dir, artifact_expires_at="2999-01-01T00:00:00+00:00", fail_types=(), gates=None):
        self.artifact_dir = artifact_dir
        self.artifact_expires_at = artifact_expires_at
        self.fail_types = set(fail_types)
        self.gates = dict(gates or {})
        self.applied = []
        self.calls = []

    async def cancel(self, attempt_id):
        return True

    async def resume(self, request, checkpoint, decision, emit):
        return WorkResult("interrupted", error="not supported")

    async def execute(self, request, emit):
        self.calls.append(request.task_type)
        gate = self.gates.get(request.task_type)
        if gate is not None:
            await asyncio.to_thread(gate.wait)
        if request.task_type in self.fail_types:
            return WorkResult("failed", error=f"boom:{request.task_type}")
        task_input = dict(request.task_input)
        if request.task_type == "file.organize":
            if request.approval_manifest:
                moves = [dict(item) for item in request.approval_manifest]
                self.applied.extend(moves)
                return WorkResult("completed", {"directory": task_input.get("directory"), "applied": moves, "status": "completed"})
            moves = [{"source": "D:/Downloads/a.txt", "destination": "D:/Downloads/txt/a.txt"}]
            return WorkResult("waiting_approval", {"directory": task_input.get("directory"), "moves": moves, "total": 1})
        if request.task_type == "screen.capture":
            path = self.artifact_dir / "shot.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\x89PNG\r\n\x1a\nweb")
            artifact = WorkArtifact(
                artifact_id="shot.png",
                attempt_id=request.attempt_id,
                media_type="image/png",
                size=path.stat().st_size,
                path=str(path),
                expires_at=self.artifact_expires_at,
            )
            return WorkResult("completed", {"artifact_path": artifact.path}, artifacts=[artifact])
        if request.task_type == "system.volume.adjust":
            return WorkResult("completed", {"percent": task_input.get("percent"), "status": "ok"})
        if request.task_type == "file.list":
            return WorkResult("completed", {"directory": task_input.get("directory"), "total": 0, "files": []})
        return WorkResult("failed", error=f"unsupported task_type: {request.task_type}")


def _plan_payload():
    return {
        "objective": "整理下载目录，审批后截图并调低音量",
        "account_id": "alice",
        "conversation_id": "chat-1",
        "steps": [
            {
                "step_id": "organize",
                "task_type": "file.organize",
                "objective": "按扩展名整理目录",
                "input": {"directory": "D:/Downloads"},
                "depends_on": [],
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
        ],
    }


def _wait_for(client, url, predicate, *, timeout=5.0):
    deadline = time.monotonic() + timeout
    payload = {}
    while time.monotonic() < deadline:
        response = client.get(url)
        assert response.status_code == 200, response.text
        payload = response.json()
        if predicate(payload):
            return payload
        time.sleep(0.02)
    return payload


def _build_client(monkeypatch, tmp_path, provider):
    from fastapi.testclient import TestClient
    import web_api
    from agent_core.work_host import WorkHost

    web_api.work_host = WorkHost(tmp_path / "work.db", provider=provider, max_workers=2)
    monkeypatch.setattr(web_api, "_initialize_runtime", lambda: None)
    return TestClient(web_api.app)


def test_work_plan_routes_submit_approve_and_isolate_scope(monkeypatch, tmp_path):
    provider = WebPlanProvider(artifact_dir=tmp_path)
    with _build_client(monkeypatch, tmp_path, provider) as client:
        response = client.post("/api/work/plans", json=_plan_payload())
        assert response.status_code == 202, response.text
        submitted = response.json()
        assert submitted["status"] == "queued"
        assert submitted["step_count"] == 3
        assert submitted["ready_steps"] == ["organize"]
        plan_id = submitted["plan_id"]

        listing = client.get("/api/work/plans?account_id=alice&conversation_id=chat-1").json()["plans"]
        assert [plan["plan_id"] for plan in listing] == [plan_id]
        assert listing[0]["step_count"] == 3

        scope = "account_id=alice&conversation_id=chat-1"
        detail = _wait_for(
            client,
            f"/api/work/plans/{plan_id}?{scope}",
            lambda payload: payload["status"] == "waiting_approval",
        )
        assert detail["status"] == "waiting_approval"
        assert [step["status"] for step in detail["steps"]] == ["waiting_approval", "pending", "pending"]
        assert detail["steps"][0]["preview"]["moves"]
        assert detail["pending_approvals"]

        events = client.get(f"/api/work/plans/{plan_id}/events?{scope}").json()["events"]
        assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
        replay = client.get(f"/api/work/plans/{plan_id}/events?{scope}&after_sequence=2").json()["events"]
        assert [event["sequence"] for event in replay] == list(range(3, len(events) + 1))

        approval_id = detail["pending_approvals"][0]["approval_id"]
        approved = client.post(f"/api/work/approvals/{approval_id}?{scope}", json={"decision": "approved"})
        assert approved.status_code == 200, approved.text

        detail = _wait_for(client, f"/api/work/plans/{plan_id}?{scope}", lambda payload: payload["status"] == "completed")
        assert detail["status"] == "completed"
        assert [step["status"] for step in detail["steps"]] == ["completed", "completed", "completed"]
        assert detail["artifact_summaries"]
        assert provider.applied

        # The plan detail and the single-task detail must agree.
        capture_step = detail["steps"][1]
        work_item_id = capture_step["work_item_id"]
        task = client.get(f"/api/work/tasks/{work_item_id}?{scope}").json()
        assert task["work_item_id"] == capture_step["task"]["work_item_id"]
        assert task["status"] == capture_step["task"]["status"] == "completed"
        assert task["events"] == capture_step["task"]["events"]

        # Another owner cannot read or subscribe to this plan.
        assert client.get(f"/api/work/plans/{plan_id}?account_id=bob&conversation_id=chat-1").status_code == 404
        assert client.get(f"/api/work/plans/{plan_id}/events?account_id=bob&conversation_id=chat-1").status_code == 404
        assert client.get("/api/work/plans?account_id=bob&conversation_id=chat-1").json()["plans"] == []


def test_work_plan_rejects_unvalidated_plans_without_persisting(monkeypatch, tmp_path):
    provider = WebPlanProvider(artifact_dir=tmp_path)
    with _build_client(monkeypatch, tmp_path, provider) as client:
        payload = _plan_payload()
        payload["steps"][0]["task_type"] = "shell.exec"
        response = client.post("/api/work/plans", json=payload)
        assert response.status_code == 400
        assert "unknown task_type" in response.json()["detail"]
        assert client.get("/api/work/plans?account_id=alice&conversation_id=chat-1").json()["plans"] == []
        assert provider.calls == []


def test_work_plan_cancel_keeps_applied_moves_and_blocks_retry(monkeypatch, tmp_path):
    gate = threading.Event()
    provider = WebPlanProvider(artifact_dir=tmp_path, gates={"system.volume.adjust": gate})
    try:
        with _build_client(monkeypatch, tmp_path, provider) as client:
            scope = "account_id=alice&conversation_id=chat-1"
            plan_id = client.post("/api/work/plans", json=_plan_payload()).json()["plan_id"]
            detail = _wait_for(
                client, f"/api/work/plans/{plan_id}?{scope}", lambda payload: payload["status"] == "waiting_approval"
            )
            client.post(
                f"/api/work/approvals/{detail['pending_approvals'][0]['approval_id']}?{scope}",
                json={"decision": "approved"},
            )
            _wait_for(
                client,
                f"/api/work/plans/{plan_id}?{scope}",
                lambda payload: payload["steps"][0]["status"] == "completed" and payload["steps"][1]["status"] == "completed",
            )

            cancelled = client.post(f"/api/work/plans/{plan_id}/cancel?{scope}")
            assert cancelled.status_code == 200
            assert cancelled.json()["cancelled"] is True
            detail = client.get(f"/api/work/plans/{plan_id}?{scope}").json()
            assert detail["status"] == "cancelled"
            assert detail["steps"][0]["status"] == "completed"
            assert detail["steps"][0]["output"]["applied"] == provider.applied
            assert detail["steps"][1]["status"] == "completed"
            assert detail["steps"][2]["status"] == "cancelled"

            assert client.post(f"/api/work/plans/missing/cancel?{scope}").status_code == 404
            # Release the provider thread before the portal shuts its executor down.
            gate.set()
    finally:
        gate.set()


def test_plan_artifact_download_still_enforces_scope_and_expiry(monkeypatch, tmp_path):
    provider = WebPlanProvider(artifact_dir=tmp_path)
    with _build_client(monkeypatch, tmp_path, provider) as client:
        scope = "account_id=alice&conversation_id=chat-1"
        payload = {
            "objective": "截图",
            "account_id": "alice",
            "conversation_id": "chat-1",
            "steps": [{"step_id": "shot", "task_type": "screen.capture", "objective": "截图", "input": {}}],
        }
        plan_id = client.post("/api/work/plans", json=payload).json()["plan_id"]
        detail = _wait_for(client, f"/api/work/plans/{plan_id}?{scope}", lambda payload: payload["status"] == "completed")
        step = detail["steps"][0]
        artifact_id = detail["artifact_summaries"][0]["artifact_id"]
        work_item_id = step["work_item_id"]

        own = client.get(f"/api/work/tasks/{work_item_id}/artifacts/{artifact_id}?{scope}")
        assert own.status_code == 200
        assert client.get(f"/api/work/tasks/{work_item_id}/artifacts/{artifact_id}?account_id=bob&conversation_id=chat-1").status_code == 404
        assert client.get(f"/api/work/tasks/{work_item_id}/artifacts/{artifact_id}?{scope}&account_id=alice&conversation_id=other").status_code == 404

        with sqlite3.connect(str(tmp_path / "work.db")) as db:
            db.execute("UPDATE work_artifacts SET expires_at='2000-01-01T00:00:00+00:00' WHERE artifact_id=?", (artifact_id,))
        assert client.get(f"/api/work/tasks/{work_item_id}/artifacts/{artifact_id}?{scope}").status_code == 404
