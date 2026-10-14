"""动作审计与重启对账。

【这个文件守的是什么】

`outcome_unknown` 光靠"包一层网关"是做不出来的：Python 杀不掉线程，
超时的动作可能还在后台跑。要让这个词有意义，必须有**第二条**机制：

    执行前先把 `running` 写进审计表
    → 进程重启时，凡是还挂着 `running` 的，一律对账为 `outcome_unknown`

没有这一步，重启后那条记录还挂在 `running` 上，没人知道副作用到底发生没有。

【审计表只记身份，不记参数值】

只记：动作身份、来源、作用域、状态、耗时、错误码、审批编号。
**不复制参数值** —— 参数里可能有路径、坐标、用户原话，那是另一套数据
（会话/账本）该管的事。审计表被高频读，也不该成为敏感数据的第二副本。
"""

from __future__ import annotations

import pytest

from agent_core.contracts import ActionRequest, Scope


@pytest.fixture()
def journal(tmp_path):
    from agent_core.action_journal import ActionJournal

    return ActionJournal(tmp_path / "actions.db")


def _request(action_id: str = "act-1", **overrides) -> ActionRequest:
    base = dict(
        action_id=action_id,
        name="demo__ping",
        arguments={"value": "secret-path"},
        scope=Scope("u1", "web", "c1", "companion"),
        origin="chat_model",
        run_id="run-1",
        turn_id="turn-1",
        tool_call_id="call-1",
    )
    base.update(overrides)
    return ActionRequest(**base)


# ──────────────────────── 登记与幂等 ────────────────────────


def test_register_is_idempotent_and_reports_whether_it_was_new(journal) -> None:
    """登记必须原子且幂等：第一次 True，重复 False。

    网关靠这个返回值决定"要不要真的执行"。
    """
    assert journal.register(_request()) is True
    assert journal.register(_request()) is False


def test_registered_record_can_be_read_back(journal) -> None:
    journal.register(_request())
    record = journal.get("act-1")
    assert record is not None
    assert record["name"] == "demo__ping"
    assert record["origin"] == "chat_model"
    assert record["status"] == "running"


def test_unknown_action_id_returns_none(journal) -> None:
    assert journal.get("nope") is None


# ──────────────────────── 不复制敏感参数 ────────────────────────


def test_audit_record_does_not_copy_argument_values(journal) -> None:
    """审计记录里不得出现参数**值**（只留字段名）。

    参数值里有路径 / 坐标 / 用户原话，审计表被高频读，
    不该成为敏感数据的第二副本。
    """
    journal.register(_request())
    record = journal.get("act-1")

    blob = repr(record)
    assert "secret-path" not in blob, "审计记录复制了参数值"
    # 字段名留着是有用的（知道这次动了哪些槽位），但不带值
    assert "value" in blob


# ──────────────────────── 状态转换 ────────────────────────


def test_finish_records_terminal_state_and_duration(journal) -> None:
    journal.register(_request())
    journal.finish("act-1", status="completed", duration_ms=42)

    record = journal.get("act-1")
    assert record["status"] == "completed"
    assert record["duration_ms"] == 42


def test_finish_with_error_code_is_recorded(journal) -> None:
    journal.register(_request())
    journal.finish("act-1", status="failed", error_kind="permanent", duration_ms=7)

    record = journal.get("act-1")
    assert record["status"] == "failed"
    assert record["error_kind"] == "permanent"


def test_finish_unknown_action_id_is_a_noop_not_a_raise(journal) -> None:
    """对账/收尾路径可能拿到已经不在的记录，不许抛异常打断流程。"""
    journal.finish("nope", status="failed")


def test_waiting_approval_records_the_approval_id(journal) -> None:
    journal.register(_request())
    journal.finish("act-1", status="waiting_approval", approval_id="appr-9")

    record = journal.get("act-1")
    assert record["status"] == "waiting_approval"
    assert record["approval_id"] == "appr-9"


# ──────────────────────── 重启对账 ────────────────────────


def test_reconcile_marks_leftover_running_as_outcome_unknown(tmp_path) -> None:
    """重启对账：遗留的 `running` 一律改判 `outcome_unknown`。

    这是 `outcome_unknown` 能成立的**唯一**依据：进程在执行中途死了，
    审计表里那条还挂着 running，我们既不能假定成功也不能假定失败。
    """
    from agent_core.action_journal import ActionJournal

    db = tmp_path / "actions.db"
    first = ActionJournal(db)
    first.register(_request("act-a"))
    first.register(_request("act-b"))
    first.finish("act-b", status="completed")
    # 模拟进程死亡：act-a 留在 running 上，没人调 finish

    second = ActionJournal(db)  # 相当于新进程启动
    repaired = second.reconcile_on_start()

    assert repaired == 1, "应该只对账掉那一条遗留的 running"
    assert second.get("act-a")["status"] == "outcome_unknown"
    assert second.get("act-a")["error_kind"] == "indeterminate"
    # 已经到终态的不能被改动
    assert second.get("act-b")["status"] == "completed"


def test_reconcile_is_idempotent(tmp_path) -> None:
    """对账跑两遍不该产生额外改动（启动可能被重试）。"""
    from agent_core.action_journal import ActionJournal

    db = tmp_path / "actions.db"
    first = ActionJournal(db)
    first.register(_request("act-a"))

    second = ActionJournal(db)
    assert second.reconcile_on_start() == 1
    assert second.reconcile_on_start() == 0, "第二次对账不该再找到 running"


def test_reconcile_does_not_touch_waiting_approval(tmp_path) -> None:
    """等审批的动作不是"遗留的 running"：它跨重启本来就该活着。"""
    from agent_core.action_journal import ActionJournal

    db = tmp_path / "actions.db"
    first = ActionJournal(db)
    first.register(_request("act-a"))
    first.finish("act-a", status="waiting_approval", approval_id="appr-1")

    second = ActionJournal(db)
    assert second.reconcile_on_start() == 0
    assert second.get("act-a")["status"] == "waiting_approval"


def test_reconcile_counts_only_running(tmp_path) -> None:
    """混着一堆终态时，只数 running 那几条。"""
    from agent_core.action_journal import ActionJournal

    db = tmp_path / "actions.db"
    first = ActionJournal(db)
    for i, status in enumerate(["completed", "failed", "rejected", "cancelled"]):
        first.register(_request(f"act-done-{i}"))
        first.finish(f"act-done-{i}", status=status)
    first.register(_request("act-left"))
    first.register(_request("act-left-2"))

    second = ActionJournal(db)
    assert second.reconcile_on_start() == 2
