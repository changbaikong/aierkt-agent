"""Build and validate the persistent local code-agent runtime, then update .env.

The environment file is changed only after the copied Python/PowerShell runtime
passes the production fail-closed validator. Auto-apply remains disabled.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

from agent_core.sandbox_contracts import SandboxRuntimeConfig, validate_runtime_config


def _portable(path: Path) -> str:
    return path.resolve().as_posix()


def _inside(root: Path, path: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("runtime executable must be inside runtime_root") from exc


def update_env_lines(
    source: str,
    *,
    runtime_root: Path,
    powershell: Path,
    python: Path,
    snapshot_root: Path,
    sandbox_state_root: Path,
    artifact_dir: Path,
    apply_state_root: Path,
) -> str:
    _inside(runtime_root, powershell)
    _inside(runtime_root, python)
    updates = {
        "CODE_AGENT_ENABLED": "true",
        "CODE_AGENT_RUNTIME_ROOT": _portable(runtime_root),
        "CODE_AGENT_POWERSHELL": _portable(powershell),
        "CODE_AGENT_PYTHON": _portable(python),
        "CODE_AGENT_SNAPSHOT_ROOT": _portable(snapshot_root),
        "CODE_AGENT_SANDBOX_STATE_ROOT": _portable(sandbox_state_root),
        "CODE_AGENT_ARTIFACT_DIR": _portable(artifact_dir),
        "CODE_AGENT_AUTO_APPLY_LOW_RISK": "false",
        "CODE_AGENT_APPLY_STATE_ROOT": _portable(apply_state_root),
    }
    remaining = dict(updates)
    output: list[str] = []
    for line in source.splitlines():
        name = line.split("=", 1)[0].strip() if "=" in line else ""
        if name in remaining:
            output.append(f"{name}={remaining.pop(name)}")
        else:
            output.append(line)
    if output and output[-1] != "":
        output.append("")
    output.extend(f"{name}={value}" for name, value in remaining.items())
    return "\n".join(output).rstrip("\n") + "\n"


def _copy_runtime(destination: Path) -> SandboxRuntimeConfig:
    source_python = Path(sys.executable).resolve().parent
    source_pwsh_executable = shutil.which("pwsh")
    if not source_pwsh_executable:
        raise RuntimeError("PowerShell 7 is required")
    source_pwsh = Path(source_pwsh_executable).resolve().parent
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        python_target = staging / "python"
        powershell_target = staging / "powershell"
        python_target.mkdir()
        for item in source_python.iterdir():
            if item.is_file() and item.suffix.casefold() in {".exe", ".dll"}:
                shutil.copy2(item, python_target / item.name)
        shutil.copytree(
            source_python / "Lib",
            python_target / "Lib",
            ignore=shutil.ignore_patterns("site-packages", "__pycache__", "*.pyc"),
        )
        shutil.copytree(source_python / "DLLs", python_target / "DLLs")
        shutil.copytree(source_pwsh, powershell_target)
        candidate = SandboxRuntimeConfig(
            staging,
            powershell_target / "pwsh.exe",
            python_target / "python.exe",
        )
        validate_runtime_config(candidate)
        if destination.exists():
            existing = SandboxRuntimeConfig(
                destination,
                destination / "powershell" / "pwsh.exe",
                destination / "python" / "python.exe",
            )
            try:
                return validate_runtime_config(existing)
            except Exception:
                backup = destination.with_name(destination.name + ".invalid")
                if backup.exists():
                    shutil.rmtree(backup)
                destination.replace(backup)
        staging.replace(destination)
        return validate_runtime_config(SandboxRuntimeConfig(
            destination,
            destination / "powershell" / "pwsh.exe",
            destination / "python" / "python.exe",
        ))
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    local = Path(os.getenv("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    default_base = local / "ArcueidAgent" / "code-agent"
    parser.add_argument("--runtime-root", type=Path, default=default_base / "runtime")
    parser.add_argument("--state-root", type=Path, default=default_base / "state")
    parser.add_argument("--env-file", type=Path, default=Path(__file__).resolve().parents[1] / ".env")
    args = parser.parse_args()
    config = _copy_runtime(args.runtime_root.resolve())
    state = args.state_root.resolve()
    snapshot_root = state / "snapshots"
    sandbox_root = state / "sandbox"
    artifact_dir = state / "artifacts"
    apply_root = state / "apply"
    for path in (snapshot_root, sandbox_root, artifact_dir, apply_root):
        path.mkdir(parents=True, exist_ok=True)
    original = args.env_file.read_text(encoding="utf-8") if args.env_file.exists() else ""
    updated = update_env_lines(
        original,
        runtime_root=config.runtime_root,
        powershell=config.powershell_executable,
        python=config.python_executable,
        snapshot_root=snapshot_root,
        sandbox_state_root=sandbox_root,
        artifact_dir=artifact_dir,
        apply_state_root=apply_root,
    )
    temporary = args.env_file.with_suffix(args.env_file.suffix + ".tmp")
    temporary.write_text(updated, encoding="utf-8")
    os.replace(temporary, args.env_file)
    print({
        "runtime_root": str(config.runtime_root),
        "state_root": str(state),
        "code_agent_enabled": True,
        "auto_apply_enabled": False,
    })


if __name__ == "__main__":
    main()
