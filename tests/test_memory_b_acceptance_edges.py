"""B 计划边界验收：把 scripts/verify_memory_b_acceptance_edges.py 当用例跑。

那个脚本是**独立的**验收探针：用临时库、临时向量集合和合成消息，逐条反证
11 个曾经真实漏掉的边界（伪造来源、提炼失败被当成没内容、跨账号串记录、
双端重复处理、预算读不到引用字段、同回合不同正文被静默丢弃、长记忆尾部丢失、
任务价值排序被旧预算顺序覆盖、关掉召回后关联旁路、可信项目绑定没落到保存、
网页同时往两端入队）。

它是「先写失败样例」的那份样例。放进测试套件的目的只有一个：这 11 条不许
再退化 —— 改完实现忘了重跑，就等于把验收标准弄丢了。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_memory_b_acceptance_edges.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("verify_memory_b_acceptance_edges", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run_script(module):
    """跑验收脚本，并把它对全局日志的改动还原。

    脚本自己要 ``logging.disable(CRITICAL)``（独立探针不该被日志淹没），但那是
    **进程级**开关：在同一个 pytest 进程里跑完不还原，后面所有依赖日志的用例
    都会「一条日志都收不到」。这里只还原状态，不改脚本的任何判定。
    """
    previous = logging.root.manager.disable
    try:
        return asyncio.run(module.run())
    finally:
        logging.disable(previous)


def test_every_boundary_probe_passes() -> None:
    module = _load_module()
    exit_code = _run_script(module)
    assert exit_code == 0, "B 边界验收脚本报未通过：退出码 %d" % exit_code


def test_the_script_still_reports_its_own_result_as_json(capsys) -> None:
    """脚本必须把「跑了哪几条、结果是什么」打进 stdout。

    只回一个退出码的验收脚本没法复核 —— 看不出它到底跑了几条、哪条没过。
    """
    module = _load_module()
    _run_script(module)
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["passed"] == report["total"] == 11
