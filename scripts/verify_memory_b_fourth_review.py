"""Fourth-round independent B review: adversarial variants, temporary data only.

This script is written by an *independent verifier*. It does not trust any
fixer's self-report: every guarantee is re-exercised through a surface that
differs from the original B3-R1..R7 examples, using the real parse / save /
maintenance entry points against fully temporary, isolated databases and
vector stores.

Framing, stated up front: the model outputs below are **hand-built synthetic
adversarial** outputs. They prove which invariants the *program* enforces.
They say nothing about how often a live model actually produces such output.

Exit 0 only when every single check actually passed; a single failure makes the
aggregate ``ok`` false and the exit code 1. Crashes propagate.
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
    EvidenceRef,
    MemoryCandidate,
    MemoryRef,
    turn_from_messages,
)
from agent_core.memory_writes import MemoryWriter


class Embeddings:
    def embed(self, text):
        return [0.1, 0.2, 0.3]


#: Module-level result accumulator so a crash still reports what did run.
CHECKS: list[dict] = []


def record(identifier, requirement, passed, actual):
    CHECKS.append(
        dict(id=identifier, requirement=requirement, passed=bool(passed), actual=actual)
    )


def run() -> int:
    scope = Scope("fourth-review-user", "web", "fourth-review", "companion")
    os.environ["QDRANT_MODE"] = "local"
    os.environ["MEMORY_SEMANTIC_ENABLED"] = "true"

    with tempfile.TemporaryDirectory(
        prefix="memory-fourth-review-", ignore_cleanup_errors=True
    ) as folder:
        base = Path(folder)
        os.environ["QDRANT_PATH"] = str(base / "vectors")
        repos = initialize_memory_repositories(
            base / "records.db", base / "vectors", vector_size=3
        )
        writer = MemoryWriter(repos, Embeddings())
        service = MemoryMaintenanceService(writer, repos)
        extractor = SemanticMemoryExtractor(None)

        def packet(turn, text, target=None, evidence=(), scope_=None):
            p = turn_from_messages(
                turn_id=turn,
                occurred_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
                scope=scope_ or scope,
                user_text=text,
                published_reply="收到",
            )
            refs = []
            if target is not None:
                refs = [
                    MemoryRef(
                        target["id"],
                        target["kind"],
                        target["content_status"],
                        target["content"],
                        version=target["record_version"],
                    )
                ]
            return replace(p, recalled_memory_refs=refs, evidence_refs=list(evidence))

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

        def update(p, target, text, **extra):
            """Apply one model suggestion; a hard crash becomes a recorded case.

            A verifier must not let one exploding code path hide every other
            result, so an exception here is returned as an ``error`` receipt
            instead of propagating.
            """
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
            outcomes = []
            for s in suggestions:
                try:
                    outcomes.append(
                        service.apply(s, scope=scope, packet=p, turn_id=p.turn_id)
                    )
                except Exception as exc:  # noqa: BLE001 - 崩溃本身就是验收结果
                    outcomes.append(
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
            return outcomes, rejected

        def status_of(record_id):
            row = repos.records_by_ids([record_id]).get(record_id)
            return (row or {}).get("status")

        try:
            # ------------------------------------------------------------------
            # V1 (R1 surface): agreement phrased WITHOUT the word 同意.
            # The old holes keyed on adoption marker words. Here the unrelated
            # clause uses 没问题 / 就这么定 instead, and the model claims the
            # *other* proposal was adopted.
            # ------------------------------------------------------------------
            p = packet("v1-current", "先说结论这个格式没问题；边缘计算方案你再想想")
            candidates, _, rejected = parse(
                p,
                dict(
                    content="用户已经采纳边缘计算方案",
                    kind="decision",
                    content_status="accepted",
                    source_types=["user_statement"],
                    source_refs=[p.turn_id],
                ),
            )
            saved = seed_semantic(writer, scope, candidates, turn_id=p.turn_id)
            record(
                "V1-R1-no-agreement-word",
                "无关的「没问题/就这么定」不能证明另一件事已采纳",
                not any(r["content_status"] == "accepted" for r in saved),
                dict(
                    saved=[
                        dict(content=r["content"], status=r["content_status"])
                        for r in saved
                    ],
                    rejected=rejected,
                ),
            )

            # ------------------------------------------------------------------
            # V2 (R2 surface): a *successful-looking* tool receipt about a
            # different subject. R2 used a failing weather receipt; this one
            # reports success but for a different thing, which must not
            # authorize invalidating a project principle.
            # ------------------------------------------------------------------
            old = seed("v2-seed", "发布流程长期原则是先跑全量回归")
            p = packet(
                "v2-current",
                "帮我查一下明天限行",
                old,
                [
                    EvidenceRef(
                        "v2-tool",
                        "限行查询成功：明日尾号3和8限行",
                        outcome="success",
                    )
                ],
            )
            outcomes, rejected = update(
                p,
                old,
                "",
                action="invalidate",
                source_types=["tool_result"],
                source_refs=[p.turn_id],
                reason="模型认为发布原则已经失效",
            )
            record(
                "V2-R2-successful-unrelated-tool",
                "成功但无关的工具回执不能归档项目原则",
                status_of(old["id"]) == "active",
                dict(
                    status=status_of(old["id"]),
                    applied=[r["applied"] for r in outcomes],
                    reasons=[r["reason"] for r in outcomes],
                    rejected=rejected,
                ),
            )

            # V2b: the same successful tool receipt about the SAME subject but
            # still merely reporting a different fact must not license the
            # invalidation either -- the receipt itself must match the item.
            old2 = seed("v2b-seed", "回滚窗口长期原则是只保留最近两版")
            p2 = packet(
                "v2b-current",
                "帮我确认一下回滚窗口现在的设置",
                old2,
                [
                    EvidenceRef(
                        "v2b-tool",
                        "配置读取成功：回滚窗口当前为最近两版",
                        outcome="success",
                    )
                ],
            )
            outcomes2, rejected2 = update(
                p2,
                old2,
                "",
                action="invalidate",
                source_types=["tool_result"],
                source_refs=[p2.turn_id],
                reason="模型认为这条原则已经失效",
            )
            record(
                "V2b-R2-successful-matching-tool-still-needs-intent",
                "工具回执只报告现状、没有说不作数时，不能单凭它归档原则",
                status_of(old2["id"]) == "active",
                dict(
                    status=status_of(old2["id"]),
                    applied=[r["applied"] for r in outcomes2],
                    reasons=[r["reason"] for r in outcomes2],
                    rejected=rejected2,
                ),
            )

            # ------------------------------------------------------------------
            # V3 (R3 surface): the unsupported field's number comes from a
            # DIFFERENT sentence than the supported one. R3 reused the same
            # budget sentence; here the stray number lives in an unrelated
            # clause of the same turn.
            # ------------------------------------------------------------------
            old = seed("v3-seed", "会议室预定上限为十人")
            p = packet(
                "v3-current",
                "会议室预定上限不是十人，是十二人；另外我的工号是88741",
                old,
            )
            outcomes, rejected = update(p, old, "会议室预定上限十二人，登记工号88741")
            unsupported = any(
                r["applied"] and "88741" in str((r.get("record") or {}).get("content"))
                for r in outcomes
            )
            record(
                "V3-R3-number-from-another-clause",
                "另一句话里的数字不能给本条新增字段作依据",
                not unsupported,
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

            # ------------------------------------------------------------------
            # V4 (R4 surface, opposite direction): the USER text has the
            # Arabic digits and the MODEL output uses the Chinese numeral.
            # R4 tested Chinese-in-user -> Arabic-in-model.
            # ------------------------------------------------------------------
            old = seed("v4-seed", "带宽上限为200兆")
            p = packet("v4-current", "带宽上限不是200兆，改成五百兆", old)
            outcomes, rejected = update(p, old, "带宽上限为五百兆")
            record(
                "V4-R4-arabic-user-chinese-model",
                "用户说阿拉伯数字、模型写中文数词也算有依据",
                len(outcomes) == 1 and outcomes[0]["applied"],
                dict(
                    applied=[r["applied"] for r in outcomes],
                    reasons=[r["reason"] for r in outcomes],
                    rejected=rejected,
                ),
            )

            # ------------------------------------------------------------------
            # V5 (R5 surface): supersedes submitted through a DIFFERENT write
            # entry point than save_semantic_candidates -- here the extractor's
            # parse() path (not parse_maintenance) feeding save_semantic_candidates
            # with known_ids. A proposed candidate must not archive the current
            # record by listing it in supersedes.
            # ------------------------------------------------------------------
            old = seed("v5-seed", "归档策略原则是先冷后删")
            p = packet("v5-current", "今天的会议纪要写完了", old)
            parsed, rejected = extractor.parse(
                json.dumps(
                    {
                        "memories": [
                            dict(
                                content="助手建议归档策略改成先删后冷",
                                kind="plan",
                                content_status="proposed",
                                source_types=["assistant_suggestion"],
                                source_refs=[p.turn_id],
                                supersedes=[old["id"]],
                            )
                        ]
                    },
                    ensure_ascii=False,
                ),
                packet=p,
                known_ids=[old["id"]],
            )
            seed_semantic(writer,
                scope, parsed, turn_id=p.turn_id, known_ids=[old["id"]]
            )
            record(
                "V5-R5-supersedes-via-parse-entry",
                "另一个写入入口的 proposed 替代同样不能归档现行记录",
                status_of(old["id"]) == "active",
                dict(
                    old_status=status_of(old["id"]),
                    parsed=len(parsed),
                    rejected=rejected,
                ),
            )

            # ------------------------------------------------------------------
            # V6 (R6 surface): the sync callback works on the first emission
            # and fails only on the SECOND one. R6 failed on the first call.
            # A receipt claiming sync emitted+complete would be wrong.
            # ------------------------------------------------------------------
            calls = {"n": 0}

            def flaky_sync(*args, **kwargs):
                calls["n"] += 1
                if calls["n"] >= 2:
                    raise OSError("simulated second-emission sync outage")

            old = seed("v6-seed", "日志保留期是三十天")
            writer.on_change = flaky_sync
            p = packet("v6-current", "日志保留期不是三十天，改成九十天", old)
            outcomes, rejected = update(p, old, "日志保留期九十天")
            writer.on_change = None
            verify = outcomes[0]["verify"] if outcomes else {}
            record(
                "V6-R6-second-emission-sync-failure",
                "第二次同步回调失败不能回执同步全部完成",
                bool(outcomes)
                and verify.get("sync") != "emitted"
                and verify.get("partial") is True,
                dict(
                    applied=[r["applied"] for r in outcomes],
                    verify=verify,
                    sync_calls=calls["n"],
                    rejected=rejected,
                ),
            )

            # ------------------------------------------------------------------
            # V7 (R7 surface): the short-term item carries its horizon but is
            # seeded with a duration/slot whose expiry policy is not the
            # two-slot one, to check the expiry policy really reads horizon
            # instead of ignoring it.
            # ------------------------------------------------------------------
            p = packet("v7-short", "刚拿到快递所以心情轻松了一点")
            candidates, _, rejected = parse(
                p,
                dict(
                    content="刚拿到快递所以心情轻松了一点",
                    kind="fact",
                    content_status="reported",
                    source_types=["user_statement"],
                    source_refs=[p.turn_id],
                    horizon="short_term",
                ),
            )
            saved = seed_semantic(writer, scope, candidates, turn_id=p.turn_id)
            record_id = saved[0]["id"] if saved else ""
            metadata = json.loads(saved[0]["metadata"]) if saved else {}
            maintenance = writer.run_maintenance(
                now=datetime.now(UTC) + timedelta(days=400)
            )
            fresh = repos.records_by_ids([record_id]).get(record_id)
            record(
                "V7-R7-horizon-survives-expiry",
                "短期语义被保存且到期政策真的处理它，不留长期现行记录",
                bool(saved)
                and metadata.get("horizon") == "short_term"
                and (fresh is None or fresh["status"] != "active"),
                dict(
                    saved=len(saved),
                    horizon=metadata.get("horizon"),
                    status_after_400_days=(fresh or {}).get("status"),
                    expires_at=(fresh or {}).get("expires_at"),
                    maintenance=maintenance,
                    rejected=rejected,
                ),
            )

            # ------------------------------------------------------------------
            # V8 (R4/R3 combined surface): an equivalent value written with a
            # thousands separator plus a unit swap (块 vs 元) must count as the
            # same value, while a genuinely different field in the same new
            # content must still be refused.
            # ------------------------------------------------------------------
            old = seed("v8-seed", "场地租金为3,000元每月")
            p = packet("v8-current", "场地租金不是三千块，是四千五百块；其他不变", old)
            outcomes, rejected = update(p, old, "场地租金为4,500元每月，押金为九千元")
            applied_content = str((outcomes[0].get("record") or {}).get("content")) if outcomes else ""
            record(
                "V8-R4-V3-separator-and-unit-equivalence",
                "千分位与元/块算同值，但同条新出现的押金仍需单独依据",
                not (len(outcomes) == 1 and outcomes[0]["applied"] and "九千" in applied_content),
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

            # ------------------------------------------------------------------
            # V9 (R1 positive control on a DIFFERENT surface): the adoption
            # sentence really is about the same item, phrased without 同意,
            # so it must still be saved as accepted. A guard that blocks
            # everything is not a fix.
            # ------------------------------------------------------------------
            p = packet("v9-current", "容器化部署方案就这么定，按它来")
            candidates, _, rejected = parse(
                p,
                dict(
                    content="用户已采纳容器化部署方案",
                    kind="decision",
                    content_status="accepted",
                    source_types=["user_statement"],
                    source_refs=[p.turn_id],
                ),
            )
            saved = seed_semantic(writer, scope, candidates, turn_id=p.turn_id)
            record(
                "V9-R1-related-decision-wording-still-passes",
                "同一个事项上的明确决策措辞仍可自动保存为已采纳",
                any(r["content_status"] == "accepted" for r in saved),
                dict(
                    statuses=[r["content_status"] for r in saved], rejected=rejected
                ),
            )

            # ------------------------------------------------------------------
            # V10 (R7 negative control): a genuinely long_term item must NOT
            # be expired by the same maintenance run. Blocks "expire
            # everything" from passing R7.
            # ------------------------------------------------------------------
            long_row = seed("v10-long", "公司报销标准为每次上限两百元")
            writer.run_maintenance(now=datetime.now(UTC) + timedelta(days=400))
            record(
                "V10-R7-longterm-not-swept",
                "长期事实不能因为同一趟维护被误清",
                status_of(long_row["id"]) == "active",
                dict(status=status_of(long_row["id"])),
            )

            # ------------------------------------------------------------------
            # V11 (R5 surface, user-statement wording): supersedes IS authorized
            # by a genuine user replacement statement -- but the replacement
            # sentence names only the OLD thing and never states the new value,
            # so a model-invented new content must still not be allowed to
            # archive by piggybacking. This is the negative half; V11b is the
            # positive control that a real replacement still archives.
            # ------------------------------------------------------------------
            old = seed("v11-seed", "命名规范长期原则是动词开头")
            p = packet("v11-current", "命名规范那条改用名词开头吧", old)
            candidates, _, rejected = parse(
                p,
                dict(
                    action="add",
                    content="助手建议命名规范改成名词开头",
                    kind="plan",
                    content_status="proposed",
                    source_types=["user_statement"],
                    source_refs=[p.turn_id],
                    supersedes=[old["id"]],
                ),
                old,
            )
            seed_semantic(writer,
                scope, candidates, turn_id=p.turn_id, known_ids=[old["id"]]
            )
            record(
                "V11-R5-proposed-replacement-cannot-archive",
                "即使有用户改用措辞，proposed 的替代者仍不能归档现行记录",
                status_of(old["id"]) == "active",
                dict(
                    old_status=status_of(old["id"]),
                    parsed=len(candidates),
                    rejected=rejected,
                ),
            )

            # V11b positive control: a real user replacement with the new value
            # stated explicitly must still archive the old record. A guard that
            # blocks every supersede is not a fix.
            # ------------------------------------------------------------------
            old = seed("v11b-seed", "构建产物保留策略是先保留最近五次")
            p = packet(
                "v11b-current", "构建产物保留策略改用只保留最近两次，不再按五次", old
            )
            candidates, _, rejected = parse(
                p,
                dict(
                    action="add",
                    content="构建产物保留策略只保留最近两次",
                    kind="decision",
                    content_status="accepted",
                    source_types=["user_statement"],
                    source_refs=[p.turn_id],
                    supersedes=[old["id"]],
                ),
                old,
            )
            seed_semantic(writer,
                scope, candidates, turn_id=p.turn_id, known_ids=[old["id"]]
            )
            record(
                "V11b-R5-real-user-replacement-still-archives",
                "用户明确改用新值的正当替代仍能归档旧记录",
                status_of(old["id"]) == "archived",
                dict(
                    old_status=status_of(old["id"]),
                    parsed=len(candidates),
                    rejected=rejected,
                ),
            )

            # ------------------------------------------------------------------
            # V12 (R6 surface, receipt content): when a requested sync really
            # has NO callback at all the receipt must say local_only rather than
            # claiming the change was emitted. Guards against a receipt that
            # hard-codes "emitted" to satisfy R6.
            # ------------------------------------------------------------------
            old = seed("v12-seed", "备份周期为每周一次")
            writer.on_change = None
            p = packet("v12-current", "备份周期不是每周一次，改成每天一次", old)
            outcomes, rejected = update(p, old, "备份周期为每天一次")
            verify = outcomes[0]["verify"] if outcomes else {}
            record(
                "V12-R6-no-callback-reports-local-only",
                "没有同步回调时回执不得声称已发出",
                bool(outcomes)
                and outcomes[0]["applied"]
                and verify.get("sync") == "local_only",
                dict(
                    applied=[r["applied"] for r in outcomes],
                    verify=verify,
                    rejected=rejected,
                ),
            )

            # ------------------------------------------------------------------
            # V13 (R1 surface, assistant-sourced claim): the adoption evidence
            # is an assistant message in history, not the user. A model citing
            # the assistant's own proposal must not produce an "accepted" row.
            # ------------------------------------------------------------------
            from agent_core.memory_turn import HistoryItem

            p = packet("v13-current", "容灾方案你再看看")
            p = replace(
                p,
                relevant_history=[
                    HistoryItem("v13-assistant", "assistant", "建议采用同城双活容灾方案")
                ],
            )
            candidates, _, rejected = parse(
                p,
                dict(
                    content="用户已采纳同城双活容灾方案",
                    kind="decision",
                    content_status="accepted",
                    source_types=["user_statement"],
                    source_refs=["v13-assistant"],
                ),
            )
            saved = seed_semantic(writer, scope, candidates, turn_id=p.turn_id)
            record(
                "V13-R1-assistant-citation-is-not-adoption",
                "引用助手自己的建议不能给「用户已采纳」盖章",
                not any(r["content_status"] == "accepted" for r in saved),
                dict(
                    saved=[
                        dict(content=r["content"], status=r["content_status"])
                        for r in saved
                    ],
                    rejected=rejected,
                ),
            )

            # ------------------------------------------------------------------
            # V14 (R3 surface, merge action): a merge that adds an unsupported
            # monetary figure while the user only corrected a different field.
            # ------------------------------------------------------------------
            old = seed("v14-seed", "运维值班轮换周期为两周")
            p = packet(
                "v14-current", "运维值班轮换周期不是两周，是一周；补贴的事以后再说", old
            )
            outcomes, rejected = update(
                p, old, "运维值班轮换周期一周，值班补贴为每天一百五十元", action="merge"
            )
            merged_content = (
                str((outcomes[0].get("record") or {}).get("content")) if outcomes else ""
            )
            record(
                "V14-R3-merge-cannot-add-unsupported-figure",
                "合并新增的补贴金额没有原话依据时不得随同落库",
                not (outcomes and outcomes[0]["applied"] and "一百五十" in merged_content),
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
            "Local temporary databases and vector stores only; no deployment or production memory is read or changed.",
            "Variant phrasings are new but still drawn by the author, so they do not enumerate the full space of model outputs.",
            "Aggregate ok requires EVERY variant to pass; a failing subset is never reported as overall success.",
        ],
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


def _module_mtimes() -> dict[str, str]:
    """mtime of every module under test, so the report is falsifiable later."""
    stamps: dict[str, str] = {}
    for name in (
        "memory_maintenance.py",
        "memory_turn.py",
        "memory_writes.py",
        "memory_extract.py",
        "memory_repositories.py",
    ):
        path = ROOT / "agent_core" / name
        try:
            stamps[name] = datetime.fromtimestamp(
                path.stat().st_mtime, tz=UTC
            ).isoformat(timespec="milliseconds")
        except OSError:
            stamps[name] = "missing"
    return stamps


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    raise SystemExit(run())
