"""分片C 自测：替代旁路的证据保护 + 短期语义的持久化。

两个目标各测两向：
1. 替代（B3-R5）：助手建议/模型推断/无关原话一律不能归档旧记录；用户明确
   「改用 B、不再用 A」仍要能归档；
2. 短期（B3-R7 持久化侧）：horizon=short_term 落库要留语义、要写 expires_at、
   要落进到期政策管得到的那一支；长期结论不能被这条规则误伤。

返回值契约（与 lead 对齐）：
- 候选入库与否**只由候选自己决定**，不受替代旁路影响；
- 替代旁路没被授权时，候选照存、旧记录原封不动，拒绝原因进 ``packet.rejected``。
"""
from __future__ import annotations

from scripts.memory_test_support import seed_semantic
import gc
import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_core.contracts import Scope
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_turn import MemoryCandidate, turn_from_messages
from agent_core.memory_writes import MemoryWriter
import asyncio
from agent_core.memory_processing import MemoryReviewProcessor
from support.memory_review import FixedReviewProvider, supported_row

SCOPE = Scope("guard-user", "web", "guard-conversation", "companion")


class _Embeddings:
    def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


@pytest.fixture()
def env():
    os.environ["QDRANT_MODE"] = "local"
    with tempfile.TemporaryDirectory(prefix="memory-supersede-guard-") as folder:
        base = Path(folder)
        repos = initialize_memory_repositories(
            base / "records.db", base / "vectors", vector_size=3
        )
        writer = MemoryWriter(repos, _Embeddings())
        try:
            yield writer, repos
        finally:
            repos.close()
            gc.collect()


def _packet(turn: str, user_text: str, *, target: dict | None = None, evidence=()):
    refs = []
    if target is not None:
        refs = [
            {
                "memory_id": target["id"],
                "kind": target["kind"],
                "content_status": target["content_status"],
                "excerpt": target["content"],
                "version": target["record_version"],
            }
        ]
    return turn_from_messages(
        turn_id=turn,
        occurred_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
        scope=SCOPE,
        user_text=user_text,
        published_reply="收到",
        recalled_memory_refs=refs,
        evidence_refs=list(evidence),
    )


def _seed(writer, turn: str, text: str) -> dict:
    return seed_semantic(writer,
        SCOPE,
        [
            MemoryCandidate(
                content=text,
                kind="fact",
                content_status="reported",
                source_types=("user_statement",),
                source_refs=(turn,),
            )
        ],
        turn_id=turn,
    )[0]


def _candidate(text: str, **over) -> MemoryCandidate:
    payload = dict(
        content=text,
        kind="decision",
        content_status="accepted",
        source_types=("user_statement",),
        origin="program",
    )
    payload.update(over)
    return MemoryCandidate(**payload)


def _status(repos, record_id: str) -> str:
    return repos.records_by_ids([record_id])[record_id]["status"]


# ----------------------------------------------------------------------
# B3-R5 替代旁路
# ----------------------------------------------------------------------
def test_assistant_suggestion_cannot_supersede(env):
    """助手建议 + proposed + 替代清单：旧原则必须还是 active，且候选不入库（B3-R5 本尊）。"""
    writer, repos = env
    old = _seed(writer, "asst-seed", "数据项目原则是本地优先")
    packet = _packet("asst-bypass", "今天天气不错", target=old)
    forged = _candidate(
        "助手建议数据项目改成云端优先",
        kind="plan",
        content_status="proposed",
        source_types=("assistant_suggestion",),
        origin="model_candidate",
        supersedes=(old["id"],),
        source_refs=(packet.turn_id,),
    )
    saved = writer.save_semantic_candidates(
        SCOPE, [forged], turn_id=packet.turn_id, known_ids=[old["id"]], packet=packet
    )
    assert saved == []
    assert _status(repos, old["id"]) == "active"


def test_user_statement_without_replacement_wording_cannot_supersede(env):
    """用户原话里没有「改用／不再用」：只是聊到这件事，不足以淘汰。"""
    writer, repos = env
    old = _seed(writer, "talk-seed", "数据项目原则是本地优先")
    packet = _packet("talk-only", "数据项目原则我想再想想", target=old)
    candidate = _candidate(
        "数据项目改成云端优先",
        supersedes=(old["id"],),
        source_refs=(packet.turn_id,),
    )
    saved = writer.save_semantic_candidates(
        SCOPE, [candidate], turn_id=packet.turn_id, known_ids=[old["id"]], packet=packet
    )
    assert saved == []
    assert _status(repos, old["id"]) == "active"


def test_unrelated_wording_cannot_supersede(env):
    """说别的句子里出现「不用」不能给这条记忆盖章。"""
    writer, repos = env
    old = _seed(writer, "unrelated-seed", "服务器预算为5000元")
    packet = _packet("unrelated", "打印机不用了，服务器预算还是五千", target=old)
    candidate = _candidate(
        "服务器预算为3000元",
        supersedes=(old["id"],),
        source_refs=(packet.turn_id,),
    )
    saved = writer.save_semantic_candidates(
        SCOPE, [candidate], turn_id=packet.turn_id, known_ids=[old["id"]], packet=packet
    )
    assert saved == []
    assert _status(repos, old["id"]) == "active"


def test_model_inference_cannot_supersede(env):
    """模型推断的来源类别同样不能淘汰现行结论。"""
    writer, repos = env
    old = _seed(writer, "infer-seed", "交付日期是12月1日")
    packet = _packet("infer-change", "交付日期改用1月1日", target=old)
    candidate = _candidate(
        "交付日期是1月1日",
        kind="hypothesis",
        content_status="untested",
        source_types=("model_inference",),
        origin="model_candidate",
        supersedes=(old["id"],),
        source_refs=(packet.turn_id,),
    )
    saved = writer.save_semantic_candidates(
        SCOPE, [candidate], turn_id=packet.turn_id, known_ids=[old["id"]], packet=packet
    )
    assert saved == []
    assert _status(repos, old["id"]) == "active"


def test_no_packet_means_no_supersede(env):
    """候选带替代清单、内容状态仍是 proposed：没有处理包也一律不许归档。

    内容状态是 proposed 时，判据不依赖处理包 —— 拿一条「建议」去推翻已经生效的
    结论，任何时候都不成立。
    """
    writer, repos = env
    old = _seed(writer, "nopacket-seed", "数据项目原则是本地优先")
    candidate = _candidate(
        "数据项目原则是云端优先",
        kind="plan",
        content_status="proposed",
        supersedes=(old["id"],),
    )
    saved = seed_semantic(writer, SCOPE, [candidate], turn_id="nopacket")
    assert saved == []
    assert _status(repos, old["id"]) == "active"


def test_explicit_replacement_still_supersedes(env):
    """反向：用户明确「改用方案B、不再用方案A」必须照旧归档 A。"""
    writer, repos = env
    old = _seed(writer, "explicit-seed", "部署方案A是本地部署")
    packet = _packet("explicit-change", "部署改用方案B，不再用方案A", target=old)
    candidate = _candidate(
        "部署改用方案B",
        supersedes=(old["id"],),
        source_refs=(packet.turn_id,),
    )
    provider = FixedReviewProvider([supported_row('部署改用方案B',
        [('explicit-change', '部署改用方案B'), ('explicit-change', '不再用方案A')], subject='部署方案')])
    prepared, _, result = asyncio.run(MemoryReviewProcessor(writer, provider).prepare(
        packet, [candidate], persist=lambda _: None))
    assert prepared and not result.pending_review
    candidate = prepared[0]
    saved = writer.save_semantic_candidates(
        SCOPE, [candidate], turn_id=packet.turn_id, known_ids=[old["id"]], packet=packet
    )
    assert len(saved) == 1
    assert saved[0]["status"] == "active"
    archived = repos.records_by_ids([old["id"]])[old["id"]]
    assert archived["status"] == "archived"
    assert archived["content_status"] == "superseded"
    assert archived["effective_record_id"] == saved[0]["id"]


def test_superseding_candidate_may_still_be_only_proposed(env):
    """反向：用户明确替代、替代者状态是 proposed 时也要放行 —— 证据是原话，不是状态。"""
    writer, repos = env
    old = _seed(writer, "proposed-seed", "数据项目用本地存储")
    packet = _packet("proposed-change", "数据项目改用云端存储，不再用本地存储", target=old)
    candidate = _candidate(
        "数据项目用云端存储",
        kind="hypothesis",
        content_status="untested",
        supersedes=(old["id"],),
        source_refs=(packet.turn_id,),
    )
    provider = FixedReviewProvider([supported_row('数据项目用云端存储',
        [('proposed-change', '数据项目改用云端存储'), ('proposed-change', '不再用本地存储')], subject='数据项目存储')])
    prepared, _, result = asyncio.run(MemoryReviewProcessor(writer, provider).prepare(
        packet, [candidate], persist=lambda _: None))
    assert prepared and not result.pending_review
    candidate = prepared[0]
    saved = writer.save_semantic_candidates(
        SCOPE, [candidate], turn_id=packet.turn_id, known_ids=[old["id"]], packet=packet
    )
    assert len(saved) == 1
    assert _status(repos, old["id"]) == "archived"


def test_direct_call_without_packet_cannot_archive(env):
    """缺少原始处理包和复核回执时，程序直连也不能淘汰旧记录。"""
    writer, repos = env
    old = _seed(writer, "direct-seed", "默认只在本地提炼记忆")
    packet = _packet("direct-turn", "改成记忆只在本地提炼", target=old)
    candidate = _candidate(
        "改成记忆只在本地提炼",
        supersedes=(old["id"],),
        source_refs=(packet.turn_id,),
    )
    saved = writer.save_semantic_candidates(
        SCOPE, [candidate], turn_id=packet.turn_id, known_ids=[old["id"]]
    )
    assert saved == []
    assert _status(repos, old["id"]) == "active"


# ----------------------------------------------------------------------
# B3-R7 短期语义的持久化
# ----------------------------------------------------------------------
def test_short_term_candidate_keeps_horizon_and_expiry(env):
    """短期候选：metadata 留 horizon、库里写 expires_at、层状态能被到期政策处理。"""
    writer, repos = env
    packet = _packet("short-emotion", "刚拿到快递所以心情轻松了一点")
    candidate = MemoryCandidate(
        content="刚拿到快递所以心情轻松了一点",
        kind="fact",
        content_status="reported",
        source_types=("user_statement",),
        source_refs=(packet.turn_id,),
        horizon="short_term",
    )
    saved = writer.save_semantic_candidates(
        SCOPE, [candidate], turn_id=packet.turn_id, packet=packet
    )
    assert len(saved) == 1
    record = saved[0]
    assert json.loads(record["metadata"])["horizon"] == "short_term"
    assert record["expires_at"]
    assert record["layer"] == "warm"
    assert record["status"] == "candidate"


def test_short_term_row_is_expired_by_maintenance(env):
    """一年后跑维护：这条短期记录不能再是 active。"""
    writer, repos = env
    packet = _packet("short-expire", "刚拿到快递所以心情轻松了一点")
    candidate = MemoryCandidate(
        content="刚拿到快递所以心情轻松了一点",
        kind="fact",
        content_status="reported",
        source_types=("user_statement",),
        source_refs=(packet.turn_id,),
        horizon="short_term",
    )
    saved = writer.save_semantic_candidates(
        SCOPE, [candidate], turn_id=packet.turn_id, packet=packet
    )
    record_id = saved[0]["id"]
    report = writer.run_maintenance(now=datetime.now(UTC) + timedelta(days=365))
    assert report["expired"] >= 1
    assert repos.records_by_ids([record_id])[record_id]["status"] == "expired"


def test_long_term_preference_containing_today_is_untouched(env):
    """长期偏好正文里出现「今天」不得被判成短期：寿命只认声明，不认用词。"""
    writer, repos = env
    packet = _packet("long-preference", "长期原则：今天的事情今天做完")
    candidate = MemoryCandidate(
        content="长期原则：今天的事情今天做完",
        kind="preference",
        content_status="explicit",
        source_types=("user_statement",),
        source_refs=(packet.turn_id,),
        horizon="long_term",
    )
    saved = writer.save_semantic_candidates(
        SCOPE, [candidate], turn_id=packet.turn_id, packet=packet
    )
    assert len(saved) == 1
    assert saved[0]["expires_at"] in (None, "")
    assert saved[0]["status"] == "active"
    assert json.loads(saved[0]["metadata"])["horizon"] == "long_term"
    fresh = repos.records_by_ids([saved[0]["id"]])[saved[0]["id"]]
    assert fresh["status"] == "active"


def test_short_term_duplicate_is_idempotent(env):
    """同一回合重放同一内容：命中幂等键，只留一条，不落成第二行。"""
    writer, repos = env
    packet = _packet("short-replay", "刚拿到快递所以心情轻松了一点")
    candidate = MemoryCandidate(
        content="刚拿到快递所以心情轻松了一点",
        kind="fact",
        content_status="reported",
        source_types=("user_statement",),
        source_refs=(packet.turn_id,),
        horizon="short_term",
    )
    first = writer.save_semantic_candidates(
        SCOPE, [candidate], turn_id=packet.turn_id, packet=packet
    )
    second = writer.save_semantic_candidates(
        SCOPE, [candidate], turn_id=packet.turn_id, packet=packet
    )
    assert first[0]["id"] == second[0]["id"]
    assert repos.records_by_ids([first[0]["id"]])[first[0]["id"]]["status"] == "candidate"


def test_short_term_keeps_only_latest_in_slot(env):
    """同一件事一天里说了两次：留最新那条短期状态，旧的那条归档。

    两条正文不同 ⇒ stable_key 不同，去重不会命中，落到的就是「同互斥维度只留
    最新一条」这条短期规则上。
    """
    writer, repos = env
    first_packet = _packet("mood-one", "今天有点累")
    older = writer.save_semantic_candidates(
        SCOPE,
        [
            MemoryCandidate(
                content="今天有点累",
                kind="fact",
                content_status="reported",
                source_types=("user_statement",),
                source_refs=(first_packet.turn_id,),
                topics=("user.state.mood",),
                horizon="short_term",
            )
        ],
        turn_id=first_packet.turn_id,
        packet=first_packet,
    )[0]
    second_packet = _packet("mood-two", "今天还好")
    newer = writer.save_semantic_candidates(
        SCOPE,
        [
            MemoryCandidate(
                content="今天还好",
                kind="fact",
                content_status="reported",
                source_types=("user_statement",),
                source_refs=(second_packet.turn_id,),
                topics=("user.state.mood",),
                horizon="short_term",
            )
        ],
        turn_id=second_packet.turn_id,
        packet=second_packet,
    )[0]
    assert older["id"] != newer["id"]
    assert repos.records_by_ids([older["id"]])[older["id"]]["status"] == "archived"
    assert repos.records_by_ids([newer["id"]])[newer["id"]]["status"] == "candidate"
