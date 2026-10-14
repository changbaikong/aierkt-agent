"""分片F：对冻结版本做聚焦证伪（lead 亲手改动的两处）。

被验证的两处改动：
1) ``agent_core/memory_extract.py``：决定措辞词表接进候选侧与建议侧的采纳绑定；
   反向守卫：提问、明说先别定、有条件/假设的说法都不算决定。
2) ``agent_core/memory_maintenance.py``：逐项值证据只对 replace 放宽到「带值片段」，
   correct 维持严格逐项核对。

覆盖 task 指定的 a–f 六组证伪尝试，另外跑若干补充探针（不计入聚合判定，但如实记录）。
全部走真实入口：parse_maintenance / save_semantic_candidates / maintenance.apply，
数据库与向量库都在临时目录里，跑完即删。

退出码：0 = 每条必需项都通过；1 = 至少一条被证伪。崩溃照常抛出。
合成对抗输出只证明程序不变量，**不代表真实模型的出错频率**。
"""
from __future__ import annotations

import gc
import hashlib
import json
import logging
import os
import sys
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.memory_test_support import seed_semantic
from agent_core.contracts import Scope
from agent_core.memory_extract import SemanticMemoryExtractor
from agent_core.memory_maintenance import MemoryMaintenanceService, _is_value_bearing
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_turn import (
    MemoryCandidate,
    MemoryRef,
    is_hedged,
    sentences,
    turn_from_messages,
    unhedged_clauses,
    value_evidenced,
    value_tokens,
)
from agent_core.memory_writes import MemoryWriter

MODULES = (
    "memory_extract.py",
    "memory_maintenance.py",
    "memory_turn.py",
    "memory_writes.py",
    "memory_repositories.py",
)
OUTPUT = (
    Path(sys.argv[1])
    if len(sys.argv) > 1
    else ROOT / "docs" / "2026-10-12-memory-b-final-falsification.json"
)


class Embeddings:
    def embed(self, text):
        return [0.1, 0.2, 0.3]


def fingerprint() -> dict[str, dict[str, str]]:
    info: dict[str, dict[str, str]] = {}
    for name in MODULES:
        path = ROOT / "agent_core" / name
        raw = path.read_bytes()
        info[name] = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "mtime_utc": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(
                timespec="seconds"
            ),
        }
    return info


def run() -> int:
    before = fingerprint()
    checks: list[dict] = []
    probes: list[dict] = []

    def record(bucket: list[dict], identifier: str, requirement: str, passed: bool, actual) -> None:
        bucket.append(
            dict(id=identifier, requirement=requirement, passed=bool(passed), actual=actual)
        )

    timestamp = datetime.now(UTC).isoformat(timespec="milliseconds")
    with tempfile.TemporaryDirectory(
        prefix="memory-final-falsify-", ignore_cleanup_errors=True
    ) as folder:
        base = Path(folder)
        os.environ["QDRANT_MODE"] = "local"
        os.environ["MEMORY_SEMANTIC_ENABLED"] = "true"
        repos = initialize_memory_repositories(
            base / "records.db", base / "vectors", vector_size=3
        )
        writer = MemoryWriter(repos, Embeddings())
        service = MemoryMaintenanceService(writer, repos)
        extractor = SemanticMemoryExtractor(None)
        base_scope = Scope("review-user", "web", "final-falsify", "companion")

        def packet(turn: str, text: str, target=None, scope=None):
            p = turn_from_messages(
                turn_id=turn,
                occurred_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
                scope=scope or base_scope,
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
            return replace(p, recalled_memory_refs=refs)

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

        def seed(turn: str, text: str):
            return seed_semantic(writer,
                base_scope,
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

        def trace_of(text: str) -> dict:
            """如实记录绑定输入：句子、是否带条件、保留下来的分句及各自判据。"""
            from agent_core.memory_extract import _expresses_explicit_decision

            return dict(
                sentences=list(sentences(text)),
                sentence_hedged=[is_hedged(s) for s in sentences(text)],
                unhedged_clauses=list(unhedged_clauses(text)),
                decision_per_clause=[
                    _expresses_explicit_decision(c) for c in unhedged_clauses(text)
                ],
            )

        counter = {"n": 0}

        def adopt(identifier: str, user_text: str, content: str, kind: str = "decision") -> dict:
            """候选侧：模型输出 accepted 的候选，看程序是否盖章。走真实 parse+save。"""
            counter["n"] += 1
            probe_scope = Scope(
                "review-user", "web", f"{identifier}-{counter['n']}", "companion"
            )
            turn = f"{identifier}-turn-{counter['n']}"
            p = packet(turn, user_text, scope=probe_scope)
            item = dict(
                content=content,
                kind=kind,
                content_status="accepted",
                source_types=["user_statement"],
                source_refs=[turn],
            )
            candidates, _suggestions, rejected = parse(p, item)
            saved = (
                seed_semantic(writer, probe_scope, candidates, turn_id=turn)
                if candidates
                else []
            )
            return dict(
                id=identifier,
                user_text=user_text,
                candidate=content,
                parsed=len(candidates),
                rejected=rejected,
                saved=[
                    dict(content=r["content"], status=r["content_status"]) for r in saved
                ],
                stamped=any(r["content_status"] == "accepted" for r in saved),
                trace=trace_of(user_text),
            )

        def update(identifier: str, user_text: str, target, content: str, action: str) -> dict:
            """维护侧：真实 parse_maintenance + maintenance.apply。"""
            counter["n"] += 1
            turn = f"{identifier}-turn-{counter['n']}"
            p = packet(turn, user_text, target)
            item = dict(
                action=action,
                target_id=target["id"],
                expected_version=target["record_version"],
                content=content,
                kind=target["kind"],
                content_status="reported",
                source_types=["user_statement"],
                source_refs=[turn],
                reason="模型建议更新",
            )
            _candidates, suggestions, rejected = parse(p, item, target)
            outcomes = [
                service.apply(s, scope=base_scope, packet=p, turn_id=turn)
                for s in suggestions
            ]
            current = repos.records_by_ids([target["id"]])[target["id"]]
            return dict(
                id=identifier,
                user_text=user_text,
                proposed_content=content,
                parsed_suggestions=len(suggestions),
                rejected=rejected,
                applied=[o["applied"] for o in outcomes],
                reasons=[o["reason"] for o in outcomes],
                target_status=current["status"],
                target_content=current["content"],
            )

        try:
            # ---------------- (a) 疑问 / 否定 / 有条件句不得盖章 ----------------
            # 期望：control 必须盖章（否则「不盖章」全是假通过），其余一律不得盖章。
            group_a = [
                ("a1-question-prefix", "是不是要一律先落盘？", "项目日志先落盘再回传", False),
                ("a2-question-tail", "暂不部署吗", "云端部署暂不实施", False),
                ("a3-withhold", "先不要决定是否采用", "采用本地部署方案", False),
                ("a4-ruguo", "如果不累的话就一律先落盘", "项目日志先落盘再回传", False),
                ("a5-yaoshi", "要是预算够就一律先落盘", "项目日志先落盘再回传", False),
                ("a6-jiaru", "假如方案过了就改用方案B", "项目改用方案B", False),
                ("a7-wanyi", "万一失败就改成方案C", "项目改成方案C", False),
                ("a8-chufei", "除非你同意，否则一律先落盘", "项目日志先落盘再回传", False),
                ("a10-kan-qingkuang", "看情况吧，也许一律先落盘", "项目日志先落盘再回传", False),
                ("a11-yexu", "也许要一律先落盘", "项目日志先落盘再回传", False),
                ("a12-dagai", "大概要改成方案C", "项目改成方案C", False),
                ("a13-keneng", "可能要一律先落盘", "项目日志先落盘再回传", False),
                ("a14-control-plain", "项目日志一律先落盘再回传", "项目日志先落盘再回传", True),
            ]
            for identifier, user_text, content, expected in group_a:
                observed = adopt(identifier, user_text, content)
                record(
                    checks,
                    identifier,
                    "疑问/否定/有条件句不得盖章；无条件句必须盖章",
                    observed["stamped"] == expected,
                    dict(expected_stamped=expected, **observed),
                )

            # ---------------- (b) 无关事项不得互相盖章 ----------------
            group_b = [
                ("b1-shared-generic-word", "排期方案暂不调整", "部署方案立即执行", False),
                ("b2-shared-marker-only", "排期暂不调整", "云端部署暂不实施", False),
                ("b3-shared-switch-marker", "预算改成三千", "排期改成周二", False),
                ("b4-control-same-item", "排期暂不调整，下周再评估", "排期暂不调整", True),
            ]
            for identifier, user_text, content, expected in group_b:
                observed = adopt(identifier, user_text, content)
                record(
                    checks,
                    identifier,
                    "无关事项的决定措辞不得给另一件事盖章；同一件事必须盖章",
                    observed["stamped"] == expected,
                    dict(expected_stamped=expected, **observed),
                )

            # ---------------- (c) correct 塞入非数字定性内容必须被拒 ----------------
            # 对照组成对设计：同一句纠正，只差一个「原话没有的定性新增」。
            c_user = "客户名单按部门顺序回传不对，应该按工号顺序回传"
            old = seed("c-seed", "客户名单按部门顺序回传")
            observed = update(
                "c1-correct-unsupported-qualitative",
                c_user,
                old,
                "客户名单按工号顺序回传，并且加密",
                "correct",
            )
            record(
                checks,
                "c1-correct-unsupported-qualitative",
                "correct 动作塞入原话没有的非数字定性内容必须被拒",
                observed["applied"] == [False] and observed["target_status"] == "active",
                observed,
            )
            old = seed("c-seed-2", "客户名单按部门顺序回传")
            observed = update(
                "c2-control-correct-supported",
                c_user,
                old,
                "客户名单按工号顺序回传",
                "correct",
            )
            record(
                checks,
                "c2-control-correct-supported",
                "同一句纠正、去掉那处新增后必须放行（证明上面的拒绝不是假通过）",
                observed["applied"] == [True],
                observed,
            )

            # ---------------- (d) replace 塞入原话没有的数字必须仍被拒 ----------------
            # 对照组成对设计：同一轮原话、同一份替代，只差模型多塞了一个原话没有的数。
            d_user = "项目预算八千，改用方案B"
            old = seed("d-seed", "项目预算五千，选择方案A")
            observed = update(
                "d1-replace-unsupported-number",
                d_user,
                old,
                "项目预算八千，改用方案B，期限九十天",
                "replace",
            )
            record(
                checks,
                "d1-replace-unsupported-number",
                "replace 动作塞入原话没有的数字必须仍被拒",
                observed["applied"] == [False] and observed["target_status"] == "active",
                dict(
                    reason_names_the_number=any("九十" in r for r in observed["reasons"]),
                    **observed,
                ),
            )
            old = seed("d-seed-2", "项目预算五千，选择方案A")
            observed = update(
                "d2-control-replace-supported-number",
                d_user,
                old,
                "项目预算八千，改用方案B",
                "replace",
            )
            record(
                checks,
                "d2-control-replace-supported-number",
                "同一份替代、去掉那处新增数字后必须放行（证明上面的拒绝是数在起作用）",
                observed["applied"] == [True],
                observed,
            )

            # ---------------- (e) 中文数词普通词不得被误判成带值从而收紧放行 ----------------
            words = ("一律", "统一", "一样", "第一", "一些", "一定", "一并")
            verdicts = {word: _is_value_bearing(word) for word in words}
            record(
                checks,
                "e1-is-value-bearing-not-fooled",
                "含中文数词的普通词不得被 _is_value_bearing 判成带值",
                all(v is False for v in verdicts.values()),
                dict(_is_value_bearing=verdicts),
            )
            # 行为面：用户自己说的就是「统一…」，替代只换措辞，不得因「一」被收紧。
            old = seed("e-seed", "各模块日志格式各自不同")
            observed = update(
                "e2-replace-numeral-word-not-tightened",
                "改用统一的日志格式",
                old,
                "各模块使用统一的日志格式",
                "replace",
            )
            record(
                checks,
                "e2-replace-numeral-word-not-tightened",
                "用户原话含「统一」时，合理替代仍放行，不被中文数词误判收紧",
                observed["applied"] == [True],
                observed,
            )

            # ---------------- (f) 真实模型批次三个样例的原始措辞仍然正常 ----------------
            observed = adopt("f1-q1", "这个项目暂时先不部署，继续做本机验收。", "这个项目暂时不部署")
            record(
                checks,
                "f1-q1-temporarily-not-deploy",
                "Q1 原始措辞「这个项目暂时先不部署」仍能盖章",
                observed["stamped"] is True,
                observed,
            )
            observed = adopt(
                "f2-q11",
                "早上好。今天有点累，先歇会儿。另外项目日志一律先落盘再回传。",
                "项目日志先落盘再回传",
            )
            record(
                checks,
                "f2-q11-log-persist-first",
                "Q11 原始措辞「项目日志一律先落盘再回传」仍能盖章",
                observed["stamped"] is True,
                observed,
            )
            old = seed("f3-q6-seed", "项目选择方案A")
            observed = update(
                "f3-q6-switch-b-not-implemented",
                "改用方案B，不再用方案A；还没有实施。",
                old,
                "项目改用方案B",
                "replace",
            )
            record(
                checks,
                "f3-q6-switch-b-not-implemented",
                "Q6 原始措辞「改用方案B，不再用方案A；还没有实施」仍能落地更新",
                observed["applied"] == [True] and observed["target_status"] == "archived",
                observed,
            )

            # ---------------- 补充探针（不计入聚合判定，如实记录实际结果） ----------------
            for identifier, user_text, content in (
                ("p1-ruo-conditional", "若预算够就一律先落盘", "项目日志先落盘再回传"),
                ("p2-zhifao-conditional", "只要不忙就一律先落盘", "项目日志先落盘再回传"),
                ("p3-hedge-next-sentence", "如果累了就先歇会儿。另外项目日志一律先落盘再回传", "项目日志先落盘再回传"),
                ("p4-legit-decision-with-hedge-substring", "就照我刚才说的话一律先落盘", "项目日志先落盘再回传"),
                ("p6-comma-hypothetical", "如果预算够，就一律先落盘", "项目日志先落盘再回传"),
                ("p7-deng-zai", "等评审完了再改用方案B", "项目改用方案B"),
            ):
                probes.append(adopt(identifier, user_text, content))

            # replace 放宽后，原话没有的**非数字**新增能不能溜过去
            old = seed("p5-seed", "项目日志先落盘再回传")
            probes.append(
                update(
                    "p5-replace-fabricated-qualitative",
                    "改用新日志流程，先落盘再回传",
                    old,
                    "项目日志一律先落盘再回传，并且加了监控",
                    "replace",
                )
            )
            # 普通词里的中文数词：一律/统一 里的「一」会不会被当成一个「值」
            probes.append(
                dict(
                    id="p8-numeral-inside-ordinary-word",
                    value_tokens_of_yilu=list(value_tokens("一律")),
                    is_value_bearing_single_yi=_is_value_bearing("一"),
                    user_word_has_yi=value_evidenced("改用统一的日志格式", "一"),
                    user_word_lacks_yi=value_evidenced("改用新的日志流程", "一"),
                )
            )
            # 建议侧（lead 说候选侧与建议侧都接了）：无关事项的决定措辞也不得给建议盖章
            old = seed("p11-seed", "部署方案立即执行")
            counter["n"] += 1
            p11_turn = f"p11-turn-{counter['n']}"
            p11 = packet(p11_turn, "排期暂不调整", old)
            _c11, s11, rej11 = parse(
                p11,
                dict(
                    action="correct",
                    target_id=old["id"],
                    expected_version=old["record_version"],
                    content="部署方案必须立即执行",
                    kind="decision",
                    content_status="accepted",
                    source_types=["user_statement"],
                    source_refs=[p11_turn],
                    reason="模型建议更新",
                ),
                old,
            )
            probes.append(
                dict(
                    id="p11-suggestion-side-unrelated-wording",
                    user_text="排期暂不调整",
                    proposed_content="部署方案必须立即执行",
                    returned_statuses=[s.content_status for s in s11],
                    rejected=rej11,
                )
            )

            # 用户真说过的值，落在「不点名目标事项」的那一句里时，还算不算依据
            old = seed("d3-seed", "项目选择方案A")
            probes.append(
                update(
                    "p10-value-in-clause-without-item",
                    "改用方案B，不再用方案A，预算八千",
                    old,
                    "项目改用方案B，预算八千",
                    "replace",
                )
            )
            # 用户原话不含「一」、模型替代内容却含「一律」时，实际会怎样
            old = seed("p9-seed", "项目日志先落盘再回传")
            probes.append(
                update(
                    "p9-replace-yilu-not-in-user-words",
                    "改用新的日志流程，先落盘再回传",
                    old,
                    "项目日志一律先落盘再回传",
                    "replace",
                )
            )
        finally:
            repos.close()
            gc.collect()

    after = fingerprint()
    drift = {
        name: dict(before=before[name], after=after[name])
        for name in MODULES
        if before[name] != after[name]
    }
    failing = [c["id"] for c in checks if not c["passed"]]
    check_by_id = {c["id"]: c for c in checks}
    observed_by_id = {c["id"]: c["actual"] for c in checks}
    observed_by_id.update({p["id"]: p for p in probes})

    def saw_stamp(identifier: str) -> bool:
        return bool(observed_by_id.get(identifier, {}).get("stamped"))

    findings: list[dict] = []
    if not check_by_id["b1-shared-generic-word"]["passed"] and saw_stamp(
        "b1-shared-generic-word"
    ):
        findings.append(
            dict(
                id="b1-shared-generic-word",
                severity="中",
                kind="张冠李戴（事项绑定被普通词桥接）",
                summary="两个不同事项只要共用一个普通词（本例「方案」），其中一个的决定措辞就会给另一个盖章。",
                reproduction="用户原话「排期方案暂不调整」+ 模型输出 accepted 候选「部署方案立即执行」→ 保存状态 accepted。",
            )
        )
    for identifier, connective in (
        ("p1-ruo-conditional", "若…就"),
        ("p2-zhifao-conditional", "只要…就"),
    ):
        if saw_stamp(identifier):
            findings.append(
                dict(
                    id=identifier,
                    severity="中",
                    kind="条件句漏判（词表未覆盖的连接词）",
                    summary=f"「{connective}」这类条件句不在 HEDGE_MARKERS 里，仍被当成已决定盖章。",
                    reproduction=f"用户原话「{observed_by_id[identifier]['user_text']}」→ 保存状态 accepted。",
                )
            )
    if not saw_stamp("p4-legit-decision-with-hedge-substring"):
        findings.append(
            dict(
                id="p4-legit-decision-with-hedge-substring",
                severity="低",
                kind="误伤（否定方向）",
                summary="句中出现条件词子串就算带条件，「的话」把一句明确的决定也一起否掉了。",
                reproduction="用户原话「就照我刚才说的话一律先落盘」→ 保存状态 proposed（明明已定下来）。",
            )
        )
    ambiguous = []
    if saw_stamp("p7-deng-zai"):
        ambiguous.append(
            dict(
                id="p7-deng-zai",
                severity="信息（不计为证伪）",
                summary="「等评审完了再改用方案B」被盖章。按项目自己的「决定/执行分开」语义（Q6「还没有实施」仍算决定），这是可辩护的：换方案已定，只是等评审后执行。故不计入证伪，只登记实际结果。",
            )
        )

    # 残留项：lead 明确声明「本轮不修、当已知代价保留」的两条，单独归类，不淡化也不混进证伪。
    residual: list[dict] = []
    if observed_by_id.get("p10-value-in-clause-without-item", {}).get("applied") == [False]:
        residual.append(
            dict(
                id="p10-value-in-clause-without-item",
                severity="中（误拒，方向偏保守）",
                kind="值证据取证范围过窄",
                status="lead 声明本轮不修，作为已知代价保留",
                summary="用户在同一轮里真说过的值，只要落在不点名目标事项的那一句里，就不算依据，正当替代会被整条拒掉。这是 V3 那条「只认讲目标事项的句子」判据的范围取舍，与本次 lead 描述的「只对 replace 放宽带值片段」不是同一处改动。",
                reproduction="用户原话「改用方案B，不再用方案A，预算八千」+ replace 内容「项目改用方案B，预算八千」→ applied=False，理由称「八千」在原话里找不到，但用户刚刚说过这个数。",
            )
        )
    if observed_by_id.get("p9-replace-yilu-not-in-user-words", {}).get("applied") == [False]:
        residual.append(
            dict(
                id="p9-replace-yilu-not-in-user-words",
                severity="低",
                kind="中文数词被当成值（方向偏保守）",
                status="lead 声明本轮不修，作为已知代价保留",
                summary="「一律」被 value_tokens 切成数值片段「一」并判成带值，用户原话不含「一」时含「一律」的替代会被拒；方向是保守的，但拒绝理由误导（报的是「一」缺依据）。",
                reproduction="用户原话「改用新的日志流程，先落盘再回传」+ replace 内容「项目日志一律先落盘再回传」→ applied=False，理由指「一」缺依据。",
                supporting_probe=observed_by_id.get("p8-numeral-inside-ordinary-word"),
            )
        )

    verdict = (
        "FALSIFIED"
        if failing
        else (
            "REQUIRED_CHECKS_PASS_WITH_KNOWN_RESIDUALS"
            if residual
            else "NO_FALSIFICATION_FOUND"
        )
    )
    report = dict(
        ok=not failing,
        verdict=verdict,
        passed=sum(c["passed"] for c in checks),
        total=len(checks),
        failing_checks=failing,
        checks=checks,
        probes=probes,
        falsified_findings=findings,
        known_residual_limits=residual,
        ambiguous_or_defensible=ambiguous,
        not_falsified=sorted(
            [c["id"] for c in checks if c["passed"]] + [p["id"] for p in probes if p["id"] not in {f["id"] for f in findings}]
        ),
        run_at_utc=timestamp,
        tested_module_fingerprints=before,
        module_drift_during_run=drift,
        live_model_calls=0,
        production_memory_touched=False,
        limits=[
            "Synthetic hand-built adversarial model outputs verify program invariants, not live-model error frequency.",
            "Local temporary databases and vector stores only; no production memory is read or changed.",
            "(f) reproduces the three live-batch user wordings through hand-built model items, not the live model's own output.",
            "Adversarial wordings are author-drawn, so they do not enumerate the full space of model outputs.",
            "Aggregate ok requires EVERY required check to pass; a failing subset is never reported as overall success.",
        ],
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    # 证据写入失败（例如系统权限拒绝写这个路径）不能把检查结果一起吞掉：
    # 先记下来，再退回可写目录，报告里如实写明这一步发生了。
    try:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(payload, encoding="utf-8")
    except OSError as exc:
        fallback = Path(__file__).resolve().parents[1] / ".pytmp" / "evidence" / OUTPUT.name
        fallback.parent.mkdir(parents=True, exist_ok=True)
        fallback.write_text(payload, encoding="utf-8")
        report["evidence_write_fallback"] = {
            "requested": str(OUTPUT),
            "written_to": str(fallback),
            "error": f"{type(exc).__name__}: {exc}",
        }
        payload = json.dumps(report, ensure_ascii=False, indent=2)
        print(payload)
        return 0 if report["ok"] else 1
    print(payload)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    raise SystemExit(run())
