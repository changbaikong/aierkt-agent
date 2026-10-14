"""内容维护的独立边界验收：把 scripts/verify_memory_maintenance_acceptance_edges.py 当用例跑。

那六个反例都是**真实漏过**的：存储故障被当成业务拒绝、交接失败只写日志、
旧版本被分叉出第二条现行记忆、模型自拟理由就能归档长期原则、
无关历史的「同意」升级成已采纳、无关纠正词授权覆盖预算。

放进测试套件只有一个目的：这六条不许再退化。改完实现忘了重跑，
就等于把验收标准弄丢了。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_memory_maintenance_acceptance_edges.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "verify_memory_maintenance_acceptance_edges", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run_script(module):
    """跑验收脚本，并还原它对全局日志的改动（脚本自己要 disable(CRITICAL)）。"""
    previous = logging.root.manager.disable
    try:
        return asyncio.run(module.run())
    finally:
        logging.disable(previous)


def test_every_maintenance_boundary_probe_passes() -> None:
    module = _load_module()
    exit_code = _run_script(module)
    assert exit_code == 0, "内容维护边界验收脚本报未通过：退出码 %d" % exit_code


def test_the_script_reports_each_counterexample_as_json(capsys) -> None:
    """六条要一条一条看得见：编号、要求、实际结果。"""
    module = _load_module()
    _run_script(module)
    report = json.loads(capsys.readouterr().out)

    assert report["ok"] is True
    assert report["passed"] == report["total"] == 6
    assert [check["id"] for check in report["checks"]] == [
        "BM-R1",
        "BM-R2",
        "BM-R3",
        "BM-R4",
        "BM-R5",
        "BM-R6",
    ]
    assert report["live_model_calls"] == 0
    assert report["production_memory_touched"] is False
