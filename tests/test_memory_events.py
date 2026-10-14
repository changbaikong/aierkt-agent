"""业务结果事件 → 记忆候选（B 计划第五步）。"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from agent_core.contracts import Scope
from agent_core.memory_events import (
    MemoryEvent,
    candidates_from_event,
    event_from_task_result,
    record_event,
)
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_writes import MemoryWriter


class _Embed:
    def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


def _event(**overrides) -> MemoryEvent:
    base = dict(event_id="task-1", state="completed")
    base.update(overrides)
    return MemoryEvent(**base)


class EventContractTests(unittest.TestCase):
    def test_unknown_state_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _event(state="maybe")

    def test_event_id_is_required(self) -> None:
        with self.assertRaises(ValueError):
            _event(event_id="  ")


class ProgressIsNotAConclusionTests(unittest.TestCase):
    def test_progress_and_running_events_produce_no_candidates(self) -> None:
        for state in ("progress", "started", "running"):
            with self.subTest(state=state):
                self.assertEqual(
                    candidates_from_event(_event(state=state, conclusion="正在处理")), []
                )

    def test_cancelled_events_produce_no_candidates(self) -> None:
        self.assertEqual(candidates_from_event(_event(state="cancelled", conclusion="已取消")), [])


class CompletedEventTests(unittest.TestCase):
    def test_generated_files_with_failing_tests_are_recorded_separately(self) -> None:
        """文件生成 ≠ 测试通过：产物事实、验证失败、剩余工作要分开记。"""
        event = _event(
            goal="修好记忆队列",
            conclusion="产物已生成但测试失败",
            artifacts=("agent_core/memory_queue.py",),
            evidence=("sha256:abc",),
            tests_passed=False,
            failure_reason="断言未通过",
            failure_conditions=("只跑了单测，未跑全量",),
            leftovers=("修 test_memory_queue",),
        )
        candidates = candidates_from_event(event)
        kinds = {(item.kind, item.content_status) for item in candidates}
        self.assertIn(("fact", "verified"), kinds)
        self.assertIn(("experience", "observed"), kinds)
        self.assertIn(("open_question", "open"), kinds)
        # 不能出现「计划已完成」。
        self.assertNotIn(("plan", "completed"), kinds)
        failure = [item for item in candidates if item.kind == "experience"][0]
        self.assertIn("只跑了单测，未跑全量", failure.content)

    def test_verified_completion_needs_evidence_and_passing_tests(self) -> None:
        done = _event(
            conclusion="队列改造完成",
            artifacts=("agent_core/memory_queue.py",),
            evidence=("全量测试通过",),
            tests_passed=True,
        )
        kinds = {(item.kind, item.content_status) for item in candidates_from_event(done)}
        self.assertIn(("plan", "completed"), kinds)

    def test_completion_without_evidence_is_only_reported(self) -> None:
        bare = _event(conclusion="队列改造完成", tests_passed=True)
        kinds = {(item.kind, item.content_status) for item in candidates_from_event(bare)}
        self.assertNotIn(("plan", "completed"), kinds)
        self.assertIn(("experience", "observed"), kinds)

    def test_subagent_self_claim_is_unverified(self) -> None:
        claimed = _event(
            conclusion="子代理说已经改好了",
            evidence=("子代理自述",),
            tests_passed=True,
            claimed_by_subagent=True,
        )
        candidates = candidates_from_event(claimed)
        self.assertNotIn(("plan", "completed"), {(c.kind, c.content_status) for c in candidates})
        self.assertEqual(candidates[0].content_status, "tentative")
        self.assertIn("未核实", candidates[0].content)

    def test_artifact_without_evidence_is_not_verified(self) -> None:
        event = _event(artifacts=("out.txt",), conclusion="")
        self.assertEqual(candidates_from_event(event)[0].content_status, "reported")


class FailedEventTests(unittest.TestCase):
    def test_failure_keeps_its_conditions_and_leftovers(self) -> None:
        event = _event(
            state="failed",
            conclusion="写库失败",
            failure_reason="database is locked",
            failure_conditions=("并发写入未加忙等",),
            leftovers=("加 busy_timeout",),
        )
        candidates = candidates_from_event(event)
        self.assertEqual(candidates[0].kind, "experience")
        self.assertIn("并发写入未加忙等", candidates[0].content)
        self.assertEqual(candidates[0].reason, "database is locked")
        self.assertEqual(candidates[1].kind, "open_question")

    def test_failure_without_conditions_says_so_instead_of_inventing_them(self) -> None:
        candidates = candidates_from_event(_event(state="failed", conclusion="失败"))
        self.assertIn("未记录触发条件", candidates[0].content)


class TaskResultAdapterTests(unittest.TestCase):
    def test_payload_maps_only_declared_fields(self) -> None:
        event = event_from_task_result(
            {
                "task_id": "t-9",
                "status": "failed",
                "goal": "跑测试",
                "error": "boom",
                "unknown_field": "忽略",
            }
        )
        self.assertEqual(event.event_id, "t-9")
        self.assertEqual(event.state, "failed")
        self.assertEqual(event.failure_reason, "boom")
        self.assertIsNone(event.tests_passed)


class RecordEventTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        self.repo = initialize_memory_repositories(root / "memory.db", root / "vectors", vector_size=3)
        self.addCleanup(self.repo.close)
        self.writer = MemoryWriter(self.repo, _Embed())
        self.scope = Scope("user", "web", "conv-1")

    def test_recording_is_idempotent_per_event(self) -> None:
        event = _event(conclusion="队列改造完成", artifacts=("q.py",), evidence=("测试通过",), tests_passed=True)
        first = record_event(self.writer, self.scope, event)
        second = record_event(self.writer, self.scope, event)
        self.assertEqual(sorted(r["id"] for r in first), sorted(r["id"] for r in second))
        self.assertEqual(len(self.writer.list_records(self.scope)), len(first))

    def test_progress_event_writes_nothing(self) -> None:
        self.assertEqual(record_event(self.writer, self.scope, _event(state="progress")), [])
        self.assertEqual(self.writer.list_records(self.scope), [])


if __name__ == "__main__":
    unittest.main()
