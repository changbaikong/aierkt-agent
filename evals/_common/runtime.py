"""运行时工具：固定时间注入 + 唯一临时目录工厂。

fixed_runtime_time 从 evals/web_workflow_matrix.py 抽出，保证多轮 case 的时间语义可复现。
new_basetemp 给统一 runner 和 pytest 用，每次指向全新空目录，规避 Windows 上
pytest safe-delete 阈值（LESSONS_LEARNED：累积垃圾撞 50 会 SystemExit）。
"""

from __future__ import annotations

import tempfile
from pathlib import Path


def fixed_runtime_time() -> str:
    """固定运行时本机时间，注入 system prompt 让相对时间表达可复现。"""
    return (
        "[运行时本机时间]\n"
        "当前本机时间：2030-01-01T20:00:00+08:00\n"
        "时区：Asia/Shanghai\n"
        "来源：local\n"
        "仅用该时间理解相对时间表达，例如今晚、明天和半小时后。"
        "每轮以此处最新值为准；创建任务时交由工具校验并转换为绝对时间。"
    )


def new_basetemp(prefix: str = "matrix-pytest-") -> str:
    """返回一个全新临时目录路径（已创建，调用方负责清理或交给 pytest 管理）。"""
    return tempfile.mkdtemp(prefix=prefix)


def new_temp_sqlite(prefix: str = "matrix-sqlite-") -> str:
    """返回一个全新临时目录路径，专给 per-case SQLite 用。"""
    return tempfile.mkdtemp(prefix=prefix)
