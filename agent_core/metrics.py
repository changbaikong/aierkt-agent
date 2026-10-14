"""
监控指标模块 - 导出 Prometheus 兼容的指标

这个模块提供：
1. 请求计数器（按状态码、端点分类）
2. 响应时间直方图
3. 活跃连接数
4. 错误率统计
5. Token 使用量统计

为什么需要监控？
就像体检：
- 没监控：服务挂了才知道（被动）
- 有监控：提前发现异常指标（主动）
"""
from __future__ import annotations
import time
from typing import Dict, List
from dataclasses import dataclass, field
from collections import defaultdict
import threading


@dataclass
class Counter:
    """计数器 - 只增不减的指标"""
    value: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def inc(self, amount: int = 1) -> None:
        """增加计数"""
        with self._lock:
            self.value += amount

    def get(self) -> int:
        """获取当前值"""
        with self._lock:
            return self.value


@dataclass
class Gauge:
    """仪表 - 可增可减的指标"""
    value: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def set(self, value: float) -> None:
        """设置值"""
        with self._lock:
            self.value = value

    def inc(self, amount: float = 1.0) -> None:
        """增加"""
        with self._lock:
            self.value += amount

    def dec(self, amount: float = 1.0) -> None:
        """减少"""
        with self._lock:
            self.value -= amount

    def get(self) -> float:
        """获取当前值"""
        with self._lock:
            return self.value


@dataclass
class Histogram:
    """直方图 - 记录数值分布"""
    buckets: Dict[float, int] = field(default_factory=lambda: {
        0.01: 0,   # 10ms
        0.05: 0,   # 50ms
        0.1: 0,    # 100ms
        0.5: 0,    # 500ms
        1.0: 0,    # 1s
        2.5: 0,    # 2.5s
        5.0: 0,    # 5s
        10.0: 0,   # 10s
        float('inf'): 0  # 超过10s
    })
    sum: float = 0.0
    count: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def observe(self, value: float) -> None:
        """记录一个观测值"""
        with self._lock:
            self.sum += value
            self.count += 1
            for bucket_limit in sorted(self.buckets.keys()):
                if value <= bucket_limit:
                    self.buckets[bucket_limit] += 1

    def get_stats(self) -> Dict:
        """获取统计信息"""
        with self._lock:
            avg = self.sum / self.count if self.count > 0 else 0
            return {
                "count": self.count,
                "sum": self.sum,
                "avg": avg,
                "buckets": dict(self.buckets)
            }


class MetricsCollector:
    """
    指标收集器 - 收集和导出所有监控指标

    单例模式：全局只有一个实例
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, '_initialized'):
            return
        self._initialized = True

        # 请求计数器（按端点和状态分类）
        self.requests_total = defaultdict(lambda: defaultdict(Counter))

        # 响应时间直方图（按端点分类）
        self.request_duration = defaultdict(Histogram)

        # 当前活跃的请求数
        self.active_requests = Gauge()

        # Token 使用量
        self.tokens_used = Counter()

        # 重试次数
        self.retries_total = Counter()

        # 错误计数（按类型分类）
        self.errors_total = defaultdict(Counter)

    def record_request(self, endpoint: str, status: str, duration: float) -> None:
        """
        记录一次请求

        Args:
            endpoint: 端点名称（如 "/api/chat"）
            status: 状态（"success", "failed", "timeout"）
            duration: 响应时间（秒）
        """
        self.requests_total[endpoint][status].inc()
        self.request_duration[endpoint].observe(duration)

    def record_error(self, error_type: str) -> None:
        """记录一次错误"""
        self.errors_total[error_type].inc()

    def record_retry(self) -> None:
        """记录一次重试"""
        self.retries_total.inc()

    def record_tokens(self, amount: int) -> None:
        """记录 Token 使用量"""
        self.tokens_used.inc(amount)

    def get_metrics(self) -> Dict:
        """
        获取所有指标（用于 /metrics 端点）

        Returns:
            包含所有指标的字典
        """
        metrics = {
            "requests": {},
            "duration": {},
            "active_requests": self.active_requests.get(),
            "tokens_used": self.tokens_used.get(),
            "retries_total": self.retries_total.get(),
            "errors": {}
        }

        # 请求计数
        for endpoint, statuses in self.requests_total.items():
            metrics["requests"][endpoint] = {
                status: counter.get()
                for status, counter in statuses.items()
            }

        # 响应时间
        for endpoint, histogram in self.request_duration.items():
            metrics["duration"][endpoint] = histogram.get_stats()

        # 错误统计
        for error_type, counter in self.errors_total.items():
            metrics["errors"][error_type] = counter.get()

        return metrics

    def export_prometheus(self) -> str:
        """
        导出 Prometheus 格式的指标

        Returns:
            Prometheus 文本格式的指标

        Example:
            # HELP requests_total Total number of requests
            # TYPE requests_total counter
            requests_total{endpoint="/api/chat",status="success"} 42
        """
        lines = []

        # 请求总数
        lines.append("# HELP requests_total Total number of requests")
        lines.append("# TYPE requests_total counter")
        for endpoint, statuses in self.requests_total.items():
            for status, counter in statuses.items():
                lines.append(f'requests_total{{endpoint="{endpoint}",status="{status}"}} {counter.get()}')

        # 响应时间
        lines.append("# HELP request_duration_seconds Request duration in seconds")
        lines.append("# TYPE request_duration_seconds histogram")
        for endpoint, histogram in self.request_duration.items():
            stats = histogram.get_stats()
            lines.append(f'request_duration_seconds_sum{{endpoint="{endpoint}"}} {stats["sum"]}')
            lines.append(f'request_duration_seconds_count{{endpoint="{endpoint}"}} {stats["count"]}')
            for bucket_limit, bucket_count in stats["buckets"].items():
                le = "+Inf" if bucket_limit == float('inf') else str(bucket_limit)
                lines.append(f'request_duration_seconds_bucket{{endpoint="{endpoint}",le="{le}"}} {bucket_count}')

        # 活跃请求数
        lines.append("# HELP active_requests Current number of active requests")
        lines.append("# TYPE active_requests gauge")
        lines.append(f'active_requests {self.active_requests.get()}')

        # Token 使用量
        lines.append("# HELP tokens_used_total Total number of tokens used")
        lines.append("# TYPE tokens_used_total counter")
        lines.append(f'tokens_used_total {self.tokens_used.get()}')

        # 重试次数
        lines.append("# HELP retries_total Total number of retries")
        lines.append("# TYPE retries_total counter")
        lines.append(f'retries_total {self.retries_total.get()}')

        # 错误统计
        lines.append("# HELP errors_total Total number of errors by type")
        lines.append("# TYPE errors_total counter")
        for error_type, counter in self.errors_total.items():
            lines.append(f'errors_total{{type="{error_type}"}} {counter.get()}')

        return "\n".join(lines) + "\n"


# 全局单例
metrics = MetricsCollector()


def track_request(endpoint: str):
    """
    装饰器 - 自动跟踪请求的执行时间和状态

    Example:
        @track_request("/api/chat")
        async def chat_endpoint():
            return {"message": "Hello"}
    """
    def decorator(func):
        import functools
        import asyncio

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            metrics.active_requests.inc()
            start_time = time.time()
            status = "success"

            try:
                result = await func(*args, **kwargs)
                return result
            except Exception as e:
                status = "failed"
                metrics.record_error(type(e).__name__)
                raise
            finally:
                duration = time.time() - start_time
                metrics.record_request(endpoint, status, duration)
                metrics.active_requests.dec()

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            metrics.active_requests.inc()
            start_time = time.time()
            status = "success"

            try:
                result = func(*args, **kwargs)
                return result
            except Exception as e:
                status = "failed"
                metrics.record_error(type(e).__name__)
                raise
            finally:
                duration = time.time() - start_time
                metrics.record_request(endpoint, status, duration)
                metrics.active_requests.dec()

        return async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper

    return decorator
