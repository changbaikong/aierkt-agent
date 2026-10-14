"""File-based operational logging for the memory subsystem.

记忆日志不再刷到进程控制台（stderr），统一写入 ``data/memory.log``，
由 monitor_console.py 只读展示。聊天入口（chat_cli）保持干净，运行详情在监控台看。
"""
from __future__ import annotations

import logging
from pathlib import Path


LOGGER_NAME = "agent_core.memory"
DEFAULT_MEMORY_LOG = Path("data") / "memory.log"


def configure_memory_logging(level: int = logging.INFO, path: Path | str | None = None) -> logging.Logger:
    """给 ``agent_core.memory`` logger 挂一个文件 handler（幂等：只挂一次）。

    - 默认写 ``data/memory.log``；目录不存在会自动创建
    - 文件用 UTF-8 编码，中文不乱码
    - ``propagate=False``：日志不进 root、不往 stderr 冒泡
    - ``MemoryWriter`` / ``MemoryRetriever`` 等对象构造时自动调用，日志始终就绪
    """
    log_path = Path(path) if path is not None else DEFAULT_MEMORY_LOG
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    if not any(getattr(handler, "_memory_file", False) for handler in logger.handlers):
        handler = logging.FileHandler(log_path, encoding="utf-8", errors="replace")
        handler._memory_file = True  # type: ignore[attr-defined]
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger