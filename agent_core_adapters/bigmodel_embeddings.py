"""Zhipu BigModel text-embedding adapter for the memory vector index."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from agent_core.project_env import PROJECT_ENV_PATH


DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_MODEL = "embedding-3"
DEFAULT_DIMENSIONS = 2048


class BigModelEmbeddingProvider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        dimensions: int = DEFAULT_DIMENSIONS,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.dimensions = dimensions
        self.timeout = timeout
        self.transport = transport

    @classmethod
    def from_env(cls, env_path: str | Path = PROJECT_ENV_PATH) -> "BigModelEmbeddingProvider":
        load_dotenv(dotenv_path=Path(env_path), override=False)
        api_key = os.environ.get("ZHIPUAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("ZHIPUAI_API_KEY is missing from the project .env")
        return cls(
            api_key=api_key,
            model=os.environ.get("ZHIPUAI_EMBEDDING_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
            base_url=os.environ.get("ZHIPUAI_EMBEDDING_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL,
            dimensions=int(os.environ.get("ZHIPUAI_EMBEDDING_DIMENSIONS", str(DEFAULT_DIMENSIONS))),
        )

    def embed(self, text: str) -> list[float]:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload: dict[str, Any] = {"model": self.model, "input": text, "dimensions": self.dimensions}
        with httpx.Client(timeout=self.timeout, trust_env=False, transport=self.transport) as client:
            response = client.post(f"{self.base_url}/embeddings", headers=headers, json=payload)
        if response.status_code >= 400:
            raise RuntimeError(f"BigModel embedding request failed with HTTP {response.status_code}")
        try:
            vector = response.json()["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RuntimeError("BigModel embedding response has no vector") from exc
        if not isinstance(vector, list) or not all(isinstance(value, (int, float)) for value in vector):
            raise RuntimeError("BigModel embedding response contains an invalid vector")
        if len(vector) != self.dimensions:
            raise RuntimeError(f"BigModel embedding dimension mismatch: expected {self.dimensions}, got {len(vector)}")
        return [float(value) for value in vector]
