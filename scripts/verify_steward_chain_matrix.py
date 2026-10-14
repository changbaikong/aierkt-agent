r"""打印"长链路 / 短链路 × 普通搜索 / 代码代理"四格对照表。

用法（项目根目录）：
    venv\Scripts\python.exe scripts/verify_steward_chain_matrix.py

全部离线：可控时钟 + 假工具 / 假事件流，不联网、不调模型、不写用户聊天库。
断言在 tests/test_steward_chain_matrix.py；这个脚本只负责把观测结果排成人能看的表。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_steward_chain_matrix import format_matrix, run_matrix  # noqa: E402


async def main() -> int:
    results = await run_matrix(Path(tempfile.mkdtemp()))
    print("管家阶段呈现：长链路 vs 短链路 / 普通搜索 vs 代码代理")
    print(format_matrix(results))
    print(
        "\n规则：普通搜索首次等待满 1.5 秒才说、相邻至少 4 秒、一轮最多一条首次 + 两条中段；"
        "代码任务四类阶段各一次、不套聊天上限；两者都是终态回执之前先关进度。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
