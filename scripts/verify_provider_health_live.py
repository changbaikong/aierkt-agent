"""手动联调脚本：线上验证 Provider/Harness 分层健康探测与能力匹配结论。

和 `scripts/verify_harness_acp_live.py` 的区别：那个验证"任务能不能跑通"，
这个验证"Host 敢不敢把任务交出去"——分层健康检查（配置 → 沙箱探测 →
无模型 ACP 握手）加上每个 provider × task_type 的匹配结论，并核对
**报告与结论逐项一致**：

* 安装/探测/握手任何一层失败 → 结论必须是 rejected（部署失败，退出码 2）；
* 可执行 + Host 验证链齐全 → 结论必须是 matched；
* 可执行但验证链缺一半 → 结论必须是 degraded；
* 能力报告声称的验证链，必须与健康检查看到的装配状态一致（不一致是回归，退出码 1）。

无模型探测不执行 ``session/prompt``，日常检查不消耗模型额度；
发布前的真实任务验证走 `scripts/verify_harness_acp_live.py`。

用法（在仓库根目录）：

    python scripts/verify_provider_health_live.py
    python scripts/verify_provider_health_live.py --json    # 机器可读输出

退出码：0 = 结论与证据一致；1 = 报告与结论互相矛盾（回归失败码）；
2 = 安装/探测/握手失败，Harness 当前不可执行（部署失败码）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify live provider health probes and match verdicts")
    parser.add_argument("--json", action="store_true", help="输出机器可读的 JSON 报告")
    return parser.parse_args()


def health_layers(health: dict) -> dict:
    """从分层健康报告提取各层状态（ok / failed / not_run）。"""
    failure_stage = str(health.get("failure_stage") or "")

    def layer(ok: bool, stage: str) -> str:
        if ok:
            return "ok"
        return "failed" if failure_stage.startswith(stage) or failure_stage == stage else "not_run"

    chain = health.get("host_chain") or {}
    return {
        "install": "failed" if failure_stage == "config" else "ok",
        "probe": layer(bool((health.get("probe") or {}).get("ok")), "probe"),
        "handshake": layer(bool((health.get("handshake") or {}).get("ok")), "handshake"),
        "executable": "ok" if health.get("executable") else "failed",
        "host_chain": {
            "verifier": bool(chain.get("verifier")),
            "auto_apply": bool(chain.get("auto_apply")),
            "auto_apply_enabled": bool(chain.get("auto_apply_enabled")),
        },
        "failure_stage": failure_stage,
    }


def expected_verdict(health: dict, entry: dict) -> tuple[str, str]:
    """从健康检查与能力报告推出**应该**得到的结论，返回（结论, 依据）。"""
    if "executable" in health and not health.get("executable"):
        stage = health.get("failure_stage") or ""
        return "rejected", f"可执行状态为 false（失败阶段：{stage}）：{health.get('reason')}"
    if not health.get("available"):
        return "rejected", f"健康检查不可用：{health.get('reason')}"
    chain = dict(health.get("host_chain") or {})
    report_chain = bool((entry.get("capabilities") or {}).get("host_verification_chain"))
    assembled = bool(chain.get("verifier")) and bool(chain.get("auto_apply"))
    if report_chain != assembled:
        return "inconsistent", (
            f"能力报告声称验证链={report_chain}，健康检查看到的装配状态"
            f"verifier={chain.get('verifier')} auto_apply={chain.get('auto_apply')}"
        )
    if assembled:
        return "matched", "执行/取消/恢复/事件/快照/沙箱/验证链全部可证明"
    return "degraded", "Harness 可执行但 Host 验证链未装配：只能生成补丁"


async def run(args: argparse.Namespace) -> int:
    load_project_env()
    workspace = Path(tempfile.mkdtemp(prefix="provider-health-live-"))
    # allowed_roots 目录必须真实存在：快照服务装配时会校验它，不存在就会把
    # 整个 Harness provider 打成"装配失败"（fail-closed），探测就跑不起来了。
    roots = workspace / "roots"
    roots.mkdir(parents=True)
    module, host = build_work_module(
        db_path=workspace / "work.db",
        allowed_roots=[roots],
        code_agent_enabled=True,
        harness_enabled=True,
        # 与网页入口同源：这些参数决定 Host 侧验证/回写能力的真实装配。
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
    del module  # 只要 host；工具注册表与本脚本无关

    # --json 模式下 stdout 只出 JSON，人读的信息走 stderr。
    def say(text: str) -> None:
        print(text, file=sys.stderr if args.json else sys.stdout)

    harness = host.provider.provider_by_id("deepseek_harness")
    config = getattr(harness, "config", None)
    problems: list[str] = []
    if config is not None:
        problems = config.validate()
        say(f"Harness 可执行文件：{config.executable}")
        say(f"沙箱模式：{config.sandbox_mode_value}（要求版本 {config.required_version.describe()}）")

    started = time.monotonic()
    health = await host.provider_health("deepseek_harness")
    probe_seconds = time.monotonic() - started
    report = await host.capability_report()
    single = await host.match_provider(task_type="code.agent")

    exit_code = 0
    consistency = "consistent"
    deployment_failure = False
    harness_entry = next(
        (item for item in report if item["provider_id"] == "deepseek_harness" and item["task_type"] == "code.agent"),
        None,
    )
    if health is None or harness_entry is None:
        say("没有找到 deepseek_harness 的匹配结论，装配不完整。")
        consistency = "no_harness_entry"
        exit_code = 1
    else:
        layers = health_layers(health)
        chain = layers["host_chain"]
        say(
            "分层状态：install={install} probe={probe} handshake={handshake} "
            "executable={executable} 验证链 verifier={verifier} auto_apply={auto_apply}"
            "（auto_apply_enabled={auto_apply_enabled}）failure_stage={failure_stage}".format(
                install=layers["install"],
                probe=layers["probe"],
                handshake=layers["handshake"],
                executable=layers["executable"],
                verifier=chain["verifier"],
                auto_apply=chain["auto_apply"],
                auto_apply_enabled=chain["auto_apply_enabled"],
                failure_stage=layers["failure_stage"],
            )
        )
        if "executable" in health and not health.get("executable"):
            # 探测或握手失败：Harness 当前不可执行，属于部署失败。
            deployment_failure = True
            say(f"部署失败（失败阶段：{layers['failure_stage']}）：{health.get('reason')}")
        elif not health.get("available"):
            deployment_failure = True
            say(f"部署失败：{health.get('reason')}")
        want, why = expected_verdict(health, harness_entry)
        say(f"依据证据应有的结论：{want}（{why}）")
        if "inconsistent" == want:
            consistency = "report_chain_mismatch"
            exit_code = 1
        elif harness_entry["verdict"] != want or single["verdict"] != want:
            consistency = "verdict_mismatch"
            say(
                f"  不一致：能力报告={harness_entry['verdict']}，单项诊断={single['verdict']}，"
                f"证据指向={want}。"
            )
            exit_code = 1
        elif single["provider_id"] != "deepseek_harness":
            consistency = "provider_id_mismatch"
            say(f"  不一致：诊断结论里的执行者是 {single['provider_id']}，不是具体执行者。")
            exit_code = 1
        if deployment_failure and exit_code == 0:
            exit_code = 2
    if exit_code == 0:
        say("报告与结论逐项一致。")

    if args.json:
        print(
            json.dumps(
                {
                    "probe_seconds": round(probe_seconds, 2),
                    "config_problems": problems,
                    "health": health,
                    "health_layers": health_layers(health or {}),
                    "matching": report,
                    "single_match": single,
                    "consistency": consistency,
                    "exit_code": exit_code,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        say(f"健康探测耗时：{probe_seconds:.2f}s（结果进程内缓存 300s）")
        say("匹配结论：")
        for entry in report:
            checks = "；".join(
                f"{item['name']}={'过' if item['passed'] else '不过'}" for item in entry["checks"]
            )
            reasons = "；".join(entry["reasons"]) or "无"
            say(
                f"  {entry['provider_id']} × {entry['task_type']} → {entry['verdict']}"
                f"（{checks}；原因：{reasons}）"
            )
    return exit_code


def main() -> int:
    try:
        return asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
