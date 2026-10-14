import unittest
from unittest.mock import AsyncMock, MagicMock

from backend.sticker_collector import _audit_image


class StickerCollectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_audit_requires_a_finished_sticker_matching_the_query(self) -> None:
        llm = MagicMock()
        llm.judge_image = AsyncMock(
            return_value='{"decision":"reject","confidence":0.99,"label":"普通照片","reason":"不是贴纸"}'
        )

        result = await _audit_image(
            llm,
            b"test-image",
            "image/jpeg",
            "Murasame Senren Banka chibi reaction sticker",
        )

        prompt = llm.judge_image.await_args.args[1]
        self.assertEqual("reject", result["decision"])
        self.assertIn("Search target: Murasame Senren Banka chibi reaction sticker", prompt)
        self.assertIn("Hard reject all unedited real photographs of people or animals", prompt)
        self.assertIn("A cat or person merely looking expressive is NOT a sticker", prompt)

