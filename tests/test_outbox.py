from __future__ import annotations

import asyncio

from agent_core.outbox import OutboxStore, OutboxWorker


def test_outbox_enqueue_is_idempotent(tmp_path) -> None:
    store = OutboxStore(tmp_path / "outbox.db")
    first = store.enqueue(account_id="a", delivery_target="unified", event_type="task.due", payload={"title": "x"}, dedupe_key="run-1")
    second = store.enqueue(account_id="a", delivery_target="unified", event_type="task.due", payload={"title": "x"}, dedupe_key="run-1")
    assert first.message_id == second.message_id
    assert len(store.pending()) == 1


def test_outbox_worker_retries_failed_send(tmp_path) -> None:
    store = OutboxStore(tmp_path / "outbox.db")
    item = store.enqueue(account_id="a", delivery_target="unified", event_type="task.due", payload={"title": "x"}, dedupe_key="run-2")
    attempts = 0

    async def send(_account_id, _target, _event):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("offline")

    worker = OutboxWorker(store, send, retry_base_seconds=0)

    async def run():
        await worker.process_once()
        assert store.get(item.message_id).status == "pending"
        await worker.process_once()

    asyncio.run(run())
    assert attempts == 2
    assert store.get(item.message_id).status == "sent"
