import unittest
from datetime import datetime

from agent_core.character_state import (
    _load_pool,
    get_time_slot,
    get_current_character_state,
    _RECENT_USED,
)


class CharacterStateTests(unittest.TestCase):
    def test_pool_has_over_100_items(self):
        pool = _load_pool()
        total = sum(len(items) for items in pool.values())
        self.assertGreaterEqual(total, 100)
        self.assertIn("morning", pool)
        self.assertIn("afternoon", pool)
        self.assertIn("evening", pool)
        self.assertIn("late_night", pool)
        self.assertIn("rain", pool)

    def test_time_slots_align_with_day_boundaries(self):
        self.assertEqual(get_time_slot(datetime(2026, 10, 1, 8, 0)), "morning")
        self.assertEqual(get_time_slot(datetime(2026, 10, 1, 14, 0)), "afternoon")
        self.assertEqual(get_time_slot(datetime(2026, 10, 1, 20, 0)), "evening")
        self.assertEqual(get_time_slot(datetime(2026, 10, 1, 2, 0)), "late_night")

    def test_state_prompt_format(self):
        text = get_current_character_state(dt=datetime(2026, 10, 1, 15, 0))
        self.assertIn("[爱尔奎特当下生活状态]", text)
        self.assertIn("时段作息：午后・闲适日常", text)
        self.assertIn("手头在干嘛：", text)

    def test_rain_override_switches_to_rain_slot(self):
        text = get_current_character_state(
            dt=datetime(2026, 10, 1, 15, 0), weather_condition="小雨"
        )
        self.assertIn("时段作息：下雨天・缩在屋里", text)


if __name__ == "__main__":
    unittest.main()
