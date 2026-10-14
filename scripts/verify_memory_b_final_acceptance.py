"""B 计划最终验收入口：把全部必需检查收在一处，任何一项失败即整体不通过。

覆盖四块，缺一不可：

1. **既有反例脚本全部重跑**（原 11 条 B 边界、6 条自动维护、9 条维护闭环、
   搜索融合、双端语义、第三/四/五轮、最终证伪、无依据结论变体、真实候选寿命重放）；
2. **新增变体**：虚构审批、虚构执行、虚构评审结论、同义纠正、扩大适用范围、
   跨事项数值、虚构条件与原因；
3. **寿命重放**：真实批次里三次「暂不部署」候选推进两天、当天情绪到期、
   用户明说三天期限按三天；
4. **两条遗留项**：同事项「预算八千」可更新、普通词里的「一」不再触发数值检查，
   同时保留跨事项与扩大范围的负例。

判分规则：``ok`` 只有在**每一项必需检查都通过**时才为真；不接受「已知代价」
排除，也不把脚本初筛当成语义终审。全部离线运行，``live_model_calls=0``；
真实模型批次另有入口，不在这里冒充。
"""
from __future__ import annotations

import gc
import asyncio
import importlib.util
import inspect
import io
import json
import logging
import os
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from dataclasses import replace

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))

from scripts.memory_test_support import seed_semantic
from agent_core.contracts import Scope
from agent_core.memory_extract import SemanticMemoryExtractor
from agent_core.memory_maintenance import MemoryMaintenanceService
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_turn import MemoryCandidate, MemoryRef, turn_from_messages
from agent_core.memory_writes import MemoryWriter

#: 必须全部通过的既有脚本（顺序 = 报告里的顺序）。
EXISTING = (
    "verify_memory_b_acceptance_edges.py",
    "verify_memory_maintenance_acceptance_edges.py",
    "verify_memory_maintenance_loop.py",
    "verify_memory_search_fusion.py",
    "verify_memory_dual_end_semantics.py",
    "verify_memory_b_third_review.py",
    "verify_memory_b_fourth_review.py",
    "verify_memory_b_round5_falsify.py",
    "verify_memory_final_falsification.py",
    "verify_memory_b_unsupported_claims_review.py",
    "verify_memory_b_live_retention_review.py",
)


class Embeddings:
    def embed(self, _text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


def run_existing() -> list[dict]:
    """逐个跑既有脚本（同进程调用 run()／main()），记录通过数与判定。"""
    results: list[dict] = []
    for name in EXISTING:
        path = SCRIPTS / name
        entry = {"script": name, "passed": 0, "total": 0, "ok": False, "error": ""}
        buffer = io.StringIO()
        try:
            spec = importlib.util.spec_from_file_location(f"final_{path.stem}", path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            assert spec.loader is not None
            with redirect_stdout(buffer):
                spec.loader.exec_module(module)
                entry_fn = getattr(module, "run", None) or getattr(module, "main", None)
                if entry_fn is None:
                    raise AttributeError(f"{name} 没有 run()/main() 入口")
                outcome = entry_fn()
                if inspect.iscoroutine(outcome):
                    outcome = asyncio.run(outcome)
            payload = _parse_report(buffer.getvalue())
            if isinstance(outcome, dict):
                # 有的脚本把报告本身当返回值（不是退出码），以报告为准。
                payload = payload or outcome
                entry["ok"] = bool(payload.get("ok"))
            else:
                entry["ok"] = bool(payload.get("ok")) and outcome in (0, None, True)
            entry["passed"] = int(payload.get("passed") or 0)
            entry["total"] = int(payload.get("total") or 0)
        except Exception as exc:  # noqa: BLE001 - 脚本崩溃本身就是验收结果
            entry["error"] = f"{type(exc).__name__}: {exc}"
        results.append(entry)
        gc.collect()
    return results


def _parse_report(text: str) -> dict:
    start = text.find("{")
    while start >= 0:
        try:
            return json.loads(text[start:])
        except ValueError:
            start = text.find("{", start + 1)
    return {}


def _packet(scope, turn: str, text: str, target: dict | None = None):
    packet = turn_from_messages(
        turn_id=turn,
        occurred_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
        scope=scope,
        user_text=text,
        published_reply="收到",
    )
    if target is None:
        return packet
    return replace(
        packet,
        recalled_memory_refs=[
            MemoryRef(
                target["id"],
                target["kind"],
                target["content_status"],
                target["content"],
                version=int(target["record_version"]),
            )
        ],
    )


def run_variants(scope, writer, service, extractor) -> list[dict]:
    """新增变体：错误的写不进去，正常的写得进去。"""
    checks: list[dict] = []

    def seed(turn: str, text: str) -> dict:
        return seed_semantic(writer,
            scope,
            [
                MemoryCandidate(
                    content=text,
                    kind="decision",
                    content_status="accepted",
                    source_types=("user_statement",),
                    source_refs=(turn,),
                )
            ],
            turn_id=turn,
        )[0]

    def update(name: str, user_text: str, old: dict, content: str, **extra):
        packet = _packet(scope, f"{name}-turn", user_text, old)
        item = dict(
            action="replace",
            target_id=old["id"],
            expected_version=int(old["record_version"]),
            content=content,
            kind="decision",
            content_status="accepted",
            source_types=["user_statement"],
            source_refs=[packet.turn_id],
            reason="模型建议更新",
        )
        item.update(extra)
        _, suggestions, rejected = extractor.parse_maintenance(
            json.dumps({"memories": [item]}, ensure_ascii=False),
            packet=packet,
            known_ids=[old["id"]],
            known_versions={old["id"]: int(old["record_version"])},
        )
        outcomes = []
        for suggestion in suggestions:
            try:
                outcomes.append(service.apply(suggestion, scope=scope, packet=packet, turn_id=packet.turn_id))
            except Exception as exc:  # noqa: BLE001
                outcomes.append({"applied": False, "reason": f"CRASH {type(exc).__name__}: {exc}"})
        return packet, outcomes, rejected

    def record(name: str, requirement: str, passed: bool, actual) -> None:
        checks.append(
            dict(id=name, requirement=requirement, passed=bool(passed), actual=actual)
        )

    # --- 错误事实不能借替代写入 -------------------------------------------
    for name, content, old_text, label in (
        ("claim-approval", "部署方案改用方案B，安全评审已通过", "部署方案为方案A", "虚构评审通过"),
        ("claim-execution", "部署方案改用方案B，回滚演练已经成功", "部署方案为方案A", "虚构演练成功"),
        ("claim-window", "容灾方案改用方案B，实施窗口已获批准", "容灾方案为方案A", "既有 W8 虚构批准结论"),
    ):
        old = seed(f"{name}-seed", f"{old_text}（{name}）")
        _packet_obj, outcomes, rejected = update(
            name, "部署方案改用方案B，不再用方案A；尚未实施，也没有评审结论。", old, content
        )
        record(
            name,
            f"{label}：没有出处的结论不得随替代写入",
            not any(item["applied"] for item in outcomes),
            dict(
                applied=[dict(applied=i["applied"], reason=i["reason"]) for i in outcomes],
                old_status=_status(service, old["id"]),
                rejected=rejected,
            ),
        )

    # --- 正常表达必须通过 --------------------------------------------------
    for name, content, user_text in (
        ("control-switch", "部署方案改用方案B", "部署方案改用方案B，不再用方案A"),
        ("control-synonym", "部署方案决定使用方案B", "部署方案改用方案B，不再用方案A"),
    ):
        old = seed(f"{name}-seed", "部署方案为方案A")
        _packet_obj, outcomes, rejected = update(name, user_text, old, content)
        record(
            name,
            f"正常表达必须保存：{content}",
            any(item["applied"] for item in outcomes),
            dict(applied=[dict(applied=i["applied"], reason=i["reason"]) for i in outcomes], rejected=rejected),
        )

    # 纠正的同义改写（用户原话用了「用」，模型写「使用」）
    old = seed("control-correct-seed", "回滚方案选择使用方案A")
    _packet_obj, outcomes, rejected = update(
        "control-correct-synonym",
        "回滚方案不是选择使用方案A，是决定使用方案B",
        old,
        "回滚方案决定使用方案B",
        action="correct",
    )
    record(
        "control-correct-synonym",
        "同义改写不算新值：纠正也允许换说法",
        any(item["applied"] for item in outcomes),
        dict(applied=[dict(applied=i["applied"], reason=i["reason"]) for i in outcomes], rejected=rejected),
    )

    # --- 扩大适用范围与跨事项数值仍是负例 ---------------------------------
    old = seed("scope-seed", "值班安排为轮换排班")
    _packet_obj, outcomes, rejected = update(
        "scope-expansion",
        "今天先这样做",
        old,
        "值班安排为轮换排班，以后全部这样做",
        action="correct",
    )
    record(
        "scope-expansion",
        "「今天先这样做」不能变成「以后全部这样做」",
        not any(item["applied"] for item in outcomes),
        dict(applied=[dict(applied=i["applied"], reason=i["reason"]) for i in outcomes], rejected=rejected),
    )

    old = seed("cross-item-seed", "会议室预定上限为十人")
    _packet_obj, outcomes, rejected = update(
        "cross-item-number",
        "会议室预定上限改成十二人；另外我的工号是88741",
        old,
        "会议室预定上限为十二人，工号88741",
        action="correct",
    )
    record(
        "cross-item-number",
        "另一件事的号码不能给这件事当依据",
        not any(item["applied"] for item in outcomes),
        dict(applied=[dict(applied=i["applied"], reason=i["reason"]) for i in outcomes], rejected=rejected),
    )

    # --- 虚构条件与原因 ----------------------------------------------------
    old = seed("condition-seed", "部署策略为先灰度后全量")
    _packet_obj, outcomes, rejected = update(
        "invented-condition",
        "部署策略不是先灰度后全量，是先全量后灰度",
        old,
        "如果预算允许就先全量后灰度",
        action="correct",
    )
    record(
        "invented-condition",
        "原话里没有的前提条件不能写进新版本",
        not any(item["applied"] for item in outcomes),
        dict(applied=[dict(applied=i["applied"], reason=i["reason"]) for i in outcomes], rejected=rejected),
    )

    # --- 候选替代旁路同样接入 ---------------------------------------------
    old = seed("candidate-bypass-seed", "数据项目原则是本地优先")
    bypass_text = "数据项目改用云端优先，安全评审已通过"
    packet = _packet(scope, "candidate-bypass-turn", "数据项目改用云端优先，不再本地优先", old)
    raw = json.dumps(
        {
            "memories": [
                {
                    "action": "add",
                    "content": bypass_text,
                    "kind": "decision",
                    "content_status": "accepted",
                    "source_types": ["user_statement"],
                    "source_refs": [packet.turn_id],
                    "supersedes": [old["id"]],
                }
            ]
        },
        ensure_ascii=False,
    )
    candidates, _suggestions, rejected = extractor.parse_maintenance(
        raw, packet=packet, known_ids=[old["id"]]
    )
    saved = writer.save_semantic_candidates(
        scope, candidates, turn_id=packet.turn_id, packet=packet, known_ids=[old["id"]]
    )
    record(
        "candidate-bypass-claim",
        "新建候选借替代字段塞入无依据结论时必须整条不落库",
        not saved and _status(service, old["id"]) == "active",
        dict(saved=[r.get("content") for r in saved], rejected=rejected, old_status=_status(service, old["id"])),
    )
    return checks


def _status(service, record_id: str) -> str:
    row = service.repositories.records_by_ids([record_id]).get(record_id) or {}
    return str(row.get("status") or "")


def run_lifetime(scope, writer) -> list[dict]:
    """寿命重放：真实候选 + 当天情绪 + 明示期限。"""
    checks: list[dict] = []
    evidence_path = ROOT / "docs" / "2026-10-12-memory-quality-independent-rerun-evidence.json"
    recorded = json.loads(evidence_path.read_text(encoding="utf-8"))
    extractor = SemanticMemoryExtractor(None)

    for result in recorded["runs"]:
        if result["case"] != "Q1":
            continue
        saved = seed_semantic(writer,
            scope,
            [MemoryCandidate(**item) for item in result["candidates"]],
            turn_id=f"Q1-{result['repeat']}",
            project_binding="isolated-final-project",
        )
        writer.run_maintenance(now=datetime.now(UTC) + timedelta(days=2))
        after = writer.repositories.records_by_ids([row["id"] for row in saved])
        alive = [row for row in after.values() if str(row["status"]) in ("active", "candidate")]
        checks.append(
            dict(
                id=f"live-retention-Q1-{result['repeat']}",
                requirement="真实「暂不部署」候选推进两天后仍然有效",
                passed=len(alive) == len(saved) and bool(saved),
                actual=dict(
                    user_text=result["packet"]["user_text"],
                    before=[row["content"] for row in saved],
                    after=[dict(content=row["content"], status=row["status"], expires_at=row["expires_at"]) for row in after.values()],
                ),
            )
        )

    def _save(user_text: str, content: str, turn: str) -> dict:
        packet = _packet(scope, turn, user_text)
        raw = json.dumps(
            {
                "memories": [
                    {
                        "content": content,
                        "kind": "fact",
                        "content_status": "reported",
                        "source_types": ["user_statement"],
                        "source_refs": [turn],
                        "horizon": "short_term",
                    }
                ]
            },
            ensure_ascii=False,
        )
        candidates, _suggestions, rejected = extractor.parse_maintenance(raw, packet=packet)
        assert len(candidates) == 1, rejected
        return writer.save_semantic_candidates(
            scope, candidates, turn_id=turn, packet=packet
        )[0]

    mood = _save("今天有点累，先歇会儿。", "今天有点累", "mood-turn")
    writer.run_maintenance(now=datetime.now(UTC) + timedelta(days=2))
    mood_after = writer.repositories.records_by_ids([mood["id"]])[mood["id"]]
    checks.append(
        dict(
            id="same-day-mood-expires",
            requirement="当天情绪仍按现有短期政策到期",
            passed=str(mood_after["status"]) != "active",
            actual=dict(status=mood_after["status"], expires_at=mood_after["expires_at"]),
        )
    )

    timed = _save("这条约束保留三天：服务器先只读。", "服务器先只读", "three-day-turn")
    remaining = datetime.fromisoformat(str(timed["expires_at"])) - datetime.now(UTC)
    checks.append(
        dict(
            id="explicit-three-days",
            requirement="用户明说保留三天时按三天设期限",
            passed=timedelta(days=2, hours=20) < remaining < timedelta(days=3, hours=1),
            actual=dict(expires_at=timed["expires_at"], remaining_hours=round(remaining.total_seconds() / 3600, 1)),
        )
    )
    return checks


def run_legacy(scope, writer, service, extractor) -> list[dict]:
    """两条遗留项：同事项「预算八千」可更新、普通词里的「一」不再当数值。"""
    checks: list[dict] = []

    packet = turn_from_messages(
        turn_id="legacy-budget-turn",
        occurred_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
        scope=scope,
        user_text="部署方案改用方案B，不再用方案A，预算八千",
        published_reply="收到",
    )
    old = seed_semantic(writer,
        scope,
        [
            MemoryCandidate(
                content="遗留项：部署方案为方案A",
                kind="decision",
                content_status="accepted",
                source_types=("user_statement",),
                source_refs=("legacy-seed",),
            )
        ],
        turn_id="legacy-seed",
    )[0]
    version = int(old["record_version"])
    packet = replace(
        packet,
        # 证据时间必须晚于记录形成时间：包先建、记录后写时，毫秒差会被判成
        # 「证据早于现行版本」而拒绝，这一条就会随机器快慢时通时不通。
        occurred_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
        recalled_memory_refs=[
            MemoryRef(old["id"], old["kind"], old["content_status"], old["content"], version=version)
        ],
    )
    raw = json.dumps(
        {
            "memories": [
                {
                    "action": "replace",
                    "target_id": old["id"],
                    "expected_version": version,
                    "content": "遗留项：部署方案改用方案B，预算八千",
                    "kind": "decision",
                    "content_status": "accepted",
                    "source_types": ["user_statement"],
                    "source_refs": ["legacy-budget-turn"],
                    "reason": "用户改用方案B并给出预算",
                }
            ]
        },
        ensure_ascii=False,
    )
    _candidates, suggestions, rejected = extractor.parse_maintenance(
        raw, packet=packet, known_ids=[old["id"]], known_versions={old["id"]: version}
    )
    outcomes = [service.apply(s, scope=scope, packet=packet, turn_id="legacy-budget-turn") for s in suggestions]
    checks.append(
        dict(
            id="legacy-budget-eight-thousand",
            requirement="同一事项明确给出的「预算八千」可以更新",
            passed=any(item["applied"] for item in outcomes),
            actual=dict(applied=[dict(applied=i["applied"], reason=i["reason"]) for i in outcomes], rejected=rejected),
        )
    )

    from agent_core.memory_turn import changed_value_tokens, value_tokens

    words = {word: value_tokens(f"项目日志{word}先落盘再回传") for word in ("一律", "统一", "一样", "一致")}
    numeric = changed_value_tokens("项目日志一律先落盘再回传", "项目日志先落盘再回传")
    checks.append(
        dict(
            id="legacy-ordinary-numeral-word",
            requirement="普通词里的「一」不再触发数值检查",
            passed=all(not tokens for tokens in words.values())
            and "一" not in numeric,
            actual=dict(value_tokens=words, changed=numeric),
        )
    )
    # 反方向仍在：真正的数值缺依据照样拒绝。
    numbers = value_tokens("预算八千元")
    checks.append(
        dict(
            id="legacy-real-numbers-still-checked",
            requirement="收窄只针对普通词：真正的数值照旧是数值",
            passed=numbers == ("八千",),
            actual=dict(value_tokens=numbers),
        )
    )
    return checks


def main() -> int:
    logging.disable(logging.CRITICAL)
    os.environ["QDRANT_MODE"] = "local"
    os.environ["MEMORY_SEMANTIC_ENABLED"] = "true"
    checks: list[dict] = []
    existing: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="final-acceptance-", ignore_cleanup_errors=True) as folder:
        base = Path(folder)
        os.environ["QDRANT_PATH"] = str(base / "vectors")
        repos = initialize_memory_repositories(base / "records.db", base / "vectors", vector_size=3)
        scope = Scope("final-acceptance", "web", "final", "companion")
        writer = MemoryWriter(repos, Embeddings())
        service = MemoryMaintenanceService(writer, repos)
        extractor = SemanticMemoryExtractor(None)
        try:
            checks.extend(run_variants(scope, writer, service, extractor))
            checks.extend(run_lifetime(scope, writer))
            checks.extend(run_legacy(scope, writer, service, extractor))
        finally:
            repos.close()
            gc.collect()
    existing = run_existing()

    required_failed = [item["id"] for item in checks if not item["passed"]]
    scripts_failed = [item["script"] for item in existing if not item["ok"]]
    report = dict(
        ok=not required_failed and not scripts_failed,
        verdict=(
            "ALL_REQUIRED_CHECKS_PASS"
            if not required_failed and not scripts_failed
            else "NOT_PASSED"
        ),
        existing_scripts=existing,
        new_checks=checks,
        new_passed=sum(1 for item in checks if item["passed"]),
        new_total=len(checks),
        failed_required=required_failed,
        failed_scripts=scripts_failed,
        live_model_calls=0,
        production_memory_touched=False,
        run_at_utc=datetime.now(UTC).isoformat(timespec="milliseconds"),
        limits=[
            "离线反例只证明程序会／不会接受哪些输出，不代表真实模型产生这类错误的频率。",
            "寿命重放用模拟时钟推进两天，不是真实长跑观察。",
            "真实模型 54 次批次另有入口，本文件的 live_model_calls 为 0。",
        ],
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    out = ROOT / ".pytmp" / "evidence" / "2026-10-12-memory-b-final-acceptance.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(payload, encoding="utf-8")
    print(payload)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
