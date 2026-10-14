"""原项目边界守卫：任务跑在快照里，原项目只能由 Host 的审批链改动。

实测背景（2026-10-06，真模型 + 真桌面 Harness 0.2.0-rc.2）：任务文本里带了项目
绝对路径时，模型会直接读写那个原项目。**沙箱本身是生效的** —— `dsh-fs-sandbox`
挂载为 `ctx.fs`，`workspace-write` 的可写根是"工作目录 + 平台临时目录"；当时那次
越界之所以成功，是因为 fixture 恰好放在 `%LOCALAPPDATA%\\Temp` 下，属于规则允许的
可写区。真正不到位的是 Host：它只检查自己有没有回写，就宣称"原项目未改动"。

所以这里的守卫与沙箱职责不同，是 Host 自己的核对：任务结束时原项目必须仍然等于
快照基线，否则判失败并记录。项目在临时目录之外时，沙箱与这道守卫互为补位；
项目在临时目录里时，这道守卫就是唯一能发现改动的一层。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.code_task_contracts import CodeAgentOutcome
from agent_core.code_task_lifecycle import CodeTaskLifecycle
from agent_core.contracts import Scope
from agent_core.deepseek_harness_provider import DeepSeekHarnessProvider
from agent_core.work_provider import WorkRequest
from agent_core.workspace_snapshot import WorkspaceSnapshotService


def make_fixture(tmp_path: Path) -> tuple[WorkspaceSnapshotService, Path, Path]:
    allowed = tmp_path / "projects"
    project = allowed / "demo"
    project.mkdir(parents=True)
    (project / "app.py").write_text("value = 1\n", encoding="utf-8")
    (project / "notes.txt").write_text("keep\n", encoding="utf-8")
    return WorkspaceSnapshotService(tmp_path / "snapshots", [allowed]), project, allowed


def outcome() -> CodeAgentOutcome:
    return CodeAgentOutcome("completed", summary="done", output={"steps_used": 1})


def request_for(project: Path) -> WorkRequest:
    return WorkRequest(
        work_item_id="work-1",
        attempt_id="attempt-1",
        scope=Scope(owner_id="alice", channel="web", conversation_id="chat"),
        objective="改一下",
        task_type="code.agent",
        input={"project_root": str(project)},
        provider_id="deepseek_harness",
    )


def lifecycle_for(tmp_path: Path, snapshots: WorkspaceSnapshotService) -> CodeTaskLifecycle:
    return CodeTaskLifecycle(snapshot_service=snapshots, artifact_dir=tmp_path / "artifacts")


def test_project_changes_detects_content_edits_and_ignores_caches(tmp_path: Path) -> None:
    snapshots, project, _ = make_fixture(tmp_path)
    snapshot = snapshots.create("attempt-1", project)

    assert snapshot.project_changes() == ()

    (project / "app.py").write_text("value = 2\n", encoding="utf-8")
    (project / "new_file.py").write_text("x = 1\n", encoding="utf-8")
    (project / "__pycache__").mkdir()
    (project / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"\x00\x01")

    assert snapshot.project_changes() == ("app.py", "new_file.py")


@pytest.mark.asyncio
async def test_external_project_edit_fails_the_task_with_a_violation_event(tmp_path: Path) -> None:
    snapshots, project, _ = make_fixture(tmp_path)
    snapshot = snapshots.create("attempt-1", project)
    lifecycle = lifecycle_for(tmp_path, snapshots)
    events = []

    # 模型在快照里改了（正常），同时又去改了原项目（越界）。
    (snapshot.workspace_root / "app.py").write_text("value = 2\n", encoding="utf-8")
    (project / "app.py").write_text("value = 2\n", encoding="utf-8")

    result = await lifecycle.result_from_outcome(
        request_for(project), snapshot, outcome(), lambda event: events.append(event)
    )

    assert result.status == "failed"
    assert "原项目在任务快照之外被修改" in (result.error or "")
    assert result.output["scope_violation"]["files"] == ["app.py"]
    violations = [event for event in events if event.event_type == "provider_scope_violation"]
    assert violations and violations[0].payload["status"] == "modified"
    # 越界时不再往下走验证/回写：没有 patch 产物，也没有 apply 结果。
    assert "apply_status" not in result.output
    assert result.artifacts == []


@pytest.mark.asyncio
async def test_clean_run_records_intact_scope(tmp_path: Path) -> None:
    snapshots, project, _ = make_fixture(tmp_path)
    snapshot = snapshots.create("attempt-1", project)
    lifecycle = lifecycle_for(tmp_path, snapshots)
    (snapshot.workspace_root / "app.py").write_text("value = 2\n", encoding="utf-8")

    result = await lifecycle.result_from_outcome(
        request_for(project), snapshot, outcome(), lambda event: None
    )

    assert result.status == "completed"
    assert result.output["project_scope"]["status"] == "intact"
    assert "value = 2" in result.output["patch"]


@pytest.mark.asyncio
async def test_unreadable_project_is_reported_as_unverifiable(tmp_path: Path) -> None:
    snapshots, project, _ = make_fixture(tmp_path)
    snapshot = snapshots.create("attempt-1", project)
    lifecycle = lifecycle_for(tmp_path, snapshots)
    # 原项目在任务期间消失：扫不动，但任务本身照常完成，只是不再声称"没被改过"。
    (project / "app.py").unlink()
    (project / "notes.txt").unlink()
    project.rmdir()

    result = await lifecycle.result_from_outcome(
        request_for(project), snapshot, outcome(), lambda event: None
    )

    assert result.status in {"completed", "failed"}
    assert result.output.get("project_scope", {}).get("status") in {"unverifiable", "modified"}


def test_harness_prompt_states_the_workspace_boundary() -> None:
    """提示词必须把边界写死：模型是照着任务文本里的绝对路径去改原项目的。"""
    provider = DeepSeekHarnessProvider.__new__(DeepSeekHarnessProvider)
    run = type(
        "Run",
        (),
        {"objective": "修复 C:/some/project 里失败的测试"},
    )()

    prompt = DeepSeekHarnessProvider._prompt_text(provider, run)
    resume = DeepSeekHarnessProvider._resume_prompt_text(provider, run)

    for text in (prompt, resume):
        assert "只能" in text and "当前工作目录" in text
        assert "绝对路径" in text
        assert "C:/some/project" in text  # 任务目标仍然完整保留
