from __future__ import annotations

import unittest

from backend.search import _parse_rss_results


class SearchTests(unittest.TestCase):
    def test_rss_results_are_parsed_and_html_is_removed(self) -> None:
        payload = b"""<?xml version="1.0" encoding="UTF-8"?>
        <rss><channel><item>
          <title>Example &amp; News</title>
          <description>&lt;b&gt;Useful&lt;/b&gt; summary</description>
          <link>https://example.com/story</link>
        </item></channel></rss>"""

        result = _parse_rss_results(payload)

        self.assertIn("Example & News", result)
        self.assertIn("Useful summary", result)
        self.assertIn("https://example.com/story", result)
        self.assertNotIn("<b>", result)


if __name__ == "__main__":
    unittest.main()
