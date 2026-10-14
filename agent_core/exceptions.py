"""
异常分类系统 - 区分可重试错误和永久性错误

这个模块定义了 agent_core 中所有的异常类型，帮助系统判断：
- 哪些错误可以重试（网络抖动、API限流）
- 哪些错误是永久性的（逻辑错误、权限错误）
- 哪些错误需要特殊处理（资源耗尽、超时）
"""
from __future__ import annotations
from typing import Optional


class AgentCoreError(Exception):
    """Agent Core 基础异常类 - 所有自定义异常的父类"""

    def __init__(self, message: str, details: Optional[dict] = None):
        """
        初始化异常

        Args:
            message: 错误描述信息
            details: 额外的上下文信息（如 run_id, scope 等）
        """
        super().__init__(message)
        self.message = message
        self.details = details or {}


# ============= 可重试错误 =============

class RetryableError(AgentCoreError):
    """可重试错误基类 - 暂时性故障，重试可能成功"""
    pass


class NetworkError(RetryableError):
    """网络错误 - 连接超时、DNS解析失败等"""
    pass


class RateLimitError(RetryableError):
    """API限流错误 - 请求频率过高，需要退避重试"""

    def __init__(self, message: str, retry_after: Optional[int] = None, details: Optional[dict] = None):
        """
        Args:
            retry_after: 建议的重试等待时间（秒）
        """
        super().__init__(message, details)
        self.retry_after = retry_after


class ResourceTemporarilyUnavailableError(RetryableError):
    """资源暂时不可用 - 数据库锁、队列满等"""
    pass


class EmbeddingServiceError(RetryableError):
    """向量嵌入服务错误 - 嵌入生成失败，可重试"""
    pass


# ============= 永久性错误 =============

class PermanentError(AgentCoreError):
    """永久性错误基类 - 重试无意义，需要人工介入或修改逻辑"""
    pass


class ValidationError(PermanentError):
    """输入验证错误 - 参数格式不正确、长度超限等"""
    pass


class AuthorizationError(PermanentError):
    """权限错误 - 用户无权执行此操作"""
    pass


class ResourceNotFoundError(PermanentError):
    """资源不存在 - 对话ID、工具不存在等"""
    pass


class ConfigurationError(PermanentError):
    """配置错误 - 环境变量缺失、配置项不合法"""
    pass


class DataIntegrityError(PermanentError):
    """数据完整性错误 - 数据库约束冲突、数据损坏"""
    pass


# ============= 特殊处理错误 =============

class TimeoutError(AgentCoreError):
    """超时错误 - 操作执行时间过长"""

    def __init__(self, message: str, timeout_seconds: float, details: Optional[dict] = None):
        super().__init__(message, details)
        self.timeout_seconds = timeout_seconds


class ResourceExhaustedError(AgentCoreError):
    """资源耗尽错误 - Token预算用完、内存不足、队列满"""
    pass


class ConversationLockedError(AgentCoreError):
    """对话已锁定错误 - 其他请求正在处理此对话"""

    def __init__(self, message: str, scope: Optional[str] = None, details: Optional[dict] = None):
        super().__init__(message, details)
        self.scope = scope


# ============= 工具函数 =============

def is_retryable(exception: Exception) -> bool:
    """
    判断异常是否可重试

    Args:
        exception: 捕获的异常对象

    Returns:
        True 表示可以重试，False 表示不应该重试

    Example:
        try:
            result = call_api()
        except Exception as e:
            if is_retryable(e):
                # 重试逻辑
                pass
            else:
                # 直接失败
                raise
    """
    # 权限错误必须放在最前面排除：它是 OSError 的子类，
    # 若落到下面的 OSError 规则里会被误判成"可重试"，
    # 而权限问题重试多少次都不会变，得由人来授权。
    if isinstance(exception, PermissionError):
        return False

    # 自定义的可重试错误
    if isinstance(exception, RetryableError):
        return True

    # 常见的可重试标准库异常
    import_errors = (
        ConnectionError,
        TimeoutError,  # 标准库的 TimeoutError
        OSError,
    )

    return isinstance(exception, import_errors)


def get_retry_delay(attempt: int, base_delay: float = 1.0, max_delay: float = 30.0) -> float:
    """
    计算指数退避重试延迟

    Args:
        attempt: 当前重试次数（从1开始）
        base_delay: 基础延迟时间（秒）
        max_delay: 最大延迟时间（秒）

    Returns:
        建议的等待时间（秒）

    Example:
        第1次重试: 1秒
        第2次重试: 2秒
        第3次重试: 4秒
        第4次重试: 8秒
        ...

    为什么使用指数退避？
    就像排队买奶茶，如果店员说"等一下"，你不会每秒都问一次"好了吗"，
    而是第一次等1分钟，第二次等2分钟，第三次等4分钟...
    这样既给对方时间处理，也不会浪费自己的时间。
    """
    import random

    # 指数退避：2^(attempt-1) * base_delay
    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)

    # 加入随机抖动（jitter），避免多个请求同时重试导致"惊群效应"
    # 就像大家都设定闹钟在整点醒来，如果都在同一时刻重试，服务器又会崩溃
    jitter = random.uniform(0, delay * 0.1)  # 抖动范围为延迟的10%

    return delay + jitter
