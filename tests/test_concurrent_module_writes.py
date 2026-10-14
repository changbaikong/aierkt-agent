"""并发写入压力测试：验证多模块共享 SQLite 时无锁错误。"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path

from agent_core.contracts import RunSnapshot, Scope
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_writes import MemoryWriter
from agent_core.persistence import SQLiteRunStore


class Embed:
    def embed(self, _text: str) -> list[float]:
        return [1.0, 0.0, 0.0]


class ConcurrentModuleWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        self.repositories = initialize_memory_repositories(
            root / "memory.db", root / "vectors", vector_size=3
        )
        self.addCleanup(self.repositories.close)
        self.run_store = SQLiteRunStore(root / "runs.db")
        self.writer = MemoryWriter(self.repositories, Embed())

    def test_concurrent_memory_writes_from_different_modules_no_lock_error(self) -> None:
        """多个模块并发写入 memory_records，WAL 模式下不应有锁错误。"""
        scopes = [
            Scope("owner", "desktop", "chat", "search"),
            Scope("owner", "desktop", "chat", "netease"),
            Scope("owner", "desktop", "chat", "companion"),
        ]

        async def write_batch(scope: Scope, batch_id: int) -> None:
            for i in range(20):
                self.writer.append_message(
                    scope, "user", f"{scope.module_id} message {batch_id}-{i}"
                )
                await asyncio.sleep(0.001)

        async def run_concurrent_writes():
            tasks = [
                write_batch(scope, batch_id)
                for batch_id in range(3)
                for scope in scopes
            ]
            await asyncio.gather(*tasks)

        asyncio.run(run_concurrent_writes())

        # 验证每个模块都写入成功
        for scope in scopes:
            records = self.repositories.recent_hot_records(scope)
            self.assertEqual(len(records), 60)  # 3 batches × 20 messages

    def test_concurrent_run_snapshots_from_different_modules_no_lock_error(self) -> None:
        """多个模块并发写入 agent_core_runs，不应有锁错误。"""
        scopes = [
            Scope("owner", "desktop", "chat", "search"),
            Scope("owner", "desktop", "chat", "netease"),
            Scope("owner", "desktop", "chat", "companion"),
        ]

        async def save_runs(scope: Scope, run_count: int) -> None:
            for i in range(run_count):
                snapshot = RunSnapshot(
                    run_id=f"{scope.module_id}-run-{i}",
                    scope=scope,
                    messages=[{"role": "user", "content": f"message {i}"}],
                    steps=i + 1,
                    tokens=(i + 1) * 100,
                    status="completed",
                )
                await asyncio.to_thread(self.run_store.save, snapshot)
                await asyncio.sleep(0.001)

        async def run_concurrent_saves():
            tasks = [save_runs(scope, 15) for scope in scopes]
            await asyncio.gather(*tasks)

        asyncio.run(run_concurrent_saves())

        # 验证每个模块都保存成功
        for scope in scopes:
            for i in range(15):
                snapshot = self.run_store.load(f"{scope.module_id}-run-{i}", scope)
                self.assertIsNotNone(snapshot)
                self.assertEqual(snapshot.scope.module_id, scope.module_id)

    def test_mixed_read_write_under_concurrent_load(self) -> None:
        """并发读写混合负载：一边写入，一边检索，验证无死锁。"""
        scope = Scope("owner", "desktop", "chat", "search")

        async def writer_task():
            for i in range(30):
                self.writer.append_message(scope, "user", f"concurrent message {i}")
                await asyncio.sleep(0.002)

        async def reader_task():
            from agent_core.memory_retrieval import MemoryRetriever
            retriever = MemoryRetriever(self.repositories, Embed())
            for i in range(20):
                await asyncio.to_thread(retriever.retrieve, scope, f"query {i}")
                await asyncio.sleep(0.003)

        async def run_mixed_load():
            await asyncio.gather(writer_task(), reader_task())

        asyncio.run(run_mixed_load())

        records = self.repositories.recent_hot_records(scope)
        self.assertEqual(len(records), 30)


if __name__ == "__main__":
    unittest.main()
