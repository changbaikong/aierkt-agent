"""矩阵测试公共 conftest：把 tests/ 加到 sys.path，让矩阵测试能 import _common.fake_registry。

pytest 默认 prepend 模式只把 test 文件所在目录加到 sys.path，
跨目录 import（tests/matrices/ 想 import tests/_common/）需要这里补一刀。
"""

from __future__ import annotations

import sys
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))
