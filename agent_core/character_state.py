"""爱尔奎特日常生活状态（Character State）管理器。

从 character_state_pool.json 加载 100+ 条真祖同居生活引子，
根据系统当前时间（早晨/下午/傍晚/深夜）以及天气状态智能抽取，
并内置最近 15 次防重冷却队列，确保几天内都不重复。
零模型额外开销，为对话提供鲜活的生活切片锚点。
"""

from __future__ import annotations

import collections
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("agent_core.character_state")

POOL_PATH = Path(__file__).with_name("character_state_pool.json")

# 内存级防重复冷却队列（记录最近用过的引子摘要，最长保存 20 条）
_RECENT_USED: collections.deque[str] = collections.deque(maxlen=20)


def _load_pool() -> dict[str, list[str]]:
    try:
        if POOL_PATH.exists():
            return json.loads(POOL_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        LOGGER.warning("failed to load character_state_pool.json: %s", exc)
    return {
        "morning": ["抱着抱枕在沙发上发呆，刚睡醒打着小哈欠。"],
        "afternoon": ["缩在沙发角落看电视里吵吵闹闹的电视剧，手里抓着零食。"],
        "evening": ["趴在窗台上看外面的路灯一盏盏亮起来，有点嘴馋。"],
        "late_night": ["夜深了精神反而更好了，坐在窗台边吹晚风看夜空。"],
        "rain": ["外面下着大雨，正整个人裹在厚毯子里听窗玻璃上的雨声。"],
    }


def get_time_slot(dt: datetime) -> str:
    """根据时间返回作息分类：morning, afternoon, evening, late_night。"""
    hour = dt.hour
    if 6 <= hour < 11:
        return "morning"
    if 11 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 23:
        return "evening"
    return "late_night"


def get_current_character_state(
    *,
    dt: datetime | None = None,
    weather_condition: str = "",
) -> str:
    """获取当前爱尔奎特的生活状态提示词段落。"""
    if dt is None:
        dt = datetime.now()

    pool = _load_pool()

    # 1. 判断是否走雨天专属池
    if weather_condition and any(w in weather_condition for w in ("雨", "雪", "暴雨", "雷阵雨")):
        slot_key = "rain"
    else:
        slot_key = get_time_slot(dt)

    candidates = pool.get(slot_key) or pool.get("afternoon") or []
    if not candidates:
        return ""

    # 2. 防重挑选：在未冷却的候选中挑选，若全被冷却则降级允许
    available = [c for c in candidates if c not in _RECENT_USED]
    if not available:
        available = candidates

    # 使用基于 (日期, 2小时窗口) 的哈希或者伪随机，同一2小时内保持平稳
    slot_id = (dt.year * 10000 + dt.month * 100 + dt.day) * 100 + (dt.hour // 2)
    chosen_idx = slot_id % len(available)
    chosen = available[chosen_idx]

    # 记入防重队列
    _RECENT_USED.append(chosen)

    slot_names = {
        "morning": "清晨・刚醒不久",
        "afternoon": "午后・闲适日常",
        "evening": "傍晚・等门嘴馋",
        "late_night": "深夜・夜行猫性",
        "rain": "下雨天・缩在屋里",
    }
    phase_label = slot_names.get(slot_key, "日常生活")

    return (
        f"[爱尔奎特当下生活状态]\n"
        f"时段作息：{phase_label}\n"
        f"手头在干嘛：{chosen}\n"
        f"（提示：这是你此时此刻在屋里的生活切片；你可以根据它自然流露自己的动作与小心情，想到什么随口就说，不要死板背诵。）"
    )
