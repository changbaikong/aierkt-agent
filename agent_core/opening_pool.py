"""预备普通问候：模型或接口变慢时仍能稳定开口。"""

from __future__ import annotations

import json
from pathlib import Path


GREETING_POOL: tuple[str, ...] = tuple(
    json.loads(Path(__file__).with_name("opening_pool.json").read_text(encoding="utf-8"))
)


def prepared_greeting(visit_id: str) -> str:
    """同一次进入始终选同一句，避免超时响应与重复请求相互覆盖。"""
    value = 0
    for character in visit_id:
        value = (value * 31 + ord(character)) & 0xFFFFFFFF
    return GREETING_POOL[value % len(GREETING_POOL)]
