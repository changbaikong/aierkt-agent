"""双端语义对照：把 scripts/verify_memory_dual_end_semantics.py 当用例跑。

同一个包分别送进本地维护工人与云端工人的真实入口，比对：
- 正常纠正后的现行版本、归档关系、来源、范围、版本号是否一致；
- 证据不足的纠正是否两端都不改状态、也不按失败重试；
- 存储故障是否两端都进入可恢复失败并保留建议。

放进套件的目的只有一个：这类「两端各改各的、慢慢分叉」的问题不许再退化。
"""
from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_memory_dual_end_semantics.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("verify_memory_dual_end_semantics", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run_script(module):
    """跑脚本并还原它对全局日志的改动（脚本自己要 disable(CRITICAL)）。"""
    previous = logging.root.manager.disable
    try:
        return module.run()
    finally:
        logging.disable(previous)


def test_both_ends_agree_on_the_same_package(capsys) -> None:
    module = _load_module()
    exit_code = _run_script(module)
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 0, [check for check in report["checks"] if not check["passed"]]
    assert report["ok"] is True
    assert report["passed"] == report["total"]
    assert report["live_model_calls"] == 0
    assert report["production_memory_touched"] is False
    assert report["review_fixture_calls"] == {"local": 2, "cloud": 2}
    expected = {check["id"]: check for check in report["checks"] if check["id"].startswith("独立预期:")}
    assert expected["独立预期:current_contents"]["expected"] == ["项目采购预算三千"]
    assert expected["独立预期:current_versions"]["expected"] == [2]
    assert len(expected) == 7


def test_the_script_reports_each_end_side_by_side(capsys) -> None:
    """每一项都要能看见两端的实际值，不能只回一个通过。"""
    module = _load_module()
    _run_script(module)
    report = json.loads(capsys.readouterr().out)

    ids = {check["id"] for check in report["checks"]}
    assert "纠正:current_contents" in ids
    assert "纠正:archived_points_to_current" in ids
    assert "拒绝:两端都保持原状" in ids
    assert "故障:两端都进入可恢复失败" in ids
    for check in report["checks"]:
        assert "local" in check and "cloud" in check


def test_the_script_restores_the_process_environment(monkeypatch) -> None:
    """脚本设的开关是进程级的：用完必须还原，不能污染后面的用例。"""
    import os

    monkeypatch.delenv("MEMORY_SEMANTIC_ENABLED", raising=False)
    monkeypatch.delenv("MEMORY_PROCESSING_ENDPOINT", raising=False)
    module = _load_module()
    _run_script(module)

    assert "MEMORY_SEMANTIC_ENABLED" not in os.environ
    assert "MEMORY_PROCESSING_ENDPOINT" not in os.environ
