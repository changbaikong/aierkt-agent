"""测试 MemoryRepositories 的 Qdrant server 模式支持。"""
import tempfile
import pytest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from agent_core.memory_repositories import MemoryRepositories
from agent_core.vector_config import QdrantConfig


def test_repositories_with_local_config():
    """测试使用本地模式配置初始化。"""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        vector_path = Path(tmp) / "qdrant"

        config = QdrantConfig(mode="local", path=vector_path)
        repos = MemoryRepositories(db_path, vector_config=config, vector_size=128)

        assert repos.vector_config.mode == "local"
        assert repos.vector_path == vector_path
        assert repos.vector_size == 128

        repos.close()


def test_repositories_backward_compatible():
    """测试向后兼容：使用 vector_path 参数应自动转换为本地模式。"""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        vector_path = Path(tmp) / "qdrant"

        repos = MemoryRepositories(db_path, vector_path, vector_size=128)

        assert repos.vector_config.mode == "local"
        assert repos.vector_path == vector_path

        repos.close()


@patch("agent_core.memory_repositories.QdrantClient")
def test_repositories_with_server_config(mock_client_class):
    """测试使用 server 模式配置初始化。"""
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"

        config = QdrantConfig(
            mode="server",
            url="http://localhost:6333",
            api_key="test-key",
            timeout=60.0,
        )
        repos = MemoryRepositories(db_path, vector_config=config, vector_size=256)

        # 验证配置
        assert repos.vector_config.mode == "server"
        assert repos.vector_config.url == "http://localhost:6333"
        assert repos.vector_config.api_key == "test-key"
        assert repos.vector_path is None  # server 模式无本地路径

        # 验证客户端创建参数
        mock_client_class.assert_called_once_with(
            url="http://localhost:6333",
            api_key="test-key",
            timeout=60.0,
            prefer_grpc=False,
            https=None,
        )

        repos.close()


def test_repositories_missing_both_path_and_config():
    """缺少 vector_path 和 vector_config 应该报错。"""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"

        with pytest.raises(ValueError, match="必须指定 vector_path 或 vector_config"):
            MemoryRepositories(db_path)


def test_repositories_config_priority():
    """vector_config 优先级高于 vector_path。"""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        vector_path = Path(tmp) / "qdrant_ignored"

        # 提供 server 配置，vector_path 应被忽略
        config = QdrantConfig(mode="server", url="http://localhost:6333")

        with patch("agent_core.memory_repositories.QdrantClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client

            repos = MemoryRepositories(db_path, vector_path, vector_config=config)

            assert repos.vector_config.mode == "server"
            assert repos.vector_path is None  # 忽略了 vector_path

            repos.close()


@patch("agent_core.memory_repositories.QdrantClient")
def test_server_mode_client_reuse(mock_client_class):
    """server 模式应该复用同一 URL 的客户端。"""
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client

    with tempfile.TemporaryDirectory() as tmp:
        db_path1 = Path(tmp) / "test1.db"
        db_path2 = Path(tmp) / "test2.db"

        config = QdrantConfig(mode="server", url="http://localhost:6333")

        repos1 = MemoryRepositories(db_path1, vector_config=config)
        repos2 = MemoryRepositories(db_path2, vector_config=config)

        # 只应创建一次客户端
        assert mock_client_class.call_count == 1
        assert repos1.vector_client is repos2.vector_client

        repos1.close()
        repos2.close()


@patch("agent_core.memory_repositories.QdrantClient")
def test_different_server_urls_create_different_clients(mock_client_class):
    """不同的 server URL 应该创建不同的客户端。"""
    mock_client_class.side_effect = [MagicMock(), MagicMock()]

    with tempfile.TemporaryDirectory() as tmp:
        db_path1 = Path(tmp) / "test1.db"
        db_path2 = Path(tmp) / "test2.db"

        config1 = QdrantConfig(mode="server", url="http://localhost:6333")
        config2 = QdrantConfig(mode="server", url="http://localhost:6334")

        repos1 = MemoryRepositories(db_path1, vector_config=config1)
        repos2 = MemoryRepositories(db_path2, vector_config=config2)

        # 应创建两次客户端
        assert mock_client_class.call_count == 2
        assert repos1.vector_client is not repos2.vector_client

        repos1.close()
        repos2.close()


def test_local_mode_creates_directory():
    """本地模式应该自动创建向量存储目录。"""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        vector_path = Path(tmp) / "qdrant" / "nested"

        config = QdrantConfig(mode="local", path=vector_path)
        repos = MemoryRepositories(db_path, vector_config=config)

        assert vector_path.exists()
        assert vector_path.is_dir()

        repos.close()
