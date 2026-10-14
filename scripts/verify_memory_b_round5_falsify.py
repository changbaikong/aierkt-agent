"""Round-5 independent falsification of the Lead's two frozen-version changes.

Targets:
  1) agent_core/memory_extract.py  -- DECISION_MARKERS wired into the binding path
     (`_expresses_explicit_decision`, `_decision_wording_evidenced`,
     `_adoption_matches_subject`, `_strip_decision_markers`).
  2) agent_core/memory_maintenance.py -- `_value_evidence_gap` narrowed to
     value-bearing fragments only for action == "replace" (`_is_value_bearing`).

Everything runs on real parse / save / maintenance entry points against
fully temporary, isolated databases and vector stores.

Framing restated: the model outputs below are hand-built synthetic adversarial
outputs. They prove which invariants the *program* enforces; they say nothing
about how often a live model produces such output.

Exit 0 only if every case actually passed.
"""
from __future__ import annotations

import gc
import json
import logging
import os
import sys
import tempfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.memory_test_support import seed_semantic
from agent_core.contracts import Scope
from agent_core.memory_extract import SemanticMemoryExtractor
from agent_core.memory_maintenance import MemoryMaintenanceService
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_turn import (
    HistoryItem,
    MemoryCandidate,
    MemoryRef,
    turn_from_messages,
)
from agent_core.memory_writes import MemoryWriter

CHECKS: list[dict] = []


class Embeddings:
    def embed(self, text):
        return [0.1, 0.2, 0.3]


def record(identifier, requirement, passed, actual):
    CHECKS.append(
        dict(id=identifier, requirement=requirement, passed=bool(passed), actual=actual)
    )


def _module_mtimes() -> dict:
    from datetime import UTC as _UTC

    stamps = {}
    for name in (
        "memory_extract.py",
        "memory_maintenance.py",
        "memory_turn.py",
        "memory_writes.py",
    ):
        p = ROOT / "agent_core" / name
        try:
            stamps[name] = datetime.fromtimestamp(p.stat().st_mtime, tz=_UTC).isoformat(
                timespec="milliseconds"
            )
        except OSError:
            stamps[name] = "missing"
    return stamps


def run() -> int:
    scope = Scope("r5-user", "web", "r5", "companion")
    os.environ["QDRANT_MODE"] = "local"
    os.environ["MEMORY_SEMANTIC_ENABLED"] = "true"

    with tempfile.TemporaryDirectory(
        prefix="memory-r5-review-", ignore_cleanup_errors=True
    ) as folder:
        base = Path(folder)
        os.environ["QDRANT_PATH"] = str(base / "vectors")
        repos = initialize_memory_repositories(
            base / "records.db", base / "vectors", vector_size=3
        )
        writer = MemoryWriter(repos, Embeddings())
        service = MemoryMaintenanceService(writer, repos)
        extractor = SemanticMemoryExtractor(None)

        def packet(turn, text, target=None, history=(), refs=()):
            p = turn_from_messages(
                turn_id=turn,
                occurred_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
                scope=scope,
                user_text=text,
                published_reply="收到",
            )
            mem = list(refs)
            if target is not None:
                mem.append(
                    MemoryRef(
                        target["id"],
                        target["kind"],
                        target["content_status"],
                        target["content"],
                        version=target["record_version"],
                    )
                )
            return replace(
                p, recalled_memory_refs=mem, relevant_history=list(history)
            )

        def parse(p, item, target=None):
            kwargs = (
                {}
                if target is None
                else dict(
                    known_ids=[target["id"]],
                    known_versions={target["id"]: target["record_version"]},
                )
            )
            return extractor.parse_maintenance(
                json.dumps({"memories": [item]}, ensure_ascii=False), packet=p, **kwargs
            )

        def parse_candidate(p, item, known_ids=None):
            """`parse` returns only (candidates, rejected); normalise to 3-tuple."""
            parsed = extractor.parse(
                json.dumps({"memories": [item]}, ensure_ascii=False),
                packet=p,
                known_ids=known_ids,
            )
            if len(parsed) == 2:
                candidates, rejected = parsed
                return candidates, [], rejected
            return parsed

        def seed(turn, text, **extra):
            data = dict(
                content=text,
                kind="fact",
                content_status="reported",
                source_types=("user_statement",),
                source_refs=(turn,),
            )
            data.update(extra)
            return seed_semantic(writer,
                scope, [MemoryCandidate(**data)], turn_id=turn
            )[0]

        def status_of(record_id):
            row = repos.records_by_ids([record_id]).get(record_id)
            return (row or {}).get("status")

        def apply_suggestions(p, suggestions):
            out = []
            for s in suggestions:
                try:
                    out.append(
                        service.apply(s, scope=scope, packet=p, turn_id=p.turn_id)
                    )
                except Exception as exc:  # noqa: BLE001 - 崩溃本身就是验收结果
                    out.append(
                        {
                            "action": s.action,
                            "applied": False,
                            "reason": f"CRASH {type(exc).__name__}: {exc}",
                            "record": None,
                            "archived": [],
                            "verify": {},
                            "crashed": True,
                        }
                    )
            return out

        def update(p, target, text, **extra):
            item = dict(
                action="correct",
                target_id=target["id"],
                expected_version=target["record_version"],
                content=text,
                kind="fact",
                content_status="reported",
                source_types=["user_statement"],
                source_refs=[p.turn_id],
                reason="模型建议更新",
            )
            item.update(extra)
            _, suggestions, rejected = parse(p, item, target)
            return apply_suggestions(p, suggestions), rejected

        try:
            # ==============================================================
            # W1: conditional decision wording must NOT stamp an adoption.
            # "如果不累的话就一律先落盘" is a hypothetical, not a decision.
            # ==============================================================
            p = packet("w1-current", "如果不累的话就一律先落盘；部署的事回头说")
            candidates, _, rejected = parse_candidate(
                p,
                dict(
                    content="用户已决定日志一律先落盘再回传",
                    kind="decision",
                    content_status="accepted",
                    source_types=["user_statement"],
                    source_refs=[p.turn_id],
                ),
            )
            saved = seed_semantic(writer, scope, candidates, turn_id=p.turn_id)
            record(
                "W1-conditional-decision-not-accepted",
                "有条件/假设句里的决定措辞不能盖章成已决定",
                not any(r["content_status"] == "accepted" for r in saved),
                dict(
                    saved=[
                        dict(content=r["content"], status=r["content_status"])
                        for r in saved
                    ],
                    rejected=rejected,
                ),
            )

            # ==============================================================
            # W2: an interrogative decision wording must NOT stamp adoption.
            # ==============================================================
            p = packet("w2-current", "暂不部署吗？还是再等等")
            candidates, _, rejected = parse_candidate(
                p,
                dict(
                    content="用户已决定暂不部署",
                    kind="decision",
                    content_status="accepted",
                    source_types=["user_statement"],
                    source_refs=[p.turn_id],
                ),
            )
            saved = seed_semantic(writer, scope, candidates, turn_id=p.turn_id)
            record(
                "W2-interrogative-decision-not-accepted",
                "疑问句里的决定措辞不能盖章成已决定",
                not any(r["content_status"] == "accepted" for r in saved),
                dict(
                    saved=[
                        dict(content=r["content"], status=r["content_status"])
                        for r in saved
                    ],
                    rejected=rejected,
                ),
            )

            # ==============================================================
            # W3: unrelated subject's decision wording must not license a
            # different subject. "排期暂不调整" must not prove "部署暂不实施".
            # The Lead added `_strip_decision_markers` for exactly this.
            # ==============================================================
            p = packet("w3-current", "排期暂不调整；部署那边你安排")
            candidates, _, rejected = parse_candidate(
                p,
                dict(
                    content="用户已决定部署暂不实施",
                    kind="decision",
                    content_status="accepted",
                    source_types=["user_statement"],
                    source_refs=[p.turn_id],
                ),
            )
            saved = seed_semantic(writer, scope, candidates, turn_id=p.turn_id)
            record(
                "W3-unrelated-decision-wording-not-accepted",
                "无关事项的决定措辞不能给另一件事盖章",
                not any(r["content_status"] == "accepted" for r in saved),
                dict(
                    saved=[
                        dict(content=r["content"], status=r["content_status"])
                        for r in saved
                    ],
                    rejected=rejected,
                ),
            )

            # ==============================================================
            # W4: vague acknowledgements stay proposed.
            # ==============================================================
            p = packet("w4-current", "嗯，好的，可以")
            candidates, _, rejected = parse_candidate(
                p,
                dict(
                    content="用户已采纳容灾方案",
                    kind="decision",
                    content_status="accepted",
                    source_types=["user_statement"],
                    source_refs=[p.turn_id],
                ),
            )
            saved = seed_semantic(writer, scope, candidates, turn_id=p.turn_id)
            record(
                "W4-vague-acknowledgement-stays-proposed",
                "含糊表态（嗯/好的/可以）不能成为已采纳",
                not any(r["content_status"] == "accepted" for r in saved),
                dict(
                    saved=[
                        dict(content=r["content"], status=r["content_status"])
                        for r in saved
                    ],
                    rejected=rejected,
                ),
            )

            # ==============================================================
            # W5: positive control -- a genuine same-subject decision wording
            # must STILL be accepted, so the guards did not block everything.
            # ==============================================================
            p = packet("w5-current", "项目日志一律先落盘再回传，就这么办")
            candidates, _, rejected = parse_candidate(
                p,
                dict(
                    content="用户已决定项目日志一律先落盘再回传",
                    kind="decision",
                    content_status="accepted",
                    source_types=["user_statement"],
                    source_refs=[p.turn_id],
                ),
            )
            saved = seed_semantic(writer, scope, candidates, turn_id=p.turn_id)
            record(
                "W5-genuine-decision-still-accepted",
                "同一事项上的真实决定措辞仍可保存为已决定",
                any(r["content_status"] == "accepted" for r in saved),
                dict(
                    statuses=[r["content_status"] for r in saved], rejected=rejected
                ),
            )

            # ==============================================================
            # W6: correct action must still refuse an unsupported NON-numeric
            # qualitative addition invented by the model.
            # ==============================================================
            old = seed("w6-seed", "部署策略为先灰度后全量")
            p = packet("w6-current", "部署策略不是先灰度后全量，改成先全量后灰度", old)
            outcomes, rejected = update(
                p, old, "部署策略为先全量后灰度，并且在夜间低峰期执行"
            )
            leaked = (
                str((outcomes[0].get("record") or {}).get("content")) if outcomes else ""
            )
            record(
                "W6-correct-refuses-unsupported-qualitative",
                "correct 动作塞入原话没有的非数字定性内容必须被拒",
                not (
                    outcomes
                    and outcomes[0]["applied"]
                    and "夜间低峰期" in leaked
                ),
                dict(
                    applied=[
                        dict(
                            applied=r["applied"],
                            content=(r.get("record") or {}).get("content"),
                            reason=r["reason"],
                        )
                        for r in outcomes
                    ],
                    rejected=rejected,
                ),
            )

            # ==============================================================
            # W7: replace action must STILL refuse an unsupported NUMBER.
            # This is the known cost boundary of the narrowing -- verify it did
            # not go too far.
            # ==============================================================
            old = seed("w7-seed", "部署方案为方案A")
            p = packet(
                "w7-current",
                "部署方案改用方案B，不再用方案A",
                old,
                refs=[],
            )
            outcomes, rejected = update(
                p,
                old,
                "部署方案改用方案B，保留期限为九十天",
                action="replace",
            )
            leaked = (
                str((outcomes[0].get("record") or {}).get("content")) if outcomes else ""
            )
            record(
                "W7-replace-refuses-unsupported-number",
                "replace 动作塞入原话没有的数字必须仍被拒",
                not (outcomes and outcomes[0]["applied"] and "九十天" in leaked),
                dict(
                    applied=[
                        dict(
                            applied=r["applied"],
                            content=(r.get("record") or {}).get("content"),
                            reason=r["reason"],
                        )
                        for r in outcomes
                    ],
                    rejected=rejected,
                ),
            )

            # ==============================================================
            # W8: replace action's KNOWN COST -- an invented qualitative
            # conclusion (no number) may now slip through. Report honestly.
            # ==============================================================
            old = seed("w8-seed", "容灾方案为方案A")
            p = packet("w8-current", "容灾方案改用方案B，不再用方案A", old)
            outcomes, rejected = update(
                p,
                old,
                "容灾方案改用方案B，实施窗口已获批准",
                action="replace",
            )
            leaked = (
                str((outcomes[0].get("record") or {}).get("content")) if outcomes else ""
            )
            record(
                "W8-replace-qualitative-known-cost",
                "replace 定性新增能否漏过（已知代价，如实记录）",
                not (outcomes and outcomes[0]["applied"] and "已获批准" in leaked),
                dict(
                    applied=[
                        dict(
                            applied=r["applied"],
                            content=(r.get("record") or {}).get("content"),
                            reason=r["reason"],
                        )
                        for r in outcomes
                    ],
                    rejected=rejected,
                    note="若此条 FAIL，说明 replace 的定性新增确实能落库（Lead 已知代价）",
                ),
            )

            # ==============================================================
            # W9: normal words containing Chinese numerals must not be
            # misjudged as value-bearing, which would TIGHTEN normal release.
            # A legitimate replace whose only "changed tokens" are such words
            # must still pass.
            # ==============================================================
            old = seed("w9-seed", "值班安排为统一排班")
            p = packet("w9-current", "值班安排改用轮换排班，不再统一排班", old)
            outcomes, rejected = update(
                p, old, "值班安排改用轮换排班", action="replace"
            )
            record(
                "W9-numeral-words-do-not-tighten",
                "含中文数词的普通词（统一/一样/一律）不得被误判为带值而收紧正常放行",
                bool(outcomes) and outcomes[0]["applied"],
                dict(
                    applied=[
                        dict(
                            applied=r["applied"],
                            reason=r["reason"],
                        )
                        for r in outcomes
                    ],
                    rejected=rejected,
                ),
            )

            # ==============================================================
            # W10: correct action negative control for replace-style rewording
            # -- correct must not silently benefit from the replace relaxation.
            # "使用"->"决定" rewording under correct with an invented word.
            # ==============================================================
            old = seed("w10-seed", "回滚方案选择使用方案A")
            p = packet("w10-current", "回滚方案不是选择使用方案A，是改成方案B", old)
            outcomes, rejected = update(
                p, old, "回滚方案决定使用方案B，并已通过评审"
            )
            leaked = (
                str((outcomes[0].get("record") or {}).get("content")) if outcomes else ""
            )
            record(
                "W10-correct-not-relaxed-by-replace-rule",
                "correct 不得享受 replace 的放宽而塞入定性内容",
                not (outcomes and outcomes[0]["applied"] and "已通过评审" in leaked),
                dict(
                    applied=[
                        dict(
                            applied=r["applied"],
                            content=(r.get("record") or {}).get("content"),
                            reason=r["reason"],
                        )
                        for r in outcomes
                    ],
                    rejected=rejected,
                ),
            )

            # ==============================================================
            # W11: a decision wording guarded by a withhold phrase must not
            # stamp acceptance ("先不要决定是否采用").
            # ==============================================================
            p = packet("w11-current", "先不要决定是否采用新部署方案")
            candidates, _, rejected = parse_candidate(
                p,
                dict(
                    content="用户已决定采用新部署方案",
                    kind="decision",
                    content_status="accepted",
                    source_types=["user_statement"],
                    source_refs=[p.turn_id],
                ),
            )
            saved = seed_semantic(writer, scope, candidates, turn_id=p.turn_id)
            record(
                "W11-withhold-wording-not-accepted",
                "「先不要决定是否采用」不能盖章成已决定",
                not any(r["content_status"] == "accepted" for r in saved),
                dict(
                    saved=[
                        dict(content=r["content"], status=r["content_status"])
                        for r in saved
                    ],
                    rejected=rejected,
                ),
            )

            # ==============================================================
            # W12: history-based unrelated decision wording must not bind
            # across subjects either.
            # ==============================================================
            p = packet(
                "w12-current",
                "预算的事以后再说",
                history=[HistoryItem("w12-hist", "user", "排期暂不调整")],
            )
            candidates, _, rejected = parse_candidate(
                p,
                dict(
                    content="用户已决定预算暂不调整",
                    kind="decision",
                    content_status="accepted",
                    source_types=["user_statement"],
                    source_refs=["w12-hist"],
                ),
            )
            saved = seed_semantic(writer, scope, candidates, turn_id=p.turn_id)
            record(
                "W12-history-unrelated-decision-not-bound",
                "历史里无关事项的决定措辞不能给本条盖章",
                not any(r["content_status"] == "accepted" for r in saved),
                dict(
                    saved=[
                        dict(content=r["content"], status=r["content_status"])
                        for r in saved
                    ],
                    rejected=rejected,
                ),
            )
        finally:
            repos.close()
            gc.collect()

    report = dict(
        ok=all(c["passed"] for c in CHECKS) and len(CHECKS) > 0,
        passed=sum(c["passed"] for c in CHECKS),
        total=len(CHECKS),
        checks=CHECKS,
        live_model_calls=0,
        production_memory_touched=False,
        tested_module_mtimes=_module_mtimes(),
        limits=[
            "Synthetic hand-built adversarial model outputs verify program guarantees, not live-model error frequency.",
            "Local temporary databases and vector stores only; no deployment or production memory touched.",
            "Aggregate ok requires EVERY case to pass; a failing subset is never reported as success.",
            "W8 deliberately probes a known cost of the replace relaxation; a FAIL there is a finding, not noise.",
        ],
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    raise SystemExit(run())
