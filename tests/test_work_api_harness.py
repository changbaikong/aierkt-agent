"""HTTP-surface tests for the Harness bridge (capabilities, detail, retry guard)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_core.composite_work_provider import CompositeWorkProvider
from agent_core.contracts import Scope
from agent_core.provider_activity import ProviderActivity
from agent_core.work_host import WorkHost
from agent_core.work_provider import WorkProviderCapabilities, WorkResult


class StubProvider:
    def __init__(self, provider_id: str, *, selectable: bool = False) -> None:
        self.capabilities = WorkProviderCapabilities(
            provider_id=provider_id,
            task_types=("code.agent",),
            supports_cancel=True,
            supports_resume=True,
            selectable=selectable,
        )

    async def execute(self, request, emit):  # noqa: ANN001
        return WorkResult("completed", output={"provider": self.capabilities.provider_id})

    async def cancel(self, attempt_id: str) -> bool:
        return True

    async def resume(self, request, checkpoint, decision, emit):  # noqa: ANN001
        return WorkResult("completed")

    async def health(self) -> dict:
        return {
            "provider_id": "deepseek_harness",
            "available": False,
            "reason": "Harness 未安装",
            "executable": "",
            "version": "",
            "required_version": ">=0.2.0（允许预发布版）",
            "sandbox": "workspace-write",
            "acp": False,
            "session_capabilities": [],
        }


@pytest.fixture()
def client(monkeypatch, tmp_path):
    import web_api

    host = WorkHost(
        tmp_path / "work.db",
        provider=CompositeWorkProvider(
            [StubProvider("code-agent"), StubProvider("deepseek_harness", selectable=True)]
        ),
        max_workers=1,
        code_workers=1,
    )
    monkeypatch.setattr(web_api, "work_host", host)
    monkeypatch.setattr(web_api, "_initialize_runtime", lambda: None)
    with TestClient(web_api.app) as test_client:
        test_client.host = host  # type: ignore[attr-defined]
        yield test_client


def test_capabilities_expose_provider_registry_and_harness_health(client) -> None:
    response = client.get("/api/work/capabilities")

    assert response.status_code == 200
    payload = response.json()
    providers = {item["provider_id"]: item for item in payload["providers"]}
    assert providers["code-agent"]["default"] is True
    assert providers["deepseek_harness"]["selectable"] is True
    assert payload["harness"]["provider_id"] == "deepseek_harness"
    assert payload["harness"]["available"] is False
    assert payload["harness"]["reason"] == "Harness 未安装"


def test_capabilities_carry_match_report_and_match_endpoint_answers_why(client) -> None:
    payload = client.get("/api/work/capabilities").json()

    # 两个 stub 的 health 都报不可用：匹配结论必须是 rejected，且给出原因。
    matching = {entry["provider_id"]: entry for entry in payload["matching"]}
    assert set(matching) == {"code-agent", "deepseek_harness"}
    for entry in matching.values():
        assert entry["verdict"] == "rejected"
        assert any("Harness 未安装" in reason for reason in entry["reasons"])

    single = client.get("/api/work/match?task_type=code.agent")
    assert single.status_code == 200
    report = single.json()
    assert report["provider_id"] == "code-agent"  # 未指定标识时解析默认执行者
    assert report["verdict"] == "rejected"


def test_submit_with_explicit_provider_is_accepted_and_unknown_is_rejected(client) -> None:
    accepted = client.post(
        "/api/work/tasks",
        json={
            "account_id": "local-user",
            "conversation_id": "web",
            "task_type": "code.agent",
            "objective": "改代码",
            "input": {"project_root": "C:/tmp", "provider": "deepseek_harness"},
        },
    )
    assert accepted.status_code == 202

    rejected = client.post(
        "/api/work/tasks",
        json={
            "account_id": "local-user",
            "conversation_id": "web",
            "task_type": "code.agent",
            "objective": "改代码",
            "input": {"project_root": "C:/tmp", "provider": "ghost"},
        },
    )
    assert rejected.status_code == 400
    assert "unknown work provider" in rejected.json()["detail"]


def test_task_detail_includes_provider_activity(client) -> None:
    host: WorkHost = client.host  # type: ignore[attr-defined]
    scope = Scope("local-user", "web", "web", "work")
    work = host.ledger.create_work_item(
        scope=scope, objective="长任务", task_type="code.agent", input={"project_root": "C:/tmp"}
    )
    attempt = host.ledger.create_attempt(work.work_item_id, provider_id="deepseek_harness")
    host.ledger.save_provider_activity(
        ProviderActivity(
            attempt_id=attempt.attempt_id,
            provider_id="deepseek_harness",
            status="thinking",
            session_id="s-1",
            started_at="2026-10-05T06:00:00+00:00",
        )
    )

    response = client.get(f"/api/work/tasks/{work.work_item_id}?account_id=local-user&conversation_id=web")

    assert response.status_code == 200
    detail = response.json()
    assert detail["provider_activity"]["session_id"] == "s-1"
    assert detail["provider_activity"]["status"] == "thinking"
    assert detail["attempts"][0]["provider_activity"]["provider_name"] == "DeepSeek Harness"


def test_retry_of_unknown_result_requires_explicit_confirmation(client) -> None:
    host: WorkHost = client.host  # type: ignore[attr-defined]
    scope = Scope("local-user", "web", "web", "work")
    work = host.ledger.create_work_item(
        scope=scope, objective="结果未知", task_type="code.agent", input={"project_root": "C:/tmp"}
    )
    attempt = host.ledger.create_attempt(work.work_item_id, provider_id="deepseek_harness")
    host.ledger.claim_attempt(attempt.attempt_id)
    host.ledger.finish_attempt(attempt.attempt_id, status="unknown_result", error="lost")

    url = f"/api/work/tasks/{work.work_item_id}/retry?account_id=local-user&conversation_id=web"
    plain = client.post(url)
    assert plain.status_code == 409
    assert "unknown" in plain.json()["detail"]

    confirmed = client.post(url + "&confirm_unknown=1")
    assert confirmed.status_code == 202
    assert confirmed.json()["status"] == "queued"
