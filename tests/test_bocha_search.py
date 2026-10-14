"""BoCha 搜索适配器的离线测试（httpx.MockTransport，不发真实请求）。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from agent_tools.web_search import DEFAULT_BASE_URL, BoChaWebSearchProvider

ENDPOINT = f"{DEFAULT_BASE_URL}/web-search"


def _json_response(status_code: int, payload: dict):
    return lambda request: httpx.Response(status_code, json=payload)


def _provider(handler, **kwargs) -> BoChaWebSearchProvider:
    transport = httpx.MockTransport(handler)
    return BoChaWebSearchProvider(api_key="test-key", transport=transport, **kwargs)


class BoChaEndpointTests(unittest.TestCase):
    """端点和认证头的验证。"""

    def test_posts_to_web_search_endpoint_with_bearer(self):
        seen: dict = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json={"code": 200, "data": {"web_results": []}})

        _provider(handler).search("hello")
        self.assertEqual(seen["url"], ENDPOINT)
        self.assertEqual(seen["auth"], "Bearer test-key")

    def test_payload_maps_max_results_to_count(self):
        seen: dict = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"code": 200, "data": {"web_results": []}})

        _provider(handler).search("hello", max_results=3, freshness="oneWeek")
        self.assertEqual(seen["body"]["query"], "hello")
        self.assertEqual(seen["body"]["count"], 3)
        self.assertEqual(seen["body"]["freshness"], "oneWeek")


class BoChaParsingTests(unittest.TestCase):
    """响应解析与防御式取值。"""

    def test_parses_results_with_primary_and_alternate_keys(self):
        payload = {
            "code": 200,
            "data": {
                "web_results": [
                    {"name": "A", "url": "https://a.com", "snippet": "s1", "summary": "sum1"},
                    {"title": "B", "link": "https://b.com", "content": "s2"},
                    {"name": "no-url", "url": "", "snippet": "x"},  # 无 url → 丢弃
                    {"name": "C", "url": "https://c.com"},
                ]
            },
        }
        results = _provider(_json_response(200, payload)).search("q", max_results=10)
        self.assertEqual([r.title for r in results], ["A", "B", "C"])
        self.assertEqual(results[0].url, "https://a.com")
        self.assertEqual(results[0].summary, "sum1")
        self.assertEqual(results[1].snippet, "s2")

    def test_parses_webpages_value_structure(self):
        """真实 Web Search 响应：结果在 data.webPages.value 而非 data.web_results。"""
        payload = {
            "code": 200,
            "data": {
                "_type": "SearchResponse",
                "webPages": {
                    "totalEstimatedMatches": 122845,
                    "value": [
                        {"name": "R1", "url": "https://a.com", "snippet": "s1", "summary": "sum1"},
                        {"title": "R2", "link": "https://b.com", "content": "s2"},
                    ],
                },
            },
        }
        results = _provider(_json_response(200, payload)).search("q")
        self.assertEqual([r.title for r in results], ["R1", "R2"])
        self.assertEqual(results[0].summary, "sum1")
        self.assertEqual(results[1].snippet, "s2")

    def test_results_are_limited_to_max_results(self):
        items = [{"name": f"R{i}", "url": f"https://r{i}.com"} for i in range(5)]
        payload = {"code": 200, "data": {"web_results": items}}
        results = _provider(_json_response(200, payload)).search("q", max_results=3)
        self.assertEqual(len(results), 3)

    def test_missing_web_results_returns_empty(self):
        results = _provider(_json_response(200, {"code": 200, "data": {}})).search("q")
        self.assertEqual(results, [])


class BoChaErrorTests(unittest.TestCase):
    """失败路径统一抛 RuntimeError，由 handler 接住转 failed。"""

    def test_non_200_code_raises(self):
        payload = {"code": 500, "message": "boom"}
        with self.assertRaisesRegex(RuntimeError, "code=500"):
            _provider(_json_response(200, payload)).search("q")

    def test_http_error_raises(self):
        with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
            _provider(_json_response(500, {})).search("q")


class BoChaFromEnvTests(unittest.TestCase):
    """from_env 的配置读取与可选依赖降级。"""

    def _write_env(self, directory: str, content: str) -> Path:
        env_path = Path(directory) / ".env"
        env_path.write_text(content, encoding="utf-8")
        return env_path

    def test_from_env_reads_temp_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = self._write_env(
                tmp, "BOCHA_API_KEY=env-key\nBOCHA_BASE_URL=https://example.test/v1\n"
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                provider = BoChaWebSearchProvider.from_env(env_path)
        self.assertIsNotNone(provider)
        assert provider is not None
        self.assertEqual(provider.api_key, "env-key")
        self.assertEqual(provider.base_url, "https://example.test/v1")

    def test_from_env_missing_key_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = self._write_env(tmp, "OTHER=1\n")
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertIsNone(BoChaWebSearchProvider.from_env(env_path))

    def test_from_env_defaults_when_base_url_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = self._write_env(tmp, "BOCHA_API_KEY=env-key\n")
            with mock.patch.dict(os.environ, {}, clear=True):
                provider = BoChaWebSearchProvider.from_env(env_path)
        assert provider is not None
        self.assertEqual(provider.base_url, DEFAULT_BASE_URL)

    def test_from_env_reads_summary_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = self._write_env(tmp, "BOCHA_API_KEY=env-key\nBOCHA_SUMMARY=false\n")
            with mock.patch.dict(os.environ, {}, clear=True):
                provider = BoChaWebSearchProvider.from_env(env_path)
        assert provider is not None
        self.assertFalse(provider.summary)


if __name__ == "__main__":
    unittest.main()
