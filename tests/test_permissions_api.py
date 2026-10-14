"""``/api/permissions`` 的 HTTP 层测试。

分文件而不是塞进 ``test_permissions.py``：那个文件验的是**策略语义**
（谁能放行、什么不能被放行），这里是**接口契约**（请求体、状态码、
返回字段、以及"开启时顺手清理"这条副作用）。两者改动的理由完全不同，
放一起会在改接口时误伤语义用例。

这里用 ``TestClient`` 打真路由，但把权限层预置好 —— 要验的是
"接口把层用对了吗"，不是"层本身对不对"。
"""

from __future__ import annotations

import pytest

from agent_core.contracts import Scope
from agent_core.permission_mode import (
    MODE_FULL_ACCESS,
    MODE_STANDARD,
    REASON_NON_LOOPBACK,
    PermissionLayer,
)

OWNER = "local-user"


@pytest.fixture()
def permissions(tmp_path, monkeypatch):
    """每个用例一份独立的权限层 + 独立数据库，避免互相污染。"""
    import web_api

    layer = PermissionLayer(db_path=tmp_path / "core.db", web_host="127.0.0.1")
    monkeypatch.setattr(web_api, "permission_layer", layer)
    return layer


@pytest.fixture()
def client(permissions):
    from fastapi.testclient import TestClient
    import web_api

    return TestClient(web_api.app)


def test_get_permissions_defaults_to_standard(client, permissions):
    response = client.get("/api/permissions", params={"account_id": OWNER})

    assert response.status_code == 200
    body = response.json()
    assert body["account_id" if "account_id" in body else "owner_id"] == OWNER
    assert body["configured_mode"] == MODE_STANDARD
    assert body["effective_mode"] == MODE_STANDARD
    assert body["available"] is True
    # 排除表 2026-09-22 起只剩 `work__plan_submit`：`work__approve` 整个从模型
    # 工具面删掉了，审批改由用户走统一审批控制面。
    assert body["exclusions"] == ["work__plan_submit"]
    assert sorted(body["supports"]) == ["full_access", "standard"]


def test_get_permissions_reflects_a_previous_switch(client, permissions):
    permissions.controller.set_mode(OWNER, MODE_FULL_ACCESS)

    body = client.get("/api/permissions", params={"account_id": OWNER}).json()

    assert body["configured_mode"] == MODE_FULL_ACCESS
    assert body["effective_mode"] == MODE_FULL_ACCESS


def test_put_permissions_enables_full_access(client, permissions):
    response = client.put(
        "/api/permissions", json={"account_id": OWNER, "mode": MODE_FULL_ACCESS}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["configured_mode"] == MODE_FULL_ACCESS
    assert body["effective_mode"] == MODE_FULL_ACCESS
    assert permissions.store.mode_for(OWNER) == MODE_FULL_ACCESS


def test_put_permissions_switching_back_restores_immediately(client, permissions):
    client.put("/api/permissions", json={"account_id": OWNER, "mode": MODE_FULL_ACCESS})
    response = client.put(
        "/api/permissions", json={"account_id": OWNER, "mode": MODE_STANDARD}
    )

    assert response.status_code == 200
    assert response.json()["effective_mode"] == MODE_STANDARD
    assert permissions.store.mode_for(OWNER) == MODE_STANDARD


def test_put_permissions_rejects_unknown_mode(client, permissions):
    response = client.put("/api/permissions", json={"account_id": OWNER, "mode": "yolo"})

    assert response.status_code == 422
    # 值域没被写进数据库
    assert permissions.store.mode_for(OWNER) == MODE_STANDARD


def test_put_permissions_refuses_when_not_loopback(client, tmp_path, monkeypatch):
    """非回环监听下不生效，但要诚实回报 configured 与原因码。"""
    import web_api

    layer = PermissionLayer(db_path=tmp_path / "core.db", web_host="0.0.0.0")
    monkeypatch.setattr(web_api, "permission_layer", layer)

    body = client.put(
        "/api/permissions", json={"account_id": OWNER, "mode": MODE_FULL_ACCESS}
    ).json()

    assert body["configured_mode"] == MODE_FULL_ACCESS
    assert body["effective_mode"] == MODE_STANDARD
    assert body["available"] is False
    assert body["reason"] == REASON_NON_LOOPBACK
    assert body["reason_text"]


def test_put_permissions_cleans_old_confirmations_on_enable(client, permissions):
    """开启完全访问时，该账号所有会话的旧确认框一起被拒。"""
    from agent_core.persistence import SQLiteConfirmationStore

    store = SQLiteConfirmationStore(permissions.db_path)
    for index, conversation in enumerate(("web-1", "web-2"), start=1):
        store.create(
            run_id=f"run-{index}",
            scope=Scope(OWNER, "web", conversation, "companion"),
            tool_call_id=f"c{index}",
            tool="tasks__create",
            arguments={},
            title="t",
        )
    # 把 store 接到清理链路上（生产里由 _initialize_runtime 的 attach 完成）
    permissions.attach(confirmation_store=store)

    body = client.put(
        "/api/permissions", json={"account_id": OWNER, "mode": MODE_FULL_ACCESS}
    ).json()

    assert body["cleanup"]["rejected_confirmations"] == 2
    assert body["rejected_confirmations"] == 2
    assert body["cleanup"]["failed"] == []
    assert store.pending_for_scope(Scope(OWNER, "web", "web-1", "companion")) == []


def test_put_permissions_does_not_clean_when_mode_did_not_take_effect(
    client, tmp_path, monkeypatch
):
    """非回环时旧审批依然有效 —— 不许顺手清掉（那等于替用户拒了一堆有效操作）。"""
    import web_api
    from agent_core.persistence import SQLiteConfirmationStore

    layer = PermissionLayer(db_path=tmp_path / "core.db", web_host="0.0.0.0")
    store = SQLiteConfirmationStore(layer.db_path)
    store.create(
        run_id="run-1",
        scope=Scope(OWNER, "web", "web-1", "companion"),
        tool_call_id="c1",
        tool="tasks__create",
        arguments={},
        title="t",
    )
    layer.attach(confirmation_store=store)
    monkeypatch.setattr(web_api, "permission_layer", layer)

    body = client.put(
        "/api/permissions", json={"account_id": OWNER, "mode": MODE_FULL_ACCESS}
    ).json()

    assert body["effective_mode"] == MODE_STANDARD
    assert body["cleanup"] is None
    # 确认框还在
    assert len(store.pending_for_scope(Scope(OWNER, "web", "web-1", "companion"))) == 1


def test_get_permissions_is_usable_before_runtime_initialises(tmp_path, monkeypatch):
    """服务刚起来、前端先拉权限时不能 500 —— 层要按需建。"""
    import web_api

    monkeypatch.setattr(web_api, "permission_layer", None)
    monkeypatch.setenv("AGENT_CORE_DB", str(tmp_path / "lazy.db"))
    monkeypatch.setenv("WEB_HOST", "127.0.0.1")

    from fastapi.testclient import TestClient

    response = TestClient(web_api.app).get("/api/permissions", params={"account_id": OWNER})

    assert response.status_code == 200
    assert response.json()["effective_mode"] == MODE_STANDARD
    monkeypatch.setattr(web_api, "permission_layer", None)
