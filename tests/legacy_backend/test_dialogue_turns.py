from __future__ import annotations

import unittest

from backend.dialogue_turns import SlidingBubbleWindow, bubble_delay_seconds, split_reply_bubbles


class DialogueTurnTests(unittest.TestCase):
    def test_explicit_lines_become_separate_bubbles(self) -> None:
        self.assertEqual(
            split_reply_bubbles("这个我可以帮你查。\n你想看意思，还是具体用法？"),
            ("这个我可以帮你查。", "你想看意思，还是具体用法？"),
        )

    def test_long_sentence_uses_soft_breaks(self) -> None:
        bubbles = split_reply_bubbles(
            "这个问题需要先检查远程连接状态，然后确认语音服务有没有休眠，最后再看事件是否已经过期。",
            max_chars=24,
        )
        self.assertGreater(len(bubbles), 1)
        self.assertTrue(all(len(bubble) <= 24 for bubble in bubbles))
        self.assertEqual("".join(bubbles), "这个问题需要先检查远程连接状态，然后确认语音服务有没有休眠，最后再看事件是否已经过期。")

    def test_bubble_count_is_bounded_without_losing_text(self) -> None:
        original = "一。二。三。四。五。六。七。八。"
        bubbles = split_reply_bubbles(original, max_bubbles=3)
        self.assertEqual(len(bubbles), 3)
        self.assertEqual("".join(bubbles), original)

    def test_window_never_exposes_more_than_two_pending_bubbles(self) -> None:
        window = SlidingBubbleWindow(("一", "二", "三", "四"), window_size=2)
        emitted: list[str] = []
        while window.has_more:
            self.assertLessEqual(window.pending_count, 2)
            emitted.append(window.pop_next() or "")
        self.assertEqual(emitted, ["一", "二", "三", "四"])

    def test_delay_is_bounded(self) -> None:
        self.assertEqual(bubble_delay_seconds("嗯"), 0.35)
        self.assertLessEqual(bubble_delay_seconds("很长" * 100), 1.2)


if __name__ == "__main__":
    unittest.main()
