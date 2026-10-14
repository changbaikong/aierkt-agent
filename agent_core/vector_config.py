"""Qdrant 向量数据库配置，支持本地模式和 server 模式。"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

@dataclass
class QdrantConfig:
    """Qdrant 客户端配置。

    本地模式：
        QdrantConfig(mode="local", path="./qdrant_storage")

    Server 模式：
        QdrantConfig(
            mode="server",
            url="http://localhost:6333",
            api_key="your-api-key",  # 可选
            timeout=30.0
        )
    """
    mode: Literal["local", "server"] = "local"

    # 本地模式参数
    path: str | Path | None = None

    # Server 模式参数
    url: str | None = None
    api_key: str | None = None
    timeout: float = 30.0

    # 通用参数
    prefer_grpc: bool = False
    https: bool | None = None

    def __post_init__(self) -> None:
        if self.mode == "local":
            if self.path is None:
                raise ValueError("本地模式必须指定 path")
            self.path = Path(self.path)
        elif self.mode == "server":
            if self.url is None:
                raise ValueError("server 模式必须指定 url")
        else:
            raise ValueError(f"不支持的 mode: {self.mode}")

    @classmethod
    def from_env(cls, prefix: str = "QDRANT") -> QdrantConfig:
        """从环境变量读取配置。

        环境变量：
            QDRANT_MODE=local|server
            QDRANT_PATH=./qdrant_storage (local 模式)
            QDRANT_URL=http://localhost:6333 (server 模式)
            QDRANT_API_KEY=secret (server 模式，可选)
            QDRANT_TIMEOUT=30.0
        """
        import os
        mode = os.getenv(f"{prefix}_MODE", "local")

        if mode == "local":
            path = os.getenv(f"{prefix}_PATH")
            if not path:
                raise ValueError(f"本地模式需要设置 {prefix}_PATH 环境变量")
            return cls(mode="local", path=path)

        elif mode == "server":
            url = os.getenv(f"{prefix}_URL")
            if not url:
                raise ValueError(f"server 模式需要设置 {prefix}_URL 环境变量")
            return cls(
                mode="server",
                url=url,
                api_key=os.getenv(f"{prefix}_API_KEY"),
                timeout=float(os.getenv(f"{prefix}_TIMEOUT", "30.0")),
            )

        else:
            raise ValueError(f"不支持的 {prefix}_MODE: {mode}")
