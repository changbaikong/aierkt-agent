"""会话时间戳工具。

【一条重要纪律】
"这条消息什么时候说的"属于 **created_at 字段**，只在**发给模型时**才注入；
落库和界面展示的正文必须保持干净，不含 `[消息时间: ...]` 前缀。

为什么？因为模型偶尔会把注入的时间前缀一起"复述"进回复里，
一旦这种带着前缀的文本被存回来，下一轮又会带着它发出去 —— 前缀会越滚越长。
所以任何"存/显示"的入口都先过一遍 strip_message_time_prefix 把前缀洗掉。
"""

from __future__ import annotations

import re
from typing import Any

# 匹配开头连续的若干个 `[消息时间: ...]`（用 ^ 锚定 + 分组重复，可一次吃掉多层嵌套前缀）。
_MESSAGE_TIME_PREFIX = re.compile(r"^(?:\[消息时间:\s*[^\]]*\]\s*)+")


def strip_message_time_prefix(content: Any) -> Any:
    """Remove any leading `[消息时间: ...]` prefixes from stored or displayed text."""
    # 多模态消息：content 是区块列表，只需要清洗其中 text 区块，图片等原样保留。
    if isinstance(content, list):
        return [
            {**part, "text": _MESSAGE_TIME_PREFIX.sub("", part["text"])}
            if isinstance(part, dict) and isinstance(part.get("text"), str)
            else part
            for part in content
        ]
    # 纯文本：None 也要能安全处理（调用方常直接丢表里的可空字段进来）。
    return _MESSAGE_TIME_PREFIX.sub("", str(content or ""))
