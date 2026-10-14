from agent_core.contracts import AccountTaskScope, Scope
from agent_core.persistence import SQLiteConfirmationStore


def test_confirmation_can_be_read_and_decided_from_another_ingress(tmp_path):
    store = SQLiteConfirmationStore(tmp_path / "core.db")
    original = Scope("account-1", "wechat", "wx-chat", "companion")
    other_entry = Scope("account-1", "web", "web-chat", "companion")
    request = store.create(
        run_id="run-1", scope=original, tool_call_id="call-1", tool="tasks__create",
        arguments={"title": "x"}, title="Create task",
    )

    assert store.get_for_account(request.action_id, AccountTaskScope("account-1", "companion")) is not None
    assert store.get_for_account(request.action_id, AccountTaskScope("account-2", "companion")) is None
    decided = store.decide_for_account(request.action_id, AccountTaskScope("account-1", "companion"), "confirmed")
    assert decided is not None and decided.status == "confirmed"
    assert store.decide(request.action_id, other_entry, "rejected") is None
