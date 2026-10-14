"""更新后的记忆怎么进入回答（B 计划第五步）。

钉四件事：
1. 默认用**当前有效版本**；被纠正/替代的旧版本只在「解释历史」那一段出现；
2. 冲突的两条说法都带上，谁都不被当成唯一的确定事实；
3. 同项目的新会话（另一个 conversation_id）能读到更新后的结论；
4. 召回开关关掉时，新版本既不从直接候选进来，也不从关系旁路漏进来。

对应的验收样例是 M4 / M5 / M7 / M15。
"""
from __future__ import annotations

from scripts.memory_test_support import seed_semantic
import pytest

from agent_core.contracts import Scope
from agent_core.memory_maintenance import MemoryMaintenanceService
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_retrieval import MemoryRetriever
from agent_core.memory_turn import MemoryCandidate, MemoryUpdateSuggestion
from agent_core.memory_writes import MemoryWriter


class _Embeddings:
    def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


@pytest.fixture(autouse=True)
def _semantic_memory_on(monkeypatch):
    """新版总开关默认关着：不打开它，新语义记录对检索本来就是不可见的。

    这个文件测的是「开关打开之后，更新后的版本进不进回答」，所以显式打开。
    开关关掉时的行为由 `test_turning_semantic_recall_off_...` 单独钉住。
    """
    monkeypatch.setenv("MEMORY_SEMANTIC_ENABLED", "true")


def _stack(tmp_path):
    repos = initialize_memory_repositories(
        tmp_path / "records.db", tmp_path / "vectors", vector_size=3
    )
    writer = MemoryWriter(repos, _Embeddings())
    retriever = MemoryRetriever(repos, _Embeddings())
    return writer, retriever, MemoryMaintenanceService(writer, repos), repos


def _scope(owner="user", conversation="c1") -> Scope:
    return Scope(owner, "web", conversation, "companion")


def _save(writer, scope, content, *, kind="decision", status="accepted", binding=None, **kwargs):
    return seed_semantic(writer,
        scope,
        [
            MemoryCandidate(
                content=content,
                kind=kind,
                content_status=status,
                source_types=("user_statement",),
                source_refs=("turn-0",),
                occurred_at="2026-10-11T10:00:00+00:00",
                **kwargs,
            )
        ],
        turn_id="turn-0",
        occurred_at="2026-10-11T10:00:00+00:00",
        # 项目范围必须同时给出**可信绑定**：候选自己填的项目名只是标签，
        # 对不上可信绑定时会被降回会话范围（这是有意的权限约束）。
        project_binding=binding,
    )[0]


def _correct(service, scope, target, content, *, packet_user="不是五千，是三千", project_id=""):
    from agent_core.memory_turn import MemoryTurnPacket

    packet = MemoryTurnPacket(
        turn_id="turn-1",
        occurred_at="2026-10-11T12:00:00+00:00",
        scope_owner=scope.owner_id,
        scope_channel=scope.channel,
        scope_conversation=scope.conversation_id,
        project_binding=project_id,
        user_text=packet_user,
        published_reply="好，改成三千",
    )
    suggestion = MemoryUpdateSuggestion(
            action="correct",
            target_id=target["id"],
            expected_version=int(target["record_version"]),
            content=content,
            kind="fact",
            content_status="reported",
            source_types=("user_statement",),
            reason="用户指出旧数字错了",
        )
    import asyncio
    from support.memory_review import prepare_update, supported_row
    assert (packet_user, content) == ('不是五千，是三千', '预算三千')
    suggestion = asyncio.run(prepare_update(service.writer, packet, suggestion, [supported_row(
        '预算三千', [('turn-1', '不是五千'), ('turn-1', '是三千')])]))
    return service.apply(
        suggestion,
        scope=scope,
        packet=packet,
        turn_id="turn-1",
        project_id=project_id,
    )


def test_the_current_version_is_what_gets_injected(tmp_path) -> None:
    writer, retriever, service, repos = _stack(tmp_path)
    scope = _scope()
    old = _save(writer, scope, "预算五千", kind="fact", status="reported")
    outcome = _correct(service, scope, old, "预算三千")
    assert outcome["applied"] is True, outcome["reason"]

    result = retriever.retrieve(scope, "预算")
    warm_contents = {row["content"] for row in result.warm}
    historical = {row["content"] for row in result.cold_evidence}

    assert "预算三千" in warm_contents
    assert "预算五千" not in warm_contents  # 旧版本不再当现行结论
    assert "预算五千" in historical  # 但留着，能被解释历史时查到
    repos.close()


def test_a_conflict_keeps_both_sides_and_says_so(tmp_path) -> None:
    writer, retriever, service, repos = _stack(tmp_path)
    scope = _scope()
    first = _save(writer, scope, "预算是五千", kind="fact", status="reported")
    second = _save(writer, scope, "预算是三千", kind="fact", status="reported")
    service.apply(
        MemoryUpdateSuggestion(
            action="conflict",
            target_id=first["id"],
            expected_version=1,
            conflict_ids=(second["id"],),
            reason="两条说法对不上，还没弄清是不是两个项目",
            source_types=("user_statement",),
            source_refs=("turn-1",),
        ),
        scope=scope,
        packet=_packet_for(scope, "不记得了"),
        turn_id="turn-1",
    )

    context = retriever.build_context(scope, "预算")
    assert "与他条冲突" in context.injected_text
    assert "预算是五千" in context.injected_text
    assert "预算是三千" in context.injected_text
    assert context.metadata["has_conflict"] is True
    repos.close()


def test_a_new_conversation_in_the_same_project_reads_the_corrected_choice(tmp_path) -> None:
    """M4 + M15：同项目的新会话要能拿到更新后的结论，而不是旧的。"""
    writer, retriever, service, repos = _stack(tmp_path)
    bound = "trusted-project"
    first_scope = _scope(conversation="c1")
    old = _save(
        writer,
        first_scope,
        "预算五千",
        kind="fact",
        status="reported",
        scope_kind="project",
        project_id=bound,
        binding=bound,
    )
    outcome = _correct(service, first_scope, old, "预算三千", project_id=bound)
    assert outcome["applied"] is True, outcome["reason"]

    later_scope = _scope(conversation="c-new")
    result = retriever.retrieve(later_scope, "预算", project_id=bound)
    assert "预算三千" in {row["content"] for row in result.warm}
    repos.close()


def test_another_account_never_reads_the_corrected_choice(tmp_path) -> None:
    writer, retriever, service, repos = _stack(tmp_path)
    scope = _scope()
    old = _save(writer, scope, "预算五千", kind="fact", status="reported")
    _correct(service, scope, old, "预算三千")

    result = retriever.retrieve(_scope(owner="somebody-else"), "预算")
    assert result.items == []
    repos.close()


def _packet_for(scope: Scope, user_text: str):
    from agent_core.memory_turn import MemoryTurnPacket

    return MemoryTurnPacket(
        turn_id="turn-1",
        occurred_at="2026-10-11T12:00:00+00:00",
        scope_owner=scope.owner_id,
        scope_channel=scope.channel,
        scope_conversation=scope.conversation_id,
        user_text=user_text,
        published_reply="好",
    )


def test_turning_semantic_recall_off_hides_the_new_version_entirely(tmp_path) -> None:
    writer, retriever, service, repos = _stack(tmp_path)
    scope = _scope()
    older = _save(writer, scope, "旧格式记忆锚点", kind="fact", status="reported")
    writer.save_memory(
        scope,
        "旧格式记忆回滚锚点",
        conflict_key="anchor",
        confidence=0.9,
        metadata={"related_ids": [older["id"]]},
    )

    result = retriever.retrieve(scope, "锚点", include_semantic=False)
    assert older["id"] not in {row["id"] for row in result.items}
    assert result.metadata["expanded"] == 0
    repos.close()
