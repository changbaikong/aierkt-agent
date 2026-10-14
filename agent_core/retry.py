"""
重试装饰器 - 为函数添加自动重试能力

这个模块提供了重试装饰器，可以自动处理暂时性故障：
- 自动识别可重试的错误
- 使用指数退避策略
- 记录重试日志
- 达到最大次数后抛出原始异常
"""
from __future__ import annotations
import asyncio
import functools
import logging
from typing import Callable, TypeVar, Optional, Type

from .exceptions import is_retryable, get_retry_delay, RetryableError


logger = logging.getLogger(__name__)

T = TypeVar('T')


def retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retryable_exceptions: Optional[tuple[Type[Exception], ...]] = None,
    on_retry: Optional[Callable[[Exception, int], None]] = None
):
    """
    重试装饰器 - 支持同步和异步函数

    Args:
        max_attempts: 最大重试次数（包括首次尝试），默认3次
        base_delay: 基础延迟时间（秒），默认1秒
        max_delay: 最大延迟时间（秒），默认30秒
        retryable_exceptions: 可重试的异常类型元组，None表示使用默认判断
        on_retry: 重试回调函数，接收 (异常, 当前尝试次数) 参数

    Example:
        @retry(max_attempts=3, base_delay=1.0)
        async def call_api():
            response = await fetch_data()
            return response

        # 自定义重试回调
        def log_retry(error, attempt):
            print(f"第{attempt}次重试，错误：{error}")

        @retry(max_attempts=5, on_retry=log_retry)
        def risky_operation():
            return do_something()

    为什么需要重试？
    就像打电话，如果对方信号不好，你不会立刻放弃，而是会挂断后重拨几次。
    网络请求也一样，暂时的故障（网络抖动、服务器忙）通过重试往往能成功。
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        # 判断是同步还是异步函数
        is_async = asyncio.iscoroutinefunction(func)

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs) -> T:
            last_exception = None

            for attempt in range(1, max_attempts + 1):
                try:
                    # 执行函数
                    result = await func(*args, **kwargs)
                    return result

                except Exception as e:
                    last_exception = e

                    # 判断是否应该重试
                    should_retry = False
                    if retryable_exceptions:
                        should_retry = isinstance(e, retryable_exceptions)
                    else:
                        should_retry = is_retryable(e)

                    # 如果是最后一次尝试，或者不可重试，直接抛出
                    if attempt >= max_attempts or not should_retry:
                        logger.error(
                            f"{func.__name__} failed after {attempt} attempts: {e}",
                            extra={
                                "function": func.__name__,
                                "attempt": attempt,
                                "error_type": type(e).__name__,
                                "error_message": str(e)
                            }
                        )
                        raise

                    # 计算重试延迟
                    delay = get_retry_delay(attempt, base_delay, max_delay)

                    # 记录重试日志
                    logger.warning(
                        f"{func.__name__} failed (attempt {attempt}/{max_attempts}), "
                        f"retrying in {delay:.2f}s: {e}",
                        extra={
                            "function": func.__name__,
                            "attempt": attempt,
                            "max_attempts": max_attempts,
                            "delay_seconds": delay,
                            "error_type": type(e).__name__
                        }
                    )

                    # 调用重试回调
                    if on_retry:
                        on_retry(e, attempt)

                    # 等待后重试
                    await asyncio.sleep(delay)

            # 理论上不会到这里，但为了类型安全
            raise last_exception  # type: ignore

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs) -> T:
            """同步版本的重试装饰器"""
            import time
            last_exception = None

            for attempt in range(1, max_attempts + 1):
                try:
                    result = func(*args, **kwargs)
                    return result

                except Exception as e:
                    last_exception = e

                    should_retry = False
                    if retryable_exceptions:
                        should_retry = isinstance(e, retryable_exceptions)
                    else:
                        should_retry = is_retryable(e)

                    if attempt >= max_attempts or not should_retry:
                        logger.error(
                            f"{func.__name__} failed after {attempt} attempts: {e}",
                            extra={
                                "function": func.__name__,
                                "attempt": attempt,
                                "error_type": type(e).__name__
                            }
                        )
                        raise

                    delay = get_retry_delay(attempt, base_delay, max_delay)

                    logger.warning(
                        f"{func.__name__} failed (attempt {attempt}/{max_attempts}), "
                        f"retrying in {delay:.2f}s: {e}",
                        extra={
                            "function": func.__name__,
                            "attempt": attempt,
                            "delay_seconds": delay
                        }
                    )

                    if on_retry:
                        on_retry(e, attempt)

                    time.sleep(delay)

            raise last_exception  # type: ignore

        return async_wrapper if is_async else sync_wrapper  # type: ignore

    return decorator


def retry_with_fallback(
    fallback_value: T,
    max_attempts: int = 3,
    base_delay: float = 1.0
):
    """
    带降级值的重试装饰器 - 失败后返回默认值而不是抛出异常

    Args:
        fallback_value: 失败后返回的降级值
        max_attempts: 最大重试次数
        base_delay: 基础延迟时间

    Example:
        @retry_with_fallback(fallback_value=[], max_attempts=3)
        async def get_recommendations():
            # 获取推荐内容，失败后返回空列表
            return await fetch_recommendations()

    为什么需要降级？
    就像餐厅点菜，如果你想要的菜卖完了，服务员会推荐类似的菜，
    而不是让你饿着肚子离开。有些功能失败后，给用户一个降级体验
    比直接报错更友好。
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        is_async = asyncio.iscoroutinefunction(func)

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs) -> T:
            try:
                # 先尝试用普通重试装饰器
                retried_func = retry(max_attempts, base_delay)(func)
                return await retried_func(*args, **kwargs)
            except Exception as e:
                logger.warning(
                    f"{func.__name__} failed after retries, returning fallback value: {e}",
                    extra={
                        "function": func.__name__,
                        "fallback_value": fallback_value,
                        "error_type": type(e).__name__
                    }
                )
                return fallback_value

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs) -> T:
            try:
                retried_func = retry(max_attempts, base_delay)(func)
                return retried_func(*args, **kwargs)
            except Exception as e:
                logger.warning(
                    f"{func.__name__} failed after retries, returning fallback value: {e}",
                    extra={
                        "function": func.__name__,
                        "fallback_value": fallback_value
                    }
                )
                return fallback_value

        return async_wrapper if is_async else sync_wrapper  # type: ignore

    return decorator


class RetryStats:
    """重试统计类 - 用于监控和告警"""

    def __init__(self):
        self.total_attempts = 0
        self.total_retries = 0
        self.failures = 0
        self.successes = 0

    def record_attempt(self, attempt: int, success: bool):
        """记录一次尝试"""
        self.total_attempts += 1
        if attempt > 1:
            self.total_retries += 1
        if success:
            self.successes += 1
        else:
            self.failures += 1

    def get_retry_rate(self) -> float:
        """获取重试率"""
        if self.total_attempts == 0:
            return 0.0
        return self.total_retries / self.total_attempts

    def get_failure_rate(self) -> float:
        """获取失败率"""
        if self.total_attempts == 0:
            return 0.0
        return self.failures / self.total_attempts
