"""向量载荷回填必须扛得住「记录还在、向量点已经不在」的记录。

这不是假想场景：云端库上就有这样的行（点被删过或那次 upsert 失败）。批量更新
会因为其中一个不存在的点整体报 404，异常抛穿 initialize()，服务直接起不来 ——
而真实原因只是一条派生数据过期。2026-10-11 部署到云端时就是这么崩的。
"""
from __future__ import annotations

from scripts.memory_test_support import seed_semantic
from agent_core.contracts import Scope
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_turn import MemoryCandidate
from agent_core.memory_writes import MemoryWriter


class _Embeddings:
    def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class _ServerLikeVectorClient:
    """按**云端 qdrant** 的口径模拟「点不存在」。

    本地 qdrant 对不存在的点**静默通过**，云端（远程）会回 404 "No point with id …
    found"。那次云端启动失败只在远程模式下发生，所以这里必须按远程口径造替身 ——
    直接拿本地库复现，会得到一个「测试通过、线上照崩」的假绿。
    """

    def __init__(self, inner, missing_ids) -> None:
        self._inner = inner
        self._missing = set(missing_ids)

    @staticmethod
    def _points(operations) -> list:
        found = []
        for operation in operations:
            payload = getattr(operation, "set_payload", None)
            found.extend(list(getattr(payload, "points", None) or []))
        return found

    def batch_update_points(self, collection, operations):
        hit = [point for point in self._points(operations) if point in self._missing]
        if hit:
            raise RuntimeError(f"404 Not Found: No point with id {hit[0]} found")
        return self._inner.batch_update_points(collection, operations)

    def set_payload(self, collection_name, payload, points, **kwargs):
        hit = [point for point in points if point in self._missing]
        if hit:
            raise RuntimeError(f"No point with id {hit[0]} found")
        return self._inner.set_payload(
            collection_name=collection_name, payload=payload, points=points, **kwargs
        )

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_backfill_survives_a_record_whose_vector_point_is_gone(tmp_path) -> None:
    repos = initialize_memory_repositories(
        tmp_path / "records.db", tmp_path / "vectors", vector_size=3
    )
    try:
        writer = MemoryWriter(repos, _Embeddings())
        scope = Scope("user", "web", "c1", "companion")
        record = seed_semantic(writer,
            scope,
            [
                MemoryCandidate(
                    content="向量点会不见的那条",
                    kind="fact",
                    content_status="reported",
                    source_types=("user_statement",),
                    source_refs=("turn-1",),
                )
            ],
            turn_id="turn-1",
        )[0]

        with repos.connect() as db:
            db.execute(
                "UPDATE memory_records SET vector_payload_version=0, embedding_status='ready' WHERE id=?",
                (record["id"],),
            )
        repos.vector_client = _ServerLikeVectorClient(repos.vector_client, [record["id"]])

        repos._backfill_vector_payloads()  # 不许抛

        with repos.connect() as db:
            row = dict(
                db.execute(
                    "SELECT embedding_status, vector_payload_version FROM memory_records WHERE id=?",
                    (record["id"],),
                ).fetchone()
            )
        # 点不在了 → 标回待重建，由补偿工人补；而不是标成「载荷已是最新」。
        assert row["embedding_status"] == "pending"
        assert int(row["vector_payload_version"]) == 0
    finally:
        repos.close()


def test_backfill_still_refreshes_records_whose_points_exist(tmp_path) -> None:
    repos = initialize_memory_repositories(
        tmp_path / "records.db", tmp_path / "vectors", vector_size=3
    )
    try:
        writer = MemoryWriter(repos, _Embeddings())
        scope = Scope("user", "web", "c1", "companion")
        record = seed_semantic(writer,
            scope,
            [
                MemoryCandidate(
                    content="点子还在的那条",
                    kind="fact",
                    content_status="reported",
                    source_types=("user_statement",),
                    source_refs=("turn-1",),
                )
            ],
            turn_id="turn-1",
        )[0]
        with repos.connect() as db:
            db.execute(
                "UPDATE memory_records SET vector_payload_version=0 WHERE id=?", (record["id"],)
            )

        repos._backfill_vector_payloads()

        with repos.connect() as db:
            version = db.execute(
                "SELECT vector_payload_version FROM memory_records WHERE id=?", (record["id"],)
            ).fetchone()[0]
        assert int(version) > 0, "点还在的记录要照常补载荷，不能因为怕出错就整体跳过"
    finally:
        repos.close()
