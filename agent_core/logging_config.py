"""
日志配置模块 - 结构化日志设置

这个模块提供统一的日志配置，包括：
1. 结构化日志（JSON 格式，方便机器解析）
2. 日志级别配置（通过环境变量）
3. 日志输出目标配置（控制台 + 文件）
4. 关键字段自动添加（timestamp, level, module）

为什么需要结构化日志？
就像记账：
- 普通日志："今天花了100块钱买东西"（难以统计）
- 结构化日志：{"date": "2024-01-01", "amount": 100, "category": "shopping"}（方便查询统计）
"""
import logging
import sys
import os
from datetime import datetime
from pathlib import Path
from typing import Any


class StructuredFormatter(logging.Formatter):
    """
    结构化日志格式化器

    将日志输出为结构化的格式，包含：
    - timestamp: 时间戳
    - level: 日志级别
    - logger: 日志器名称
    - message: 日志消息
    - extra: 额外字段（如 run_id, scope 等）
    """

    def format(self, record: logging.LogRecord) -> str:
        """
        格式化日志记录

        Args:
            record: 日志记录对象

        Returns:
            格式化后的字符串
        """
        # 基础字段
        log_data = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # 添加额外字段（通过 logger.info(..., extra={...}) 传入）
        for key, value in record.__dict__.items():
            if key not in [
                "name", "msg", "args", "created", "filename", "funcName",
                "levelname", "levelno", "lineno", "module", "msecs",
                "message", "pathname", "process", "processName",
                "relativeCreated", "thread", "threadName", "stack_info",
                "exc_info", "exc_text"
            ]:
                log_data[key] = value

        # 添加文件位置信息（调试用）
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        log_data["location"] = f"{record.filename}:{record.lineno}"

        # 格式化为紧凑的 JSON（一行）
        import json
        return json.dumps(log_data, ensure_ascii=False, default=str)


class SimpleFormatter(logging.Formatter):
    """
    简单格式化器 - 适合人类阅读

    格式：[时间] [级别] [模块] 消息
    """

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
        return f"[{timestamp}] [{record.levelname}] [{record.name}] {record.getMessage()}"


def configure_logging(
    level: str = "INFO",
    use_json: bool = False,
    log_file: str | None = None
) -> None:
    """
    配置全局日志系统

    Args:
        level: 日志级别（DEBUG, INFO, WARNING, ERROR, CRITICAL）
        use_json: 是否使用 JSON 格式（结构化日志）
        log_file: 日志文件路径（None 表示不写文件）

    Example:
        # 开发环境：简单格式，INFO 级别
        configure_logging(level="INFO", use_json=False)

        # 生产环境：JSON 格式，输出到文件
        configure_logging(level="WARNING", use_json=True, log_file="agent.log")

    为什么区分开发和生产？
    - 开发环境：人类阅读日志，用简单格式
    - 生产环境：机器解析日志，用 JSON 格式（方便 ELK/Splunk 等工具）
    """
    # 解析日志级别
    level = level.upper()
    numeric_level = getattr(logging, level, logging.INFO)

    # 根模块日志器
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # 清空已有的 handlers
    root_logger.handlers.clear()

    # 选择格式化器
    formatter = StructuredFormatter() if use_json else SimpleFormatter()

    # 添加控制台输出
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # 添加文件输出（如果指定）
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    # 设置第三方库的日志级别（避免噪音）
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("qdrant_client").setLevel(logging.WARNING)

    root_logger.info(
        f"Logging configured: level={level}, json={use_json}, file={log_file}",
        extra={"log_config": {"level": level, "json": use_json, "file": log_file}}
    )


def configure_from_env() -> None:
    """
    从环境变量配置日志

    环境变量：
    - LOG_LEVEL: 日志级别（默认 INFO）
    - LOG_FORMAT: 日志格式，"json" 或 "simple"（默认 simple）
    - LOG_FILE: 日志文件路径（默认不写文件）

    Example:
        # 设置环境变量
        export LOG_LEVEL=DEBUG
        export LOG_FORMAT=json
        export LOG_FILE=logs/agent.log

        # 在程序启动时调用
        configure_from_env()
    """
    level = os.getenv("LOG_LEVEL", "INFO")
    use_json = os.getenv("LOG_FORMAT", "simple").lower() == "json"
    log_file = os.getenv("LOG_FILE")

    configure_logging(level=level, use_json=use_json, log_file=log_file)


# 默认配置（可以在模块导入时自动执行）
if __name__ != "__main__":
    # 只在作为模块导入时自动配置，避免在测试时重复配置
    configure_from_env()
