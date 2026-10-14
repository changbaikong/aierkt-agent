from __future__ import annotations

from pathlib import Path

import pytest

from evals.setup_code_agent_runtime import update_env_lines


def test_update_env_lines_enables_agent_but_keeps_auto_apply_off() -> None:
    source = "GEMINI_MODEL=demo\nCODE_AGENT_ENABLED=false\n"

    updated = update_env_lines(
        source,
        runtime_root=Path("C:/runtime"),
        powershell=Path("C:/runtime/powershell/pwsh.exe"),
        python=Path("C:/runtime/python/python.exe"),
        snapshot_root=Path("C:/state/snapshots"),
        sandbox_state_root=Path("C:/state/sandbox"),
        artifact_dir=Path("C:/state/artifacts"),
        apply_state_root=Path("C:/state/apply"),
    )

    assert updated.count("CODE_AGENT_ENABLED=true") == 1
    assert "CODE_AGENT_AUTO_APPLY_LOW_RISK=false" in updated
    assert "CODE_AGENT_RUNTIME_ROOT=C:/runtime" in updated
    assert "CODE_AGENT_POWERSHELL=C:/runtime/powershell/pwsh.exe" in updated
    assert "CODE_AGENT_PYTHON=C:/runtime/python/python.exe" in updated


def test_update_env_lines_is_idempotent() -> None:
    kwargs = {
        "runtime_root": Path("C:/runtime"),
        "powershell": Path("C:/runtime/powershell/pwsh.exe"),
        "python": Path("C:/runtime/python/python.exe"),
        "snapshot_root": Path("C:/state/snapshots"),
        "sandbox_state_root": Path("C:/state/sandbox"),
        "artifact_dir": Path("C:/state/artifacts"),
        "apply_state_root": Path("C:/state/apply"),
    }

    once = update_env_lines("GEMINI_MODEL=demo\n", **kwargs)
    twice = update_env_lines(once, **kwargs)

    assert twice == once


def test_update_env_lines_rejects_paths_outside_runtime_root() -> None:
    with pytest.raises(ValueError, match="inside runtime_root"):
        update_env_lines(
            "",
            runtime_root=Path("C:/runtime"),
            powershell=Path("D:/pwsh.exe"),
            python=Path("C:/runtime/python/python.exe"),
            snapshot_root=Path("C:/state/snapshots"),
            sandbox_state_root=Path("C:/state/sandbox"),
            artifact_dir=Path("C:/state/artifacts"),
            apply_state_root=Path("C:/state/apply"),
        )
