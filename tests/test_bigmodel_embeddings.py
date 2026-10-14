from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from agent_core_adapters.bigmodel_embeddings import BigModelEmbeddingProvider


class BigModelEmbeddingProviderTests(unittest.TestCase):
    def test_embed_calls_bigmodel_embeddings_endpoint(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3]}]})

        provider = BigModelEmbeddingProvider(
            api_key="test-key",
            model="embedding-3",
            dimensions=3,
            transport=httpx.MockTransport(handler),
        )

        self.assertEqual(provider.embed("memory text"), [0.1, 0.2, 0.3])
        self.assertEqual(str(requests[0].url), "https://open.bigmodel.cn/api/paas/v4/embeddings")
        self.assertEqual(requests[0].headers["Authorization"], "Bearer test-key")
        self.assertEqual(json.loads(requests[0].content), {"model": "embedding-3", "input": "memory text", "dimensions": 3})

    def test_from_env_loads_the_explicit_project_env_file(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        env_file = root / ".env"
        env_file.write_text("ZHIPUAI_API_KEY=from-file\nZHIPUAI_EMBEDDING_MODEL=embedding-3\n", encoding="utf-8")

        with patch.dict(os.environ, {}, clear=True):
            provider = BigModelEmbeddingProvider.from_env(env_file)

        self.assertEqual(provider.api_key, "from-file")
        self.assertEqual(provider.model, "embedding-3")
