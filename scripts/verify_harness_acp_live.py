"""手动联调脚本：用真实安装版 Harness 跑一次完整任务（提交 → 补丁 → 状态）。

和 `tests/test_deepseek_harness_live.py` 的区别：那个测协议面和 provider 本身，
这个走**完整 Host 链路** —— 真 WorkHost、真账本、真快照，和网页提交的任务同一条路。
用来回答"现在这台机器上到底能不能跑通"，以及跑不通到底卡在哪一步。

用法（在仓库根目录）：

    python scripts/verify_harness_acp_live.py --objective "把 app.py 里的 value 改成 2"
    python scripts/verify_harness_acp_live.py --project D:/some/project
    python scripts/verify_harness_acp_live.py --keep     # 保留下载的快照目录，便于排查

需要 `.env` 里有 DEEPSEEK_API_KEY（桌面版的凭证存在它自己的凭证服务里，命令行读不到）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_core.contracts import Scope  # noqa: E402
from agent_core.project_env import load_project_env  # noqa: E402
from agent_core.work_module import build_work_module  # noqa: E402

TERMINAL = {"completed", "failed", "cancelled", "interrupted", "unknown_result"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one real task through the Harness bridge")
    parser.add_argument("--project", default="", help="被修改的项目目录（默认造一个临时项目）")
    parser.add_argument(
        "--objective",
        default="把 app.py 里的 value 从 1 改成 2，只改这一行，然后简短说明你做了什么。",
    )
    parser.add_argument("--timeout", type=float, default=600.0, help="等待终态的秒数")
    parser.add_argument("--keep", action="store_true", help="保留临时项目与中间文件")
    return parser.parse_args()


def prepare_project(raw: str) -> tuple[Path, Path, bool]:
    """Return (project, allowed_root, temporary).

    The fixture lives one level **below** its allowed root on purpose: the Host's
    apply/state directories must sit outside the allowed roots, and using the
    temp directory itself as the root would put them inside it.
    """
    if raw:
        project = Path(raw).expanduser().resolve()
        if not project.is_dir():
            raise SystemExit(f"project 目录不存在：{project}")
        return project, project, False
    allowed = Path(tempfile.mkdtemp(prefix="harness-live-projects-"))
    project = allowed / "demo"
    project.mkdir(parents=True)
    (project / "app.py").write_text("value = 1\n", encoding="utf-8")
    (project / "README.md").write_text("live integration fixture\n", encoding="utf-8")
    return project, allowed, True


async def run(args: argparse.Namespace) -> int:
    load_project_env()
    project, allowed_root, temporary = prepare_project(args.project)
    workspace = Path(tempfile.mkdtemp(prefix="harness-live-state-"))
    print(f"项目目录：{project}")
    print(f"中间状态：{workspace}")

    module, host = build_work_module(
        db_path=workspace / "work.db",
        allowed_roots=[allowed_root],
        code_agent_enabled=True,
        harness_enabled=True,
        # 和网页入口同源：这些参数决定 Host 侧的验证/回写能力。
        # 漏掉它们，Harness 依然能跑出补丁，但结果只会是 "unverified"，
        # 联调时会误以为是桥接没接上验证链。
        code_runtime_root=os.getenv("CODE_AGENT_RUNTIME_ROOT", ""),
        code_powershell_executable=os.getenv("CODE_AGENT_POWERSHELL", ""),
        code_python_executable=os.getenv("CODE_AGENT_PYTHON", ""),
        code_search_executable=os.getenv("CODE_AGENT_SEARCH", ""),
        code_verification_config=os.getenv("CODE_AGENT_VERIFICATION_CONFIG", ""),
        code_agent_auto_apply_low_risk=os.getenv("CODE_AGENT_AUTO_APPLY_LOW_RISK", "").strip().lower()
        in {"1", "true", "yes", "on"},
        code_snapshot_root=workspace / "snapshots",
        code_artifact_dir=workspace / "artifacts",
        code_apply_state_root=workspace / "apply",
        code_sandbox_state_root=workspace / "sandbox",
        harness_sandbox_state_root=workspace / "harness-state",
        core_db_path=workspace / "core.db",
    )
    scope = Scope(owner_id="local-user", channel="web", conversation_id="live", module_id="work")
    started = time.monotonic()
    harness_provider = host.provider.provider_by_id("deepseek_harness")
    print(
        "Host 侧能力："
        f"verifier={type(getattr(harness_provider.lifecycle, 'verification_service', None)).__name__} "
        f"auto_apply={type(getattr(harness_provider.lifecycle, 'auto_apply_coordinator', None)).__name__}"
    )
    await host.start()
    try:
        health = await host.provider_health("deepseek_harness")
        print("Harness 体检：" + json.dumps(health, ensure_ascii=False))
        if not (health or {}).get("available"):
            print("Harness 不可用，先修配置再跑。")
            return 2

        submitted = await host.submit(
            scope,
            args.objective,
            task_type="code.agent",
            input={"project_root": str(project), "provider": "deepseek_harness"},
        )
        work_item_id = submitted["work_item_id"]
        print(f"已提交：work_item_id={work_item_id} attempt_id={submitted['attempt_id']}")

        last_line = ""
        while True:
            detail = host.get(work_item_id, scope)
            activity = detail.get("provider_activity") or {}
            line = " · ".join(
                str(item)
                for item in (
                    detail["status"],
                    activity.get("status"),
                    activity.get("step") or activity.get("tool"),
                    f"session={str(activity.get('session_id'))[:8]}",
                )
                if item
            )
            if line != last_line:
                print(f"[{time.monotonic() - started:6.1f}s] {line}")
                last_line = line
            if detail["status"] in TERMINAL:
                break
            if time.monotonic() - started > args.timeout:
                print(f"超时 {args.timeout}s，任务仍在 {detail['status']}")
                return 3
            await asyncio.sleep(1.0)

        attempt = detail["attempts"][0]
        output = attempt.get("output") or {}
        print("\n=== 结果 ===")
        print(f"任务状态：{detail['status']}（attempt {attempt['status']}，provider {attempt['provider_id']}）")
        print(f"错误：{attempt.get('error')}")
        print(f"验证：{output.get('verification_status')} 应用：{output.get('apply_status')}")
        verification = output.get("verification") or {}
        if verification.get("reason"):
            print(f"验证理由：{verification.get('reason')} {verification.get('error') or ''}".rstrip())
        print(f"改动：+{len(output.get('added') or [])} ~{len(output.get('modified') or [])} -{len(output.get('deleted') or [])}")
        patch = str(output.get("patch") or "")
        if patch:
            print("--- patch（最多 30 行）---")
            print("\n".join(patch.splitlines()[:30]))
        print("--- provider 事件 ---")
        for event in detail["events"]:
            if str(event["event_type"]).startswith("provider"):
                payload = event["payload"]
                print(
                    f"  #{event['sequence']:>3} {event['event_type']:<22} "
                    f"{payload.get('status') or payload.get('decision') or payload.get('reason') or ''}"
                )
        if temporary:
            print(f"\n临时项目内容（改动只发生在快照里，这里应保持不变）：")
            print((project / "app.py").read_text(encoding="utf-8").strip())
        terminal = detail["status"]
        return 0 if terminal == "completed" else 1
    finally:
        await host.stop()
        if not args.keep:
            shutil.rmtree(workspace, ignore_errors=True)
            if temporary:
                shutil.rmtree(project, ignore_errors=True)
        else:
            print(f"保留：{workspace}")


def main() -> int:
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
