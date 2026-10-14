import asyncio
import threading

import pytest

from agent_core.contracts import Scope
from agent_core.local_worker_provider import LocalWorkerProvider
from agent_core.work_provider import WorkRequest


def test_local_provider_lists_files_without_shell(tmp_path):
    (tmp_path / "b.txt").write_text("b")
    (tmp_path / "a.txt").write_text("a")

    async def scenario():
        provider = LocalWorkerProvider(allowed_roots=[tmp_path])
        events = []
        async def emit(event):
            events.append(event)
        result = await provider.execute(
            WorkRequest("w", "a", Scope("u", "web", "c", "work"), "按名称列出文件", {"directory": str(tmp_path)}),
            emit,
        )
        assert result.status == "completed"
        assert [item["name"] for item in result.output["items"]] == ["a.txt", "b.txt"]
        assert events[0].event_type == "progress"

    asyncio.run(scenario())


def test_local_provider_returns_approval_preview_for_sorting(tmp_path):
    (tmp_path / "a.txt").write_text("a")

    async def scenario():
        provider = LocalWorkerProvider(allowed_roots=[tmp_path])
        result = await provider.execute(
            WorkRequest("w", "a", Scope("u", "web", "c", "work"), "按类型整理文件", {"directory": str(tmp_path)}),
            lambda event: asyncio.sleep(0),
        )
        assert result.status == "waiting_approval"
        assert result.output["moves"][0]["target"].endswith("txt\\a.txt") or result.output["moves"][0]["target"].endswith("txt/a.txt")

    asyncio.run(scenario())


def test_local_provider_dispatches_explicit_task_types_and_uses_injected_backends(tmp_path):
    (tmp_path / "b.txt").write_text("b")
    captured_threads = []

    def capture():
        captured_threads.append(threading.current_thread().name)
        return b"\x89PNG\r\n\x1a\nfake"

    def adjust(percent):
        captured_threads.append(threading.current_thread().name)
        return {"status": "ok", "after_percent": percent}

    async def scenario():
        provider = LocalWorkerProvider(
            allowed_roots=[tmp_path],
            screenshot_capture=capture,
            volume_adjust=adjust,
            artifact_dir=tmp_path / "artifacts",
        )
        emit = lambda event: asyncio.sleep(0)
        listed = await provider.execute(
            WorkRequest(
                "w",
                "list-attempt",
                Scope("u", "web", "c", "work"),
                task_type="file.list",
                objective="任意目标",
                input={"directory": str(tmp_path)},
            ),
            emit,
        )
        assert listed.status == "completed"

        screenshot = await provider.execute(
            WorkRequest(
                "w",
                "screen-attempt",
                Scope("u", "web", "c", "work"),
                task_type="screen.capture",
                objective="截图",
                input={},
            ),
            emit,
        )
        assert screenshot.status == "completed"
        assert screenshot.artifacts[0].media_type == "image/png"
        assert (tmp_path / "artifacts" / screenshot.artifacts[0].artifact_id).exists()

        volume = await provider.execute(
            WorkRequest(
                "w",
                "volume-attempt",
                Scope("u", "web", "c", "work"),
                task_type="system.volume.adjust",
                objective="调高音量",
                input={"percent": 5},
            ),
            emit,
        )
        assert volume.status == "completed"
        assert volume.output["after_percent"] == 5
        assert all(name != threading.current_thread().name for name in captured_threads)

    asyncio.run(scenario())


def test_local_provider_rejects_unknown_task_type_without_keyword_routing(tmp_path):
    async def scenario():
        provider = LocalWorkerProvider(allowed_roots=[tmp_path])
        result = await provider.execute(
            WorkRequest(
                "w",
                "a",
                Scope("u", "web", "c", "work"),
                task_type="unknown.task",
                objective="按类型整理文件",
                input={"directory": str(tmp_path)},
            ),
            lambda event: asyncio.sleep(0),
        )
        assert result.status == "failed"
        assert "unsupported task_type" in (result.error or "")

    asyncio.run(scenario())


def test_stale_move_manifest_fails_the_task_and_moves_nothing(tmp_path):
    """预览之后源文件被动过 → 执行阶段必须报 failed，而不是当成成功。

    这条钉的是"业务状态词不许被当成成功"：`FileWorkService.apply_moves` 在
    源文件消失或变了的时候返回 ``{"status": "stale", ...}``。执行改走网关之后，
    字典里带 ``status`` 这件事会被网关当成一个正式的返回值来读 ——
    如果那个词既不在状态映射表里、也不在业务状态词表里，它就会被判成非法状态；
    而如果它被当成普通字典，'stale' 就静默变成了成功。**两种都错，
    正确的落点是：任务失败，一个文件都不许动。**
    """
    source = tmp_path / "a.txt"
    source.write_text("a")

    async def scenario():
        provider = LocalWorkerProvider(allowed_roots=[tmp_path])
        preview = await provider.execute(
            WorkRequest("w", "preview-attempt", Scope("u", "web", "c", "work"), task_type="file.organize",
                        objective="整理文件", input={"directory": str(tmp_path)}),
            lambda event: asyncio.sleep(0),
        )
        assert preview.status == "waiting_approval", preview.status
        source.unlink()
        applied = await provider.execute(
            WorkRequest("w", "apply-attempt", Scope("u", "web", "c", "work"), task_type="file.organize",
                        objective="整理文件", input={"directory": str(tmp_path)},
                        approval_manifest=tuple(preview.output["moves"]), approval_granted=True),
            lambda event: asyncio.sleep(0),
        )
        assert applied.status == "failed", f"stale 被当成了成功：{applied.status}"
        assert not (tmp_path / "txt" / "a.txt").exists()

    asyncio.run(scenario())


def test_unavailable_volume_backend_fails_the_task(tmp_path):
    """音量后端自己回报 unavailable → 任务失败，并把后端给的原因带出来。"""
    def adjust(_percent):
        return {"status": "unavailable", "reason": "no audio device"}

    async def scenario():
        provider = LocalWorkerProvider(allowed_roots=[tmp_path], volume_adjust=adjust)
        result = await provider.execute(
            WorkRequest("w", "v", Scope("u", "web", "c", "work"), task_type="system.volume.adjust",
                        objective="调音量", input={"percent": 10}),
            lambda event: asyncio.sleep(0),
        )
        assert result.status == "failed", f"unavailable 被当成了成功：{result.status}"
        assert "no audio device" in (result.error or "")

    asyncio.run(scenario())


def test_local_provider_rejects_missing_volume_percent_instead_of_muting(tmp_path):
    """缺少音量数值不能静默解释成 0，否则旧入口会意外把整机静音。"""
    calls = []

    def adjust(percent):
        calls.append(percent)
        return {"status": "ok", "after_percent": percent}

    async def scenario():
        provider = LocalWorkerProvider(allowed_roots=[tmp_path], volume_adjust=adjust)
        result = await provider.execute(
            WorkRequest(
                "w",
                "v-missing",
                Scope("u", "web", "c", "work"),
                task_type="system.volume.adjust",
                objective="调一下音量",
                input={},
            ),
            lambda event: asyncio.sleep(0),
        )
        assert result.status == "failed"
        assert "input.percent" in (result.error or "")
        assert calls == []

    asyncio.run(scenario())


def test_local_provider_does_not_accept_approved_moves_from_request(tmp_path):
    with pytest.raises(ValueError, match="approved_moves"):
        WorkRequest(
            "w",
            "a",
            Scope("u", "web", "c", "work"),
            task_type="file.organize",
            objective="整理文件",
            input={"directory": str(tmp_path), "approved_moves": []},
        )
