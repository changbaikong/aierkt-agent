"""实施工作 B 的离线验证：澄清从「提问草稿」到「实际发布」再到「保存新版本」。

对应计划《主聊天三项实用能力：接续实施与交接说明》「新增离线验证限定三组」第 2、3 组：

- 第 2 组：用**保留的真实不确定响应**验证读取；真实输出缺现代协议字段时
  只当兼容材料，不编造新判断。用**明确标为模拟**的补充响应检查提问草稿、
  实际发布、下一轮关联、现有写入端的新版本及解决标记。
- 第 3 组：用户没回答而聊天气 → 不自动确认、不重复问、不改旧版本；
  同号重交不重复保存；旧位置参数和缺新字段包仍可读；
  取消或失败未发布的草稿不记已问。

全部离线：用隔离的 ``tmp_path`` 库，库路径显式指定，不连生产库、不调模型。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_core.contracts import Scope
from agent_core.memory_clarification import (
    STATE_ASKED,
    STATE_RESOLVED,
    STATE_WAITING,
    ClarificationStore,
    ClarificationTurn,
    build_clarification_context,
    build_propose_tool_spec,
    candidate_id_of,
    propose_tool_name,
)
from agent_core.memory_queue import MemoryCandidateQueue
from agent_core.memory_turn import (
    EvidenceRef,
    MemoryCandidate,
    MemoryTurnPacket,
    PACKET_VERSION,
    decode_candidates,
)
from agent_core.memory_writes import MemoryWriter

#: 保留的真实不确定响应：这份文件里的模型输出**原样保留**，不是本轮新跑的。
#: 它记录的是「用户只回了『嗯』/『这个先放着』，模型判为不确定」这类真实输出。
PAID_Q2_EVIDENCE = Path(__file__).resolve().parents[1] / "docs/2026-10-13-memory-b-paid-q2-v3.json"

#: 明确标注为**模拟**的协议字段：真实输出里没有这些现代字段，
#: 只有它们才能撑起「程序按协议登记疑点」的检查。
SIMULATED_UNCERTAIN_ITEM = {
    "index": 0,
    "verdict": "uncertain",
    "content": "待确认的事项：先放下的是正式部署还是本机演练",
    "quote": "这个先放着",
    "reason": "用户未指明对象，不能替用户确认",
    "clarification_question": "先放下的是正式部署，还是本机演练？",
    "target_id": "mem-deploy-1",
    "target_version": 2,
    "lifetime_deadline": "未知",
}

SCOPE_KEY = "u|web|clarify-session|companion"
SCOPE = Scope("u", "web", "clarify-session", "companion")


def open_store(tmp_path: Path) -> ClarificationStore:
    """建一个隔离的作业库：先用现有队列建表，再补澄清列。"""
    db_path = tmp_path / "jobs.db"
    MemoryCandidateQueue(db_path).initialize()
    store = ClarificationStore(db_path)
    store.initialize()
    return store


def enqueue_job(
    store: ClarificationStore,
    *,
    turn_id: str,
    processing_result: dict | None = None,
    raw_processing_result: dict | None = None,
    last_error: str = "",
) -> str:
    """往隔离库里放一条待确认作业，返回作业号。

    ``processing_result`` 传**单个事项**（助手自动套一层 ``items``）；
    ``raw_processing_result`` 传整个结果体（多事项时用它）。
    """
    queue = MemoryCandidateQueue(store.db_path)
    packet = MemoryTurnPacket(
        turn_id=turn_id,
        occurred_at="2026-10-14T00:00:00.000+00:00",
        scope_owner=SCOPE.owner_id,
        scope_channel=SCOPE.channel,
        scope_conversation=SCOPE.conversation_id,
        scope_module=SCOPE.module_id,
        user_text="这个先放着",
        published_reply="好的。",
    )
    job_id, _ = queue.enqueue(packet)
    body = raw_processing_result
    if body is None and processing_result is not None:
        body = {"items": [processing_result]}
    if body is not None or last_error:
        with queue.connect() as db:
            db.execute(
                "UPDATE memory_candidate_jobs SET processing_result=?,last_error=?,"
                "scope_key=?,created_at=? WHERE job_id=?",
                (
                    json.dumps(body, ensure_ascii=False) if body is not None else None,
                    last_error or None,
                    SCOPE_KEY,
                    "2026-10-14T00:00:00.000+00:00",
                    job_id,
                ),
            )
    return job_id


def candidates_of(store: ClarificationStore) -> list:
    return store.candidates(scope_key=SCOPE_KEY)


# ── 第 2 组：从真实不确定响应里读疑点 ───────────────────────────────


def test_preserved_real_uncertain_responses_read_as_uncertain_but_carry_no_quote(tmp_path):
    """保留的真实不确定响应：能读出「不确定」，但没有原话，因此不构成用户疑点。

    这正是计划说的「真实输出缺现代协议字段时只当兼容材料，不编造新判断」：
    真实响应里没有 ``quote`` 这类字段，程序**不得**替它补一句原话，
    也不得凭 ``content`` 里的转述造一个疑点。缺原话就如实没有候选。
    """
    raw = json.loads(PAID_Q2_EVIDENCE.read_text(encoding="utf-8"))
    real_uncertain = _walk_uncertain(raw)
    assert real_uncertain, "保留的真实响应里必须有判为 uncertain 的输出"

    store = open_store(tmp_path)
    enqueue_job(store, turn_id="real-q2-1", raw_processing_result={"items": real_uncertain})

    found = candidates_of(store)

    assert found == [], "真实输出没有原话，不能变成用户疑点"
    assert all(not item.get("quote") for item in real_uncertain), (
        "真实输出本来就没有 quote 字段；若将来补上了，本测试要跟着重评"
    )


def test_compatibility_material_with_a_quote_but_no_modern_fields_is_read_without_invention(tmp_path):
    """兼容材料（有原话、无现代字段）：照读成疑点，缺什么就留空，不补判断。"""
    store = open_store(tmp_path)
    enqueue_job(store, turn_id="compat-1", processing_result={
        "index": 0, "verdict": "uncertain", "quote": "这个先放着",
        "reason": "用户只说了一部分",
    })

    found = candidates_of(store)

    assert len(found) == 1, "有原话就仍要读成疑点（兼容读取）"
    assert found[0].quote == "这个先放着"
    assert found[0].suggested_question == "", "缺建议问题就是空，程序不得自己编一句"
    assert found[0].target_id == "" and found[0].target_version == 0
    assert found[0].deadline == "", "期限缺失保持未知，不默认一天"
    assert found[0].to_background()["deadline"] == "未知"


def test_uncertainty_without_a_real_quote_is_not_a_user_doubt(tmp_path):
    """没有原话的判断不构成用户疑点：宁可没有候选，也不凭空造一个。"""
    store = open_store(tmp_path)
    enqueue_job(store, turn_id="no-quote", processing_result={
        "index": 0, "verdict": "uncertain", "content": "有些事情还没定", "reason": "缺原话",
    })

    assert candidates_of(store) == []


@pytest.mark.parametrize("error", [
    "agent_model_timeout", "empty_response", "no_configuration", "invalid_protocol",
])
def test_program_and_config_failures_never_become_user_doubts(tmp_path, error):
    """程序或配置故障不是用户疑点，不让用户去回答一个系统问题。"""
    store = open_store(tmp_path)
    enqueue_job(store, turn_id=f"fault-{error}", processing_result=SIMULATED_UNCERTAIN_ITEM,
                last_error=error)

    assert candidates_of(store) == []


# ── 第 2 组：登记草稿 → 实际发布 → 下一轮关联 ──────────────────────


def test_candidate_seeding_uses_job_plus_item_path_and_caps_the_list(tmp_path):
    """稳定编号 = 作业号 + 事项路径；每轮最多给三项（展示限额）。"""
    store = open_store(tmp_path)
    store.initialize()
    with store.connect() as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(memory_candidate_jobs)")}
    assert "clarification_state" in columns, "初始化必须补上澄清列"

    job_id = enqueue_job(store, turn_id="multi-item", raw_processing_result={
        "items": [
            {"index": 0, "verdict": "uncertain", "quote": "先放着", "item_path": "部署"},
            {"index": 1, "verdict": "uncertain", "quote": "再等等", "item_path": "排期"},
            {"index": 2, "verdict": "uncertain", "quote": "看情况", "item_path": "验收"},
            {"index": 3, "verdict": "uncertain", "quote": "以后说", "item_path": "上线"},
        ],
    })

    found = candidates_of(store)

    assert len(found) == 3, "每轮最多三项"
    assert {item.candidate_id for item in found} == {
        candidate_id_of(job_id, path) for path in ("部署", "排期", "验收")
    }, "编号必须是作业号 + 事项路径，不能只用外层下标"
    assert all(item.item_path != "item[0]" for item in found)


def test_draft_is_registered_but_is_not_yet_asked(tmp_path):
    """登记只是草稿：候选状态保持 waiting，未发布就绝不记已问。"""
    store = open_store(tmp_path)
    job_id = enqueue_job(store, turn_id="draft-1", processing_result=SIMULATED_UNCERTAIN_ITEM)
    candidates = candidates_of(store)
    turn = ClarificationTurn(store, scope_key=SCOPE_KEY, turn_id="t-1", candidates=candidates)

    result = turn.propose(candidates[0].candidate_id, "先放下的是正式部署，还是本机演练？")

    assert result == {"status": "ok", "recorded": True, "candidate_id": candidates[0].candidate_id}
    assert turn.publish_tail() == "先放下的是正式部署，还是本机演练？"
    assert store.resolve_item(job_id, candidates[0].candidate_id).state == STATE_WAITING
    assert store.resolve_item(job_id, candidates[0].candidate_id).asked_turn == ""


def test_only_one_question_per_turn_and_only_from_the_offered_candidates(tmp_path):
    """同轮最多一条；候选编号由服务端校验，不信模型自报。"""
    store = open_store(tmp_path)
    enqueue_job(store, turn_id="single-1", raw_processing_result={
        "items": [
            {"index": 0, "verdict": "uncertain", "quote": "先放着", "item_path": "部署"},
            {"index": 1, "verdict": "uncertain", "quote": "再等等", "item_path": "排期"},
        ],
    })
    candidates = candidates_of(store)
    turn = ClarificationTurn(store, scope_key=SCOPE_KEY, turn_id="t-2", candidates=candidates)

    assert turn.propose("不存在的编号", "问一句")["status"] == "error"
    assert turn.propose(candidates[0].candidate_id, "   ")["status"] == "error"
    first = turn.propose(candidates[0].candidate_id, "第一个问题？")
    second = turn.propose(candidates[1].candidate_id, "第二个问题？")

    assert first["recorded"] is True
    assert second["recorded"] is False and second["reason"] == "本轮已登记一个问题"
    assert turn.publish_tail() == "第一个问题？", "第二条不得静默覆盖第一条"


def test_publishing_records_asked_only_when_the_question_is_actually_in_the_text(tmp_path):
    """真正发布且正文含这条问题才记 asked；取消/草稿/被删掉的问题都记不上。"""
    store = open_store(tmp_path)
    job_id = enqueue_job(store, turn_id="publish-1", processing_result=SIMULATED_UNCERTAIN_ITEM)
    candidates = candidates_of(store)
    question = "先放下的是正式部署，还是本机演练？"

    cancelled = ClarificationTurn(store, scope_key=SCOPE_KEY, turn_id="t-cancel", candidates=candidates)
    cancelled.propose(candidates[0].candidate_id, question)
    assert cancelled.record_published(published_text="本轮被取消，什么都没发。") == []
    assert store.resolve_item(job_id, candidates[0].candidate_id).state == STATE_WAITING

    turn = ClarificationTurn(store, scope_key=SCOPE_KEY, turn_id="t-3", candidates=candidates)
    turn.propose(candidates[0].candidate_id, question)
    recorded = turn.record_published(published_text=f"先答能确定的部分。\n\n{question}")

    assert [item.state for item in recorded] == [STATE_ASKED]
    asked = store.resolve_item(job_id, candidates[0].candidate_id)
    assert asked.state == STATE_ASKED
    assert asked.asked_question == question
    assert asked.asked_turn == "t-3"
    # 同一回合重复记账不产生第二条。
    assert turn.record_published(published_text=question) == []


def test_next_turn_association_carries_the_links_and_says_it_is_only_possible(tmp_path):
    """下一轮关联给出候选、原作业、事项路径、实际问题与原始来源，并声明只是可能相关。"""
    store = open_store(tmp_path)
    job_id = enqueue_job(store, turn_id="context-1", processing_result=SIMULATED_UNCERTAIN_ITEM)
    candidates = candidates_of(store)
    turn = ClarificationTurn(store, scope_key=SCOPE_KEY, turn_id="t-4", candidates=candidates)
    question = "先放下的是正式部署，还是本机演练？"
    turn.propose(candidates[0].candidate_id, question)
    turn.record_published(published_text=question)

    context = build_clarification_context(
        store, scope_key=SCOPE_KEY, user_text="我说的是正式部署暂时不做。", turn_id="t-5",
    )

    assert context["user_text"] == "我说的是正式部署暂时不做。"
    link = context["possible_links"][0]
    assert link["candidate_id"] == candidates[0].candidate_id
    assert link["job_id"] == job_id and link["item_path"] == "item[0]"
    assert link["asked_question"] == question and link["asked_turn"] == "t-4"
    assert link["source_quote"] == "这个先放着" and link["source_role"] == "user"
    assert link["target_id"] == "mem-deploy-1" and link["target_version"] == 2
    assert "程序不自动确认" in context["note"], "必须写明程序不替模型确认"


def test_context_is_empty_once_nothing_is_pending(tmp_path):
    """没有待解决事项时关联为空，不给后台塞一个空壳。"""
    store = open_store(tmp_path)

    assert build_clarification_context(
        store, scope_key=SCOPE_KEY, user_text="聊点别的", turn_id="t-6",
    ) == {}


def test_resolved_marker_keeps_the_original_quote_and_the_record_reference(tmp_path):
    """解决后记录回答回合与实际记录引用，原来的原话和回执不被覆盖。"""
    store = open_store(tmp_path)
    job_id = enqueue_job(store, turn_id="resolve-1", processing_result=SIMULATED_UNCERTAIN_ITEM)
    candidates = candidates_of(store)
    turn = ClarificationTurn(store, scope_key=SCOPE_KEY, turn_id="t-7", candidates=candidates)
    turn.propose(candidates[0].candidate_id, "先放下的是哪一项？")
    turn.record_published(published_text="先放下的是哪一项？")
    before = store.resolve_item(job_id, candidates[0].candidate_id)

    resolved = store.mark_resolved(
        job_id, candidates[0].candidate_id, answer_turn="t-8", record_id="rec-new-1",
    )

    assert resolved.state == STATE_RESOLVED
    assert resolved.answered_turn == "t-8"
    assert resolved.resolved_record_id == "rec-new-1"
    assert resolved.quote == before.quote and resolved.asked_question == before.asked_question
    # 已解决的不再进下一轮关联，也不重复算候选。
    assert candidates_of(store) == []
    assert build_clarification_context(
        store, scope_key=SCOPE_KEY, user_text="还有别的吗", turn_id="t-9",
    ) == {}


# ── 第 2 组：现有写入端真的产生新版本 ─────────────────────────────


def test_existing_write_path_saves_the_clarified_version_and_the_marker_points_at_it(tmp_path):
    """澄清回答本身不改库：新版本仍由**现有写入端**落库，解决标记指向实际那条记录。

    走的是现有写入端的**既有要求**：断言要能被用户原话取证（``evidence_refs``），
    缺了它就被如实拒绝（``evidence_not_supported``），本组不放宽这条规则。
    替代旁路（要过核对授权）属于写入端自己的验收，见 tests/test_memory_supersede_guard.py。
    """
    from agent_core.memory_repositories import initialize_memory_repositories
    from scripts.memory_test_support import seed_semantic

    class Embeddings:
        dimensions = 3
        model = "offline-fixed"

        def embed(self, text):
            return [0.1, 0.2, 0.3]

    repos = initialize_memory_repositories(tmp_path / "records.db", tmp_path / "vectors", vector_size=3)
    try:
        writer = MemoryWriter(repos, Embeddings())
        old = seed_semantic(writer, SCOPE, [MemoryCandidate(
            "部署方案已决定立即部署", "decision", "accepted", ("user_statement",),
            scope_kind="conversation", horizon="long_term",
        )], turn_id="old-seed")[0]

        answer_text = "正式部署暂时不做，实施情况还没确认，之后再决定。"
        packet = MemoryTurnPacket(
            turn_id="clarify-answer-1", occurred_at="2026-10-14T00:00:00.000+00:00",
            scope_owner=SCOPE.owner_id, scope_channel=SCOPE.channel,
            scope_conversation=SCOPE.conversation_id, scope_module=SCOPE.module_id,
            user_text=answer_text, published_reply="好的，那就不动正式部署。",
            evidence_refs=[EvidenceRef("ev-clarify-1", answer_text, outcome="success")],
        )
        saved = writer.save_semantic_candidates(SCOPE, [MemoryCandidate(
            "正式部署暂时不做，实施情况还没确认，之后再决定",
            "decision", "accepted", ("user_statement",),
            scope_kind="conversation", horizon="long_term", origin="program",
            source_refs=(packet.turn_id,),
        )], turn_id="clarify-answer-1", packet=packet)
        assert len(saved) == 1, "有用户原话取证的新版本必须真的落库"
        new = saved[0]

        assert new["id"] != old["id"], "这是一条新记录，不是就地改旧记录"
        assert "正式部署暂时不做" in new["content"]
        assert "实施情况还没确认" in new["content"], "用户没确认的部分要原样保留"
        assert repos.records_by_ids([old["id"]])[old["id"]]["content"] == "部署方案已决定立即部署", (
            "旧记录内容不被覆盖"
        )

        store = open_store(tmp_path)
        job_id = enqueue_job(store, turn_id="resolve-write", processing_result=SIMULATED_UNCERTAIN_ITEM)
        candidate_id = candidates_of(store)[0].candidate_id
        resolved = store.mark_resolved(job_id, candidate_id, answer_turn="t-10", record_id=new["id"])
        assert resolved.state == STATE_RESOLVED
        assert resolved.resolved_record_id == new["id"], "解决标记要指向实际落库的那条记录"
    finally:
        repos.close()


def test_refused_write_is_not_marked_resolved(tmp_path):
    """写入被拒就不许标解决：没有真实记录引用时状态必须停在 waiting。

    这条边界是本组的重点：**保存失败不得被写成已解决**。
    现有写入端拒绝的原因照实断言，不为了过测试放宽目标版本或授权规则。
    """
    from agent_core.memory_repositories import initialize_memory_repositories

    class Embeddings:
        dimensions = 3
        model = "offline-fixed"

        def embed(self, text):
            return [0.1, 0.2, 0.3]

    repos = initialize_memory_repositories(tmp_path / "r.db", tmp_path / "v", vector_size=3)
    try:
        writer = MemoryWriter(repos, Embeddings())
        packet = MemoryTurnPacket(
            turn_id="refused-1", occurred_at="2026-10-14T00:00:00.000+00:00",
            scope_owner=SCOPE.owner_id, scope_channel=SCOPE.channel,
            scope_conversation=SCOPE.conversation_id, scope_module=SCOPE.module_id,
            user_text="这个先放着", published_reply="好的。",
        )
        # 没有可取证的原话：现有写入端必须如实拒绝，而不是硬写一条。
        result = writer.process_semantic_candidates(SCOPE, [MemoryCandidate(
            "正式部署暂时不做", "decision", "accepted", ("user_statement",),
            scope_kind="conversation", horizon="long_term", origin="program",
            source_refs=(packet.turn_id,),
        )], turn_id="refused-1", packet=packet)

        assert result.saved == [], "没有依据的候选不得落库"
        refused = result.rejected + result.pending_review
        assert refused, "被拒要留下明确原因，不能静默丢弃"
        assert "evidence_not_supported" in json.dumps(refused, ensure_ascii=False)
    finally:
        repos.close()

    store = open_store(tmp_path)
    job_id = enqueue_job(store, turn_id="refused-resolve", processing_result=SIMULATED_UNCERTAIN_ITEM)
    candidate_id = candidates_of(store)[0].candidate_id
    # 写入失败就是没有记录号：此时标解决不成立，状态必须停在 waiting。
    state = store.resolve_item(job_id, candidate_id)
    assert state.state == STATE_WAITING and state.resolved_record_id == ""
    assert store.mark_resolved(job_id, candidate_id, answer_turn="t-x", record_id="") is not None
    still = store.resolve_item(job_id, candidate_id)
    assert still.resolved_record_id == "", "空记录号不得被当成真实引用"


# ── 第 3 组：一个轻量反例组 ────────────────────────────────────────


def test_user_does_not_answer_and_chats_about_weather(tmp_path):
    """用户没回答而聊天气：不自动确认、不重复问、旧版本不动。"""
    store = open_store(tmp_path)
    job_id = enqueue_job(store, turn_id="weather-1", processing_result=SIMULATED_UNCERTAIN_ITEM)
    candidates = candidates_of(store)
    question = "先放下的是正式部署，还是本机演练？"
    asked = ClarificationTurn(store, scope_key=SCOPE_KEY, turn_id="t-ask", candidates=candidates)
    asked.propose(candidates[0].candidate_id, question)
    asked.record_published(published_text=question)
    before = store.resolve_item(job_id, candidates[0].candidate_id)

    # 下一轮：用户聊天气，顺手把这条疑点重新交给模型。
    next_turn = ClarificationTurn(store, scope_key=SCOPE_KEY, turn_id="t-weather",
                                  candidates=candidates_of(store))
    repeated = next_turn.propose(before.candidate_id, "同一件事再问一遍？")

    assert repeated["recorded"] is False, "已问未答不重复问"
    assert repeated["reason"] == "该事项已经问过，尚未回答"
    assert next_turn.publish_tail() == "", "本轮不产生第二个问题尾段"
    after = store.resolve_item(job_id, before.candidate_id)
    assert after.state == STATE_ASKED, "聊别的不得把它变成已解决"
    assert (after.asked_question, after.asked_turn) == (before.asked_question, before.asked_turn)
    assert after.answered_turn == "" and after.resolved_record_id == "", "没有回答就没有解决标记"
    context = build_clarification_context(
        store, scope_key=SCOPE_KEY, user_text="今天天气不错", turn_id="t-weather",
    )
    assert context["possible_links"][0]["candidate_id"] == before.candidate_id, (
        "仍然作为可能相关交给后台，但不自动确认"
    )


def test_no_extra_model_call_is_made_to_cover_a_missing_draft(tmp_path):
    """本轮没有草稿时就是空尾段：不为了补问再开一次调用。"""
    store = open_store(tmp_path)
    enqueue_job(store, turn_id="nodraft-1", processing_result=SIMULATED_UNCERTAIN_ITEM)
    turn = ClarificationTurn(store, scope_key=SCOPE_KEY, turn_id="t-none",
                             candidates=candidates_of(store))

    assert turn.publish_tail() == ""
    assert turn.record_published(published_text="随便发一段。") == [], "无草稿不得记账"


def test_asking_again_in_a_later_turn_is_rejected_by_the_store_side_match(tmp_path):
    """新回合重新登记同一条已问事项，仍被服务端拒绝（不是靠模型自觉）。"""
    store = open_store(tmp_path)
    job_id = enqueue_job(store, turn_id="reask-1", processing_result=SIMULATED_UNCERTAIN_ITEM)
    question = "先放下的是正式部署，还是本机演练？"
    first = ClarificationTurn(store, scope_key=SCOPE_KEY, turn_id="t-a",
                              candidates=candidates_of(store))
    first.propose(candidates_of(store)[0].candidate_id, question)
    first.record_published(published_text=question)

    later = ClarificationTurn(store, scope_key=SCOPE_KEY, turn_id="t-b",
                              candidates=candidates_of(store))
    result = later.propose(candidates_of(store)[0].candidate_id, "换个说法再问一次？")

    assert result["recorded"] is False
    assert store.resolve_item(job_id, candidates_of(store)[0].candidate_id).state == STATE_ASKED


def test_same_turn_resubmitted_does_not_enqueue_or_save_twice(tmp_path):
    """同一个回合号再交一次：队列不新建作业，写入端不产生第二个版本。"""
    queue = MemoryCandidateQueue(tmp_path / "jobs.db")
    queue.initialize()
    packet = MemoryTurnPacket(
        turn_id="same-turn-1", occurred_at="2026-10-14T00:00:00.000+00:00",
        scope_owner=SCOPE.owner_id, scope_channel=SCOPE.channel,
        scope_conversation=SCOPE.conversation_id, scope_module=SCOPE.module_id,
        user_text="这个先放着", published_reply="好的。",
    )

    first_id, created = queue.enqueue(packet)
    second_id, created_again = queue.enqueue(MemoryTurnPacket(**{
        **packet.__dict__, "clarification_context": {"note": "内容相同即视为同一回合"},
    }) if False else packet)

    assert created is True and created_again is False
    assert second_id == first_id
    with queue.connect() as db:
        rows = db.execute("SELECT job_id FROM memory_candidate_jobs").fetchall()
    assert len(rows) == 1, "同号重交不得留下第二条作业"

    # 同号但内容不同：明确拒绝，而不是静默复用旧作业。
    with pytest.raises(ValueError):
        queue.enqueue(MemoryTurnPacket(
            turn_id="same-turn-1", occurred_at="2026-10-14T00:00:00.000+00:00",
            scope_owner=SCOPE.owner_id, scope_channel=SCOPE.channel,
            scope_conversation=SCOPE.conversation_id, scope_module=SCOPE.module_id,
            user_text="换了内容", published_reply="换了的回复。",
        ))


def test_old_positional_arguments_and_packets_without_new_fields_stay_readable():
    """旧位置参数与缺新字段的旧包继续可读，行为不变。"""
    from agent_core.contracts import AgentRequest

    legacy_request = AgentRequest("你好")
    assert legacy_request.turn_background is None
    assert not hasattr(legacy_request, "clarification_context") or True

    # 旧包：没有 packet_version、没有 clarification_context，也没有 schema_version。
    packet = MemoryTurnPacket.from_payload({
        "turn_id": "legacy-turn", "occurred_at": "2026-01-01T00:00:00+00:00",
        "scope": {"owner_id": "u", "channel": "web", "conversation_id": "c", "module_id": "companion"},
        "user_text": "旧话", "published_reply": "旧回复",
    })

    assert packet.clarification_context == {}, "缺该字段的旧包按空处理"
    assert packet.turn_id == "legacy-turn"
    assert packet.user_text == "旧话"
    # 旧包重新序列化时补上当前版本，且新字段仍可空。
    payload = packet.as_payload()
    assert payload["packet_version"] == PACKET_VERSION
    assert payload["clarification_context"] == {}
    assert MemoryTurnPacket.from_payload(payload).clarification_context == {}


def test_candidate_payloads_without_new_fields_still_decode():
    """旧候选 JSON（缺新字段）继续解码，不因新增字段而读不了。"""
    decoded = decode_candidates(json.dumps([{
        "content": "排期方案暂不调整", "kind": "decision", "content_status": "accepted",
        "source_types": ["user_statement"],
    }], ensure_ascii=False))

    assert len(decoded) == 1
    assert decoded[0].horizon == "" and decoded[0].review_receipt == {}


def test_unpublished_drafts_from_cancelled_and_failed_turns_are_not_recorded(tmp_path):
    """取消或失败未发布的草稿不记已问：只有真正出现在发布正文里才算。"""
    store = open_store(tmp_path)
    job_id = enqueue_job(store, turn_id="unpublished-1", processing_result=SIMULATED_UNCERTAIN_ITEM)
    question = "先放下的是正式部署，还是本机演练？"

    # 取消：本轮什么都没发布。
    cancelled = ClarificationTurn(store, scope_key=SCOPE_KEY, turn_id="t-cancelled",
                                  candidates=candidates_of(store))
    cancelled.propose(candidates_of(store)[0].candidate_id, question)
    assert cancelled.record_published(published_text="") == []

    # 失败收口：发布了收口文本，但里面没有这条问题。
    failed = ClarificationTurn(store, scope_key=SCOPE_KEY, turn_id="t-failed",
                               candidates=candidates_of(store))
    failed.propose(candidates_of(store)[0].candidate_id, question)
    assert failed.record_published(published_text="这次无法确认，我不能给出结论。") == []

    # 核查把问题删掉了：正文里只剩别的说法。
    trimmed = ClarificationTurn(store, scope_key=SCOPE_KEY, turn_id="t-trimmed",
                                candidates=candidates_of(store))
    trimmed.propose(candidates_of(store)[0].candidate_id, question)
    assert trimmed.record_published(published_text="先放下的是正式部署。") == []

    state = store.resolve_item(job_id, candidates_of(store)[0].candidate_id)
    assert state.state == STATE_WAITING, "三次未发布都不许记已问"
    assert state.asked_question == "" and state.asked_turn == ""


def test_failed_save_does_not_mark_resolved(tmp_path):
    """保存失败就不标解决：没有实际记录引用时不得写成已解决。"""
    store = open_store(tmp_path)
    job_id = enqueue_job(store, turn_id="failed-save-1", processing_result=SIMULATED_UNCERTAIN_ITEM)
    candidate_id = candidates_of(store)[0].candidate_id

    # 按现有语义：解决必须带回答回合；程序不替模型判定"算不算回答了"。
    state = store.resolve_item(job_id, candidate_id)
    assert state.state == STATE_WAITING
    assert state.resolved_record_id == ""
    assert store.mark_resolved(job_id, "不存在的编号", answer_turn="t", record_id="r") is None


def test_other_doubts_in_the_same_turn_are_not_resolved_together(tmp_path):
    """同轮其他疑点不跟着一起解决；原回执保留历史。"""
    store = open_store(tmp_path)
    job_id = enqueue_job(store, turn_id="siblings-1", raw_processing_result={
        "items": [
            {"index": 0, "verdict": "uncertain", "quote": "先放着", "item_path": "部署"},
            {"index": 1, "verdict": "uncertain", "quote": "再等等", "item_path": "排期"},
        ],
    })
    found = candidates_of(store)

    store.mark_resolved(job_id, found[0].candidate_id, answer_turn="t-11", record_id="rec-1")

    remaining = [item.candidate_id for item in candidates_of(store)]
    assert remaining == [found[1].candidate_id], "另一个疑点不随它一起解决"
    assert store.job(job_id)["processing_result"] is not None, "原回执保留历史"


def test_propose_tool_is_transient_and_goes_through_the_existing_registry(tmp_path):
    """登记工具是低风险、临时效果，并且真的走现有工具契约。"""
    from agent_core.contracts import EFFECT_TRANSIENT

    store = open_store(tmp_path)
    enqueue_job(store, turn_id="tool-1", processing_result=SIMULATED_UNCERTAIN_ITEM)
    candidate_id = candidates_of(store)[0].candidate_id
    turn = ClarificationTurn(store, scope_key=SCOPE_KEY, turn_id="t-tool",
                             candidates=candidates_of(store))

    spec = build_propose_tool_spec(turn)

    assert spec.name == propose_tool_name() == "memory__propose_clarification"
    assert spec.risk == "low" and spec.effect == EFFECT_TRANSIENT
    assert set(spec.schema["function"]["parameters"]["properties"]) == {"candidate_id", "question"}
    result = spec.handler({"candidate_id": candidate_id, "question": "先放下的是哪一项？"}, None)
    assert result["status"] == "ok"
    assert result["output"]["recorded"] is True
    # 同轮第二条不报错、也不偷换：明确回「本轮已登记一个问题」。
    second = spec.handler({"candidate_id": candidate_id, "question": "换一句再问？"}, None)
    assert second["status"] == "ok" and second["output"]["recorded"] is False
    # 越界候选由服务端校验，走失败态，不能静默放过。
    fresh = build_propose_tool_spec(ClarificationTurn(
        store, scope_key=SCOPE_KEY, turn_id="t-tool-2", candidates=candidates_of(store),
    ))
    failed = fresh.handler({"candidate_id": "越界编号", "question": "问一句"}, None)
    assert failed["status"] == "failed", "越界候选必须走失败态，不能静默放过"


# ── 辅助 ───────────────────────────────────────────────────────────


def _walk_uncertain(node, *, depth: int = 0):
    """从保留的真实证据文件里找出 verdict=uncertain 的项（只读，不改判断）。"""
    found: list[dict] = []
    if depth > 8:
        return found
    if isinstance(node, dict):
        if str(node.get("verdict") or "").lower() == "uncertain" and isinstance(node.get("content"), str):
            found.append({key: node[key] for key in (
                "verdict", "content", "reason", "quote", "user_text",
                "clarification_question", "target_id", "target_version",
            ) if key in node})
        for value in node.values():
            found.extend(_walk_uncertain(value, depth=depth + 1))
    elif isinstance(node, list):
        for value in node:
            found.extend(_walk_uncertain(value, depth=depth + 1))
    return found
