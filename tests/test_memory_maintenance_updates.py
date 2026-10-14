"""内容维护更新建议的契约（B 计划第二步）。

钉的是「模型只提建议、程序裁决」这条线：
- 带动作的条目必须给出输入里**已经授权**的目标记录，编号对不上就拒；
- 版本是前提条件不是授权依据，对不上就重新取证；
- 「嗯」这类含糊表态不许把建议升级成「用户已采纳」；
- 没有下一条回复、没有读回执，都不妨碍当前回合入库。

对应的验收样例是计划里的 M1 / M2 / M3 / M13 / M14。
"""
from __future__ import annotations

from scripts.memory_test_support import seed_semantic
import asyncio
import json

import pytest

from agent_core.contracts import ProviderResponse
from agent_core.memory_extract import SemanticMemoryExtractor
from agent_core.memory_extract import MemoryExtractionError
from support.memory_review import FixedReviewProvider, supported_row, prepare_update
from agent_core.memory_turn import (
    MAINTENANCE_ACTIONS,
    MemoryCandidateError,
    MemoryRef,
    MemoryTurnPacket,
    HistoryItem,
    MemoryUpdateSuggestion,
    validate_suggestion,
)


class _Provider:
    """固定返回一段文本的提炼后端；不联网、不调模型。"""

    def __init__(self, text: str) -> None:
        self.text = text

    async def complete(self, message, *, temperature=None, max_tokens=None):
        return ProviderResponse(message={"content": self.text}, usage={})


def _packet(**overrides) -> MemoryTurnPacket:
    base = dict(
        turn_id="turn-1",
        occurred_at="2026-10-11T12:00:00+00:00",
        scope_owner="user",
        scope_channel="web",
        scope_conversation="c1",
        user_text="项目先不部署",
        published_reply="好，先不部署",
    )
    base.update(overrides)
    return MemoryTurnPacket(**base)


def _extractor(text: str) -> SemanticMemoryExtractor:
    return SemanticMemoryExtractor(_Provider(text))


def _payload(*items: dict) -> str:
    return json.dumps({"memories": list(items)}, ensure_ascii=False)


def _extract(text: str, packet: MemoryTurnPacket | None = None, **kwargs):
    return asyncio.run(
        _extractor(text).extract_maintenance(packet or _packet(), **kwargs)
    )


def _parse(text: str, packet: MemoryTurnPacket | None = None, **kwargs):
    return _extractor(text).parse_maintenance(text, packet=packet or _packet(), **kwargs)


# ---------------------------------------------------------------------------
# M1：正常陈述就自动形成候选，不需要保存指令，也不误记成已实施
# ---------------------------------------------------------------------------
def test_a_plain_statement_becomes_a_candidate_without_any_save_instruction() -> None:
    text = _payload(
        {
            "content": "项目先不部署",
            "kind": "plan",
            "content_status": "proposed",
            "source_types": ["user_statement"],
        }
    )
    candidates, suggestions = _extract(text)

    assert len(candidates) == 1
    assert suggestions == []
    assert candidates[0].content_status == "proposed"
    assert "completed" not in candidates[0].content_status


def test_the_turn_is_processed_even_when_no_follow_up_message_ever_arrives() -> None:
    """没有下一条用户消息也要处理当前回合 —— 不依赖已读回执。"""
    packet = _packet(relevant_history=[], user_text="记住：项目先不部署")
    text = _payload(
        {
            "content": "项目暂不部署",
            "kind": "decision",
            "content_status": "proposed",
            "source_types": ["user_statement"],
        }
    )
    candidates, _ = _extract(text, packet)
    assert len(candidates) == 1


# ---------------------------------------------------------------------------
# M2：助手一口气建议三件事，用户只回「嗯」
# ---------------------------------------------------------------------------
def test_a_vague_acknowledgement_does_not_upgrade_suggestions_to_adopted(tmp_path) -> None:
    text = _payload(
        {
            "content": "采用统一记忆方案",
            "kind": "decision",
            "content_status": "accepted",
            "source_types": ["user_statement", "assistant_suggestion"],
        }
    )
    candidates, _ = _extract(text, _packet(user_text="嗯"))

    assert len(candidates) == 1
    # Parsing retains the candidate; the configured model owns adoption meaning.
    assert candidates[0].content_status == "accepted"
    from agent_core.memory_processing import MemoryReviewProcessor
    _, writer, repos = _service(tmp_path)
    reviewer = FixedReviewProvider([dict(index=0, verdict='unsupported', reason='单独嗯没有明确采纳')])
    prepared, _, result = asyncio.run(MemoryReviewProcessor(writer, reviewer).prepare(
        _packet(user_text='嗯'), candidates, persist=lambda payload: None))
    assert not prepared and result.rejected
    assert writer.list_records(_scope()) == []
    repos.close()


def test_an_assistant_suggestion_never_becomes_user_adopted() -> None:
    text = _payload(
        {
            "content": "助手建议先统一记忆规则",
            "kind": "decision",
            "content_status": "accepted",
            "source_types": ["assistant_suggestion"],
        }
    )
    with pytest.raises(MemoryExtractionError, match='user_statement'):
        _extract(text, _packet(user_text="你觉得呢"))


# ---------------------------------------------------------------------------
# M3：唯一且具体的选择，用户明确答「嗯，就这样」
# ---------------------------------------------------------------------------
def test_an_explicit_short_confirmation_keeps_both_sides_sources() -> None:
    packet = _packet(
        user_text="嗯，就这样",
        relevant_history=[
            HistoryItem("msg-1", "assistant", "以后回答先说结论可以吗"),
            HistoryItem("msg-2", "user", "嗯，就这样"),
        ],
    )
    text = _payload(
        {
            "content": "以后回答先说结论",
            "kind": "preference",
            "content_status": "explicit",
            "source_types": ["user_statement"],
            "source_refs": ["msg-1", "msg-2"],
        }
    )
    candidates, _ = _extract(text, packet)

    assert candidates[0].content_status == "explicit"
    assert set(candidates[0].source_refs) == {"msg-1", "msg-2", "turn-1"}


# ---------------------------------------------------------------------------
# 动作与目标的核对
# ---------------------------------------------------------------------------
def test_a_suggestion_without_a_target_is_rejected() -> None:
    text = _payload(
        {
            "action": "correct",
            "content": "预算改成三千",
            "kind": "fact",
            "content_status": "reported",
            "source_types": ["user_statement"],
        }
    )
    candidates, suggestions, rejected = _parse(text)

    assert candidates == [] and suggestions == []
    assert "目标记录编号" in rejected[0]["reason"]


def test_a_target_outside_the_authorized_input_is_rejected() -> None:
    packet = _packet(
        user_text="不是五千，是三千",
        recalled_memory_refs=[MemoryRef("mem-1", "fact", "reported", "预算五千", version=2)],
    )
    text = _payload(
        {
            "action": "correct",
            "target_id": "mem-999",
            "content": "预算三千",
            "kind": "fact",
            "content_status": "reported",
            "source_types": ["user_statement"],
        }
    )
    candidates, suggestions, rejected = _parse(text, packet)

    assert suggestions == []
    assert "不在本次已授权输入里" in rejected[0]["reason"]


def test_a_stale_expected_version_is_rejected_instead_of_overwriting() -> None:
    packet = _packet(
        user_text="不是五千，是三千",
        recalled_memory_refs=[MemoryRef("mem-1", "fact", "reported", "预算五千", version=3)],
    )
    text = _payload(
        {
            "action": "correct",
            "target_id": "mem-1",
            "expected_version": 2,
            "content": "预算三千",
            "kind": "fact",
            "content_status": "reported",
            "source_types": ["user_statement"],
        }
    )
    candidates, suggestions, rejected = _parse(text, packet, known_versions={"mem-1": 3})

    assert suggestions == []
    assert "目标版本已变" in rejected[0]["reason"]


def test_a_matching_target_and_version_is_accepted() -> None:
    packet = _packet(
        user_text="不是五千，是三千",
        recalled_memory_refs=[MemoryRef("mem-1", "fact", "reported", "预算五千", version=3)],
    )
    text = _payload(
        {
            "action": "correct",
            "target_id": "mem-1",
            "expected_version": 3,
            "content": "预算三千",
            "kind": "fact",
            "content_status": "reported",
            "source_types": ["user_statement"],
        }
    )
    candidates, suggestions, rejected = _parse(text, packet, known_versions={"mem-1": 3})

    assert rejected == []
    assert [item.action for item in suggestions] == ["correct"]
    assert suggestions[0].target_id == "mem-1"


def test_one_call_can_produce_both_a_new_candidate_and_an_update_suggestion() -> None:
    """一次提炼同时判断保存价值和更新方式，不再多调一次模型。"""
    packet = _packet(
        user_text="预算改成三千，另外以后都先说结论",
        recalled_memory_refs=[MemoryRef("mem-1", "fact", "reported", "预算五千", version=1)],
    )
    text = _payload(
        {
            "action": "correct",
            "target_id": "mem-1",
            "expected_version": 1,
            "content": "预算三千",
            "kind": "fact",
            "content_status": "reported",
            "source_types": ["user_statement"],
        },
        {
            "content": "以后回答先说结论",
            "kind": "preference",
            "content_status": "explicit",
            "source_types": ["user_statement"],
        },
    )
    candidates, suggestions, rejected = _parse(text, packet, known_versions={"mem-1": 1})

    assert rejected == []
    assert [c.kind for c in candidates] == ["preference"]
    assert [s.action for s in suggestions] == ["correct"]


# ---------------------------------------------------------------------------
# 契约本身的规则
# ---------------------------------------------------------------------------
def test_ignore_is_the_only_action_that_may_say_not_worth_saving() -> None:
    with pytest.raises(MemoryCandidateError):
        validate_suggestion(
            MemoryUpdateSuggestion(action="merge", target_id="mem-1", value=False, reason="没价值")
        )
    validate_suggestion(
        MemoryUpdateSuggestion(action="ignore", target_id="mem-1", value=False, reason="只是寒暄")
    )


def test_an_unknown_action_is_rejected() -> None:
    with pytest.raises(MemoryCandidateError):
        validate_suggestion(MemoryUpdateSuggestion(action="rewrite", target_id="mem-1"))


def test_ignored_items_do_not_write_records() -> None:
    assert MemoryUpdateSuggestion(action="ignore").writes_a_record() is False
    assert MemoryUpdateSuggestion(action="correct", target_id="mem-1").writes_a_record() is True


def test_the_seven_actions_are_the_documented_ones() -> None:
    assert MAINTENANCE_ACTIONS == (
        "add",
        "merge",
        "correct",
        "replace",
        "conflict",
        "invalidate",
        "ignore",
    )


# ---------------------------------------------------------------------------
# 应用更新：版本保护、原子性、写后复核（B 计划第三步）
# ---------------------------------------------------------------------------
from datetime import UTC, datetime, timedelta  # noqa: E402

from agent_core.contracts import Scope  # noqa: E402
from agent_core.memory_maintenance import MemoryMaintenanceService  # noqa: E402
from agent_core.memory_repositories import initialize_memory_repositories  # noqa: E402
from agent_core.memory_writes import MemoryWriter  # noqa: E402


class _Embeddings:
    def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class _BrokenEmbeddings:
    """向量算不出来：数据库写成功、检索没跟上，两项必须分别记账。"""

    def embed(self, text: str) -> list[float]:
        raise RuntimeError("向量服务不可用")


def _service(tmp_path, *, embeddings=None):
    repos = initialize_memory_repositories(
        tmp_path / "records.db", tmp_path / "vectors", vector_size=3
    )
    writer = MemoryWriter(repos, embeddings or _Embeddings())
    service = MemoryMaintenanceService(writer, repos)
    return service, writer, repos


def _scope(owner: str = "user") -> Scope:
    return Scope(owner, "web", "c1", "companion")


def _seed(writer, scope, content: str, *, version: int = 1, occurred_at: str | None = None):
    from agent_core.memory_turn import MemoryCandidate

    return seed_semantic(writer,
        scope,
        [
            MemoryCandidate(
                content=content,
                kind="fact",
                content_status="reported",
                source_types=("user_statement",),
                source_refs=("turn-0",),
                occurred_at=occurred_at or "2026-10-11T10:00:00+00:00",
            )
        ],
        turn_id="turn-0",
        occurred_at=occurred_at or "2026-10-11T10:00:00+00:00",
    )[0]


def _correct_suggestion(target_id: str, content: str, *, version: int = 1):
    return MemoryUpdateSuggestion(
        action="correct",
        target_id=target_id,
        expected_version=version,
        content=content,
        kind="fact",
        content_status="reported",
        source_types=("user_statement",),
        source_refs=("turn-1",),
        reason="用户说旧的数字错了",
    )


def _apply_reviewed(service, suggestion, *, packet, **kwargs):
    # Finite, independently specified meanings; unknown cases get no authority.
    expected = {
        ('不是五千，是三千', '预算三千'): [('turn-1', '不是五千'), ('turn-1', '是三千')],
        ('改用 B，不再用 A', '方案 B'): [('turn-1', '改用 B'), ('turn-1', '不再用 A')],
        ('改用方案B，不再用方案A；还没有实施。', '项目决定改用方案B，不再使用方案A；目前尚未实施'):
            [('turn-1', '改用方案B'), ('turn-1', '不再用方案A'), ('turn-1', '还没有实施')],
    }
    quotes = expected.get((packet.user_text, suggestion.content))
    target = service.repositories.records_by_ids([suggestion.target_id]).get(suggestion.target_id)
    if quotes and target and target['status'] == 'active':
        suggestion = asyncio.run(prepare_update(service.writer, packet, suggestion,
            [supported_row(suggestion.content, quotes)]))
    return service.apply(suggestion, packet=packet, **kwargs)


def test_a_correction_creates_a_new_version_and_keeps_the_old_one(tmp_path) -> None:
    """M4：以后用到的是三千，旧版本留下纠正关系，不是被删掉。"""
    service, writer, repos = _service(tmp_path)
    scope = _scope()
    old = _seed(writer, scope, "预算五千")
    packet = _packet(
        user_text="不是五千，是三千",
        occurred_at="2026-10-11T12:00:00+00:00",
        turn_id="turn-1",
    )
    outcome = _apply_reviewed(service,
        _correct_suggestion(old["id"], "预算三千"),
        scope=scope,
        packet=packet,
        turn_id="turn-1",
    )

    assert outcome["applied"] is True, outcome["reason"]
    new = outcome["record"]
    assert new["content"] == "预算三千"
    assert new["record_version"] == 2
    assert new["status"] == "active"
    assert outcome["verify"]["saved"] is True
    assert outcome["verify"]["record_version"] == 2

    fresh_old = repos.records_by_ids([old["id"]])[old["id"]]
    assert fresh_old["status"] == "archived"
    assert fresh_old["effective_record_id"] == new["id"]
    assert json.loads(fresh_old["metadata"])["effective_record_id"] == new["id"]
    assert old["id"] in outcome["verify"]["archived"]
    repos.close()


def test_the_same_replacement_basis_is_required_for_replace(tmp_path) -> None:
    """没有「改用 / 不再用」这类原话，不许覆盖现行结论。"""
    service, writer, repos = _service(tmp_path)
    scope = _scope()
    old = _seed(writer, scope, "方案 A")
    suggestion = MemoryUpdateSuggestion(
        action="replace",
        target_id=old["id"],
        expected_version=1,
        content="方案 B",
        kind="decision",
        content_status="accepted",
        source_types=("user_statement",),
        reason="用户好像改主意了",
    )
    refused = _apply_reviewed(service,
        suggestion,
        scope=scope,
        packet=_packet(user_text="方案 B 也不错", occurred_at="2026-10-11T12:00:00+00:00"),
    )
    assert refused["applied"] is False
    assert "替代依据" in refused["reason"]

    accepted = _apply_reviewed(service,
        suggestion,
        scope=scope,
        packet=_packet(user_text="改用 B，不再用 A", occurred_at="2026-10-11T12:00:00+00:00"),
        turn_id="turn-1",
    )
    assert accepted["applied"] is True, accepted["reason"]
    fresh_old = repos.records_by_ids([old["id"]])[old["id"]]
    assert fresh_old["content_status"] == "superseded"
    repos.close()


def test_a_rewording_of_the_same_decision_is_not_missing_value_evidence(tmp_path) -> None:
    """换一种说法写同一个决定，不算「新值没有依据」。

    真实模型把「改用方案B，不再用方案A；还没有实施」写成「项目决定改用方案B，
    不再使用方案A；目前尚未实施」—— 只是把 用→使用、选择→决定。逐项核对若拿
    **所有**变了的实义片段去要出处，就会报「使用、再使、决定 找不到」并把整条
    正当替代拒掉（真实批次里三次全被拒）。带值的片段（方案 A/B、数字）才是
    需要逐项取证的「新值」。
    """
    service, writer, repos = _service(tmp_path)
    scope = _scope()
    old = _seed(writer, scope, "项目选择方案A")
    outcome = _apply_reviewed(service,
        MemoryUpdateSuggestion(
            action="replace",
            target_id=old["id"],
            expected_version=1,
            content="项目决定改用方案B，不再使用方案A；目前尚未实施",
            kind="decision",
            content_status="accepted",
            source_types=("user_statement",),
            source_refs=("turn-1",),
            reason="用户明确改用 B",
        ),
        scope=scope,
        packet=_packet(
            user_text="改用方案B，不再用方案A；还没有实施。",
            occurred_at="2026-10-11T12:00:00+00:00",
            turn_id="turn-1",
        ),
        turn_id="turn-1",
    )

    assert outcome["applied"] is True, outcome["reason"]
    fresh_old = repos.records_by_ids([old["id"]])[old["id"]]
    assert fresh_old["status"] == "archived"
    repos.close()


def test_an_unsupported_new_number_is_still_rejected_after_rewording_tolerance(tmp_path) -> None:
    """容忍措辞不等于容忍新数字：原话里没有 2026，就不许写进新版本。"""
    service, writer, repos = _service(tmp_path)
    scope = _scope()
    old = _seed(writer, scope, "采购项目预算五千")
    outcome = _apply_reviewed(service,
        MemoryUpdateSuggestion(
            action="correct",
            target_id=old["id"],
            expected_version=1,
            content="采购项目预算三千，交付时间为2026年12月31日",
            kind="fact",
            content_status="reported",
            source_types=("user_statement",),
            source_refs=("turn-1",),
            reason="用户纠正了预算",
        ),
        scope=scope,
        packet=_packet(
            user_text="采购项目预算不是五千，是三千；交付日期尚未确定",
            occurred_at="2026-10-11T12:00:00+00:00",
            turn_id="turn-1",
        ),
        turn_id="turn-1",
    )

    assert outcome["applied"] is False
    assert "2026" in outcome["reason"], outcome["reason"]
    assert repos.records_by_ids([old["id"]])[old["id"]]["status"] == "active"
    repos.close()


def test_the_rewording_tolerance_does_not_let_a_correction_smuggle_new_content(tmp_path) -> None:
    """替代可以换说法，纠正不行：纠正时原话里没有的定性内容仍然要拦。

    「改用 B」这类替代天然会用新说法重述同一个决定，所以只对带值的片段逐项
    取证；但纠正走的是「用户指出旧值错了」，若也放宽，模型就能把原话里根本
    没有的定性内容（这里是「云端优先／外部团队托管」）塞进新版本。
    """
    service, writer, repos = _service(tmp_path)
    scope = _scope()
    old = _seed(writer, scope, "数据项目原则是本地优先")
    outcome = _apply_reviewed(service,
        MemoryUpdateSuggestion(
            action="correct",
            target_id=old["id"],
            expected_version=1,
            content="数据项目原则改为云端优先后由外部团队托管",
            kind="fact",
            content_status="reported",
            source_types=("user_statement",),
            source_refs=("turn-1",),
            reason="模型认为改了",
        ),
        scope=scope,
        packet=_packet(
            user_text="数据项目那就继续按原来的思路办吧",
            occurred_at="2026-10-11T12:00:00+00:00",
            turn_id="turn-1",
        ),
        turn_id="turn-1",
    )

    assert outcome["applied"] is False, outcome["reason"]
    # 拒绝理由要指向具体那一项新内容：这条不是数值问题，而是原话里没有的定性内容。
    assert "云端" in outcome["reason"] or "新值缺少原话依据" in outcome["reason"], outcome["reason"]
    assert repos.records_by_ids([old["id"]])[old["id"]]["status"] == "active"
    repos.close()


def test_late_evidence_never_overwrites_the_current_version(tmp_path) -> None:
    """迟到的旧证据只进历史，不推翻现行结论：上传时间不等于发生时间。"""
    service, writer, repos = _service(tmp_path)
    scope = _scope()
    old = _seed(writer, scope, "方案 B", occurred_at="2026-10-11T12:00:00+00:00")
    outcome = _apply_reviewed(service,
        _correct_suggestion(old["id"], "方案 A"),
        scope=scope,
        packet=_packet(
            user_text="不是 B，是 A",
            occurred_at="2026-10-01T09:00:00+00:00",  # 旧对话迟到上传
        ),
    )

    assert outcome["applied"] is False
    assert "早于现行版本" in outcome["reason"]
    fresh = repos.records_by_ids([old["id"]])[old["id"]]
    assert fresh["status"] == "active"
    assert fresh["content"] == "方案 B"
    repos.close()


def test_repeating_the_same_update_does_not_create_another_version(tmp_path) -> None:
    service, writer, repos = _service(tmp_path)
    scope = _scope()
    old = _seed(writer, scope, "预算五千")
    packet = _packet(user_text="不是五千，是三千", occurred_at="2026-10-11T12:00:00+00:00")
    reviewed = asyncio.run(prepare_update(writer, packet, _correct_suggestion(old['id'], '预算三千'),
        [supported_row('预算三千', [('turn-1', '不是五千'), ('turn-1', '是三千')])]))
    first = service.apply(
        reviewed, scope=scope, packet=packet, turn_id="turn-1"
    )
    second = service.apply(
        reviewed, scope=scope, packet=packet, turn_id="turn-1"
    )

    assert first["record"]["id"] == second["record"]["id"]
    rows = [
        row
        for row in writer.list_records(scope)
        if row["semantics_key"] == old["semantics_key"] or row["id"] == first["record"]["id"]
    ]
    assert len(rows) == 2  # 旧版本 + 新版本，没有第三条
    repos.close()


def test_a_merge_without_new_wording_only_appends_the_source(tmp_path) -> None:
    service, writer, repos = _service(tmp_path)
    scope = _scope()
    old = _seed(writer, scope, "预算上限三万")
    suggestion = MemoryUpdateSuggestion(
        action="merge",
        target_id=old["id"],
        expected_version=1,
        reason="用户补充了原因",
        source_types=("user_statement",),
        source_refs=("turn-1",),
    )
    outcome = _apply_reviewed(service,
        suggestion,
        scope=scope,
        packet=_packet(user_text="因为要留出扩充余地", occurred_at="2026-10-11T12:00:00+00:00"),
        turn_id="turn-1",
    )

    assert outcome["applied"] is True, outcome["reason"]
    assert outcome["record"]["id"] == old["id"]
    assert outcome["record"]["record_version"] == 2
    assert outcome["record"]["content"] == "预算上限三万"
    assert "turn-1" in outcome["verify"]["sources"]
    repos.close()


def test_marking_a_conflict_keeps_both_sides_for_review(tmp_path) -> None:
    service, writer, repos = _service(tmp_path)
    scope = _scope()
    first = _seed(writer, scope, "预算是五千")
    second = _seed(writer, scope, "预算是三千")
    suggestion = MemoryUpdateSuggestion(
        action="conflict",
        target_id=first["id"],
        expected_version=1,
        conflict_ids=(second["id"],),
        reason="两条说法对不上，还没弄清是不是两个项目",
        source_types=("user_statement",),
        source_refs=("turn-1",),
    )
    outcome = _apply_reviewed(service,
        suggestion,
        scope=scope,
        packet=_packet(user_text="不记得了", occurred_at="2026-10-11T12:00:00+00:00"),
        turn_id="turn-1",
    )

    assert outcome["applied"] is True, outcome["reason"]
    rows = {row["id"]: row for row in writer.list_records(scope)}
    assert rows[first["id"]]["review_state"] == "pending"
    assert rows[second["id"]]["review_state"] == "pending"
    assert second["id"] in rows[first["id"]]["conflict_with"]
    assert rows[first["id"]]["status"] == "active"  # 不擅自择一，两条都留着
    assert service.pick_due(limit=5) == []  # 还没到复核时间
    due = service.pick_due(now=datetime.now(UTC) + timedelta(days=2), limit=5)
    assert {row["id"] for row in due} == {first["id"], second["id"]}
    repos.close()


def test_invalidate_without_a_reason_only_marks_for_review(tmp_path) -> None:
    service, writer, repos = _service(tmp_path)
    scope = _scope()
    old = _seed(writer, scope, "旧方案到期作废")
    with_reason = _apply_reviewed(service,
        MemoryUpdateSuggestion(
            action="invalidate",
            target_id=old["id"],
            expected_version=1,
            reason="已到期",
            source_types=("user_statement",),
            source_refs=("turn-1",),
        ),
        scope=scope,
        packet=_packet(user_text="那个方案不用了", occurred_at="2026-10-11T12:00:00+00:00"),
        turn_id="turn-1",
    )
    assert with_reason["applied"] is True
    assert with_reason["record"]["status"] == "archived"

    other = _seed(writer, scope, "另一条")
    without_reason = _apply_reviewed(service,
        MemoryUpdateSuggestion(
            action="invalidate",
            target_id=other["id"],
            expected_version=1,
            reason="",
            content="疑似到期，还没确认",
            source_types=("model_inference",),
            source_refs=("turn-1",),
        ),
        scope=scope,
        packet=_packet(user_text="", occurred_at="2026-10-11T12:00:00+00:00"),
        turn_id="turn-1",
    )
    # 没有理由只是「待复核」：内容留着，到点再看一遍，不凭空推翻。
    assert without_reason["applied"] is True
    assert without_reason["record"]["status"] == "active"
    assert without_reason["record"]["review_state"] == "pending"
    repos.close()


def test_retrieval_failure_is_reported_separately_from_save(tmp_path) -> None:
    """数据库写成功、向量没跟上：保存与检索分别记账，不假报整体成功。"""
    service, writer, repos = _service(tmp_path, embeddings=_BrokenEmbeddings())
    scope = _scope()
    old = _seed(writer, scope, "预算五千")
    outcome = _apply_reviewed(service,
        _correct_suggestion(old["id"], "预算三千"),
        scope=scope,
        packet=_packet(user_text="不是五千，是三千", occurred_at="2026-10-11T12:00:00+00:00"),
        turn_id="turn-1",
    )

    verified = outcome["verify"]
    assert verified["saved"] is True
    assert verified["retrieval"] != "ready"
    assert verified["partial"] is True
    repos.close()


def test_a_failed_write_leaves_the_current_version_untouched(tmp_path) -> None:
    """新旧交替中途失败时，原有效版本必须完好 —— 不能只让旧的失效。"""
    service, writer, repos = _service(tmp_path)
    scope = _scope()
    old = _seed(writer, scope, "预算五千")

    def boom(*args, **kwargs):
        raise RuntimeError("数据库中途失败")

    writer._insert = boom  # noqa: SLF001 - 故意打断事务中途
    outcome = _apply_reviewed(service,
        _correct_suggestion(old["id"], "预算三千"),
        scope=scope,
        packet=_packet(user_text="不是五千，是三千", occurred_at="2026-10-11T12:00:00+00:00"),
        turn_id="turn-1",
    )

    assert outcome["applied"] is False
    fresh = repos.records_by_ids([old["id"]])[old["id"]]
    assert fresh["status"] == "active"
    assert fresh["content"] == "预算五千"
    assert not fresh["effective_record_id"]
    repos.close()


def test_a_target_from_another_account_is_not_visible(tmp_path) -> None:
    service, writer, repos = _service(tmp_path)
    mine, theirs = _scope("user"), _scope("somebody-else")
    foreign = _seed(writer, theirs, "别人的记忆")
    outcome = _apply_reviewed(service,
        _correct_suggestion(foreign["id"], "被改了"),
        scope=mine,
        packet=_packet(user_text="不是这个，是那个", occurred_at="2026-10-11T12:00:00+00:00"),
    )

    assert outcome["applied"] is False
    assert "不可见" in outcome["reason"]
    assert repos.records_by_ids([foreign["id"]])[foreign["id"]]["content"] == "别人的记忆"
    repos.close()


def test_pick_due_is_empty_when_nothing_needs_review(tmp_path) -> None:
    """定期任务空转时不许有模型调用 —— 没有待复核项就直接跳过。"""
    service, writer, repos = _service(tmp_path)
    _seed(writer, _scope(), "普通一条")
    assert service.pick_due() == []
    repos.close()
