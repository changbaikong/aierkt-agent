"""测试 Qdrant 配置模块。"""
import os
import pytest
from pathlib import Path
from agent_core.vector_config import QdrantConfig


def test_local_mode_config():
    """测试本地模式配置。"""
    config = QdrantConfig(mode="local", path="./qdrant_storage")
    assert config.mode == "local"
    assert config.path == Path("./qdrant_storage")


def test_local_mode_missing_path():
    """本地模式缺少 path 应该报错。"""
    with pytest.raises(ValueError, match="本地模式必须指定 path"):
        QdrantConfig(mode="local")


def test_server_mode_config():
    """测试 server 模式配置。"""
    config = QdrantConfig(
        mode="server",
        url="http://localhost:6333",
        api_key="test-key",
        timeout=60.0,
    )
    assert config.mode == "server"
    assert config.url == "http://localhost:6333"
    assert config.api_key == "test-key"
    assert config.timeout == 60.0


def test_server_mode_missing_url():
    """server 模式缺少 url 应该报错。"""
    with pytest.raises(ValueError, match="server 模式必须指定 url"):
        QdrantConfig(mode="server")


def test_unsupported_mode():
    """不支持的 mode 应该报错。"""
    with pytest.raises(ValueError, match="不支持的 mode"):
        QdrantConfig(mode="invalid")  # type: ignore


def test_from_env_local_mode(monkeypatch):
    """测试从环境变量读取本地模式配置。"""
    monkeypatch.setenv("QDRANT_MODE", "local")
    monkeypatch.setenv("QDRANT_PATH", "./test_storage")

    config = QdrantConfig.from_env()
    assert config.mode == "local"
    assert config.path == Path("./test_storage")


def test_from_env_local_mode_missing_path(monkeypatch):
    """本地模式缺少 QDRANT_PATH 应该报错。"""
    monkeypatch.setenv("QDRANT_MODE", "local")
    monkeypatch.delenv("QDRANT_PATH", raising=False)

    with pytest.raises(ValueError, match="本地模式需要设置 QDRANT_PATH 环境变量"):
        QdrantConfig.from_env()


def test_from_env_server_mode(monkeypatch):
    """测试从环境变量读取 server 模式配置。"""
    monkeypatch.setenv("QDRANT_MODE", "server")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("QDRANT_API_KEY", "secret")
    monkeypatch.setenv("QDRANT_TIMEOUT", "45.0")

    config = QdrantConfig.from_env()
    assert config.mode == "server"
    assert config.url == "http://localhost:6333"
    assert config.api_key == "secret"
    assert config.timeout == 45.0


def test_from_env_server_mode_missing_url(monkeypatch):
    """server 模式缺少 QDRANT_URL 应该报错。"""
    monkeypatch.setenv("QDRANT_MODE", "server")
    monkeypatch.delenv("QDRANT_URL", raising=False)

    with pytest.raises(ValueError, match="server 模式需要设置 QDRANT_URL 环境变量"):
        QdrantConfig.from_env()


def test_from_env_custom_prefix(monkeypatch):
    """测试自定义环境变量前缀。"""
    monkeypatch.setenv("CUSTOM_MODE", "local")
    monkeypatch.setenv("CUSTOM_PATH", "./custom_storage")

    config = QdrantConfig.from_env(prefix="CUSTOM")
    assert config.mode == "local"
    assert config.path == Path("./custom_storage")


def test_from_env_default_to_local(monkeypatch):
    """未设置 QDRANT_MODE 时默认为 local。"""
    monkeypatch.delenv("QDRANT_MODE", raising=False)
    monkeypatch.setenv("QDRANT_PATH", "./default_storage")

    config = QdrantConfig.from_env()
    assert config.mode == "local"
    assert config.path == Path("./default_storage")


def test_server_mode_optional_api_key():
    """server 模式的 api_key 应该是可选的。"""
    config = QdrantConfig(mode="server", url="http://localhost:6333")
    assert config.mode == "server"
    assert config.url == "http://localhost:6333"
    assert config.api_key is None


def test_from_env_server_mode_optional_api_key(monkeypatch):
    """从环境变量读取 server 配置时 api_key 应该是可选的。"""
    monkeypatch.setenv("QDRANT_MODE", "server")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.delenv("QDRANT_API_KEY", raising=False)

    config = QdrantConfig.from_env()
    assert config.mode == "server"
    assert config.url == "http://localhost:6333"
    assert config.api_key is None
