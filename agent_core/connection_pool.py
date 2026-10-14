"""
SQLite 连接池 - 防止连接资源泄漏

为什么需要连接池？
想象一个餐厅：
- 没有连接池：每个客人来了都要新建一张桌子，吃完就扔掉 → 浪费资源
- 有连接池：准备固定数量的桌子，客人来了借用，吃完归还 → 高效复用

连接池的好处：
1. 减少创建/销毁连接的开销
2. 控制最大连接数，防止资源耗尽
3. 连接复用，提高性能
"""
from __future__ import annotations
import atexit
import sqlite3
import threading
from contextlib import contextmanager
from queue import Queue, Empty, Full
from typing import Optional, Iterator
import logging
from weakref import WeakValueDictionary


logger = logging.getLogger(__name__)


class SQLiteConnectionPool:
    """
    SQLite 连接池

    SQLite 的特点：
    - 写操作是串行的（同一时间只能一个写入）
    - 读操作可以并发（多个可以同时读）
    - 每个线程需要独立的连接

    所以我们的连接池设计：
    - 限制连接数，避免资源浪费
    - 每个线程从池中借用连接，用完归还
    - 自动处理连接的健康检查
    """

    def __init__(
        self,
        database: str,
        max_connections: int = 10,
        timeout: float = 30.0,
        check_same_thread: bool = False
    ):
        """
        初始化连接池

        Args:
            database: 数据库文件路径
            max_connections: 最大连接数（默认10个，够用了）
            timeout: 获取连接的超时时间（秒）
            check_same_thread: 是否检查线程安全（False允许跨线程使用）

        为什么 max_connections 默认 10？
        SQLite 是单写多读的，写操作会锁表，太多连接反而会增加等待时间。
        10个连接对于大多数应用来说已经足够。
        """
        self.database = database
        self.max_connections = max_connections
        self.timeout = timeout
        self.check_same_thread = check_same_thread

        # 连接池队列（FIFO：先进先出）
        self._pool: Queue[sqlite3.Connection] = Queue(maxsize=max_connections)

        # 当前连接数（包括在用的和空闲的）
        self._current_size = 0
        self._lock = threading.Lock()  # 保护 _current_size 的锁

        # 统计信息
        self._created_count = 0
        self._borrowed_count = 0
        self._returned_count = 0

        logger.info(
            f"SQLite connection pool initialized: database={database}, "
            f"max_connections={max_connections}"
        )

    def _create_connection(self) -> sqlite3.Connection:
        """
        创建新的数据库连接

        Returns:
            SQLite 连接对象

        为什么设置这些参数？
        - isolation_level=None: 自动提交模式，不需要手动 commit
        - check_same_thread=False: 允许连接在不同线程中使用
        - timeout=30: 写锁超时时间，避免永久等待
        """
        conn = sqlite3.connect(
            self.database,
            check_same_thread=self.check_same_thread,
            timeout=30.0  # 数据库锁超时
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")

        # 启用 WAL 模式（Write-Ahead Logging）
        # WAL 模式的好处：读写不互斥，性能更好
        conn.execute("PRAGMA journal_mode=WAL")

        # 启用外键约束（默认 SQLite 不启用）
        conn.execute("PRAGMA foreign_keys=ON")

        self._created_count += 1
        logger.debug(f"Created new SQLite connection (total created: {self._created_count})")

        return conn

    def _validate_connection(self, conn: sqlite3.Connection) -> bool:
        """
        检查连接是否还有效

        Args:
            conn: 要检查的连接

        Returns:
            True 表示连接有效，False 表示已损坏

        为什么需要验证？
        连接可能因为超时、数据库重启等原因变得不可用，
        归还时检查一下，避免下次借用时出错。
        """
        try:
            # 执行一个简单的查询测试连接
            conn.execute("SELECT 1").fetchone()
            return True
        except Exception as e:
            logger.warning(f"Connection validation failed: {e}")
            return False

    def borrow(self) -> sqlite3.Connection:
        """
        从池中借用一个连接

        Returns:
            SQLite 连接对象

        Raises:
            TimeoutError: 超时未获取到连接

        工作流程：
        1. 先尝试从池中取一个空闲连接
        2. 如果池是空的且未达到最大连接数，创建新连接
        3. 如果已达上限，等待其他人归还连接

        就像图书馆借书：
        - 书架上有书 → 直接拿走
        - 书架空了但还能买新书 → 买一本给你
        - 书已经买够了 → 等别人还书
        """
        try:
            # 尝试从池中取一个连接（非阻塞）
            conn = self._pool.get_nowait()
            self._borrowed_count += 1
            logger.debug(f"Borrowed connection from pool (borrowed: {self._borrowed_count})")
            return conn

        except Empty:
            # 池是空的，检查是否可以创建新连接
            with self._lock:
                if self._current_size < self.max_connections:
                    # 可以创建新连接
                    self._current_size += 1
                    try:
                        conn = self._create_connection()
                        self._borrowed_count += 1
                        return conn
                    except Exception:
                        # 创建失败，回退计数
                        self._current_size -= 1
                        raise

            # 已达上限，阻塞等待连接归还
            try:
                conn = self._pool.get(timeout=self.timeout)
                self._borrowed_count += 1
                logger.debug(f"Borrowed connection after waiting")
                return conn
            except Empty:
                raise TimeoutError(
                    f"Failed to get connection from pool within {self.timeout}s "
                    f"(pool_size={self._current_size}, max={self.max_connections})"
                )

    def return_connection(self, conn: sqlite3.Connection) -> None:
        """
        归还连接到池中

        Args:
            conn: 要归还的连接

        注意：归还前会验证连接健康状态，损坏的连接会被丢弃
        """
        # 验证连接是否还有效
        if not self._validate_connection(conn):
            # 连接已损坏，丢弃它
            logger.warning("Discarding invalid connection")
            try:
                conn.close()
            except Exception:
                pass
            with self._lock:
                self._current_size -= 1
            return

        # 连接有效，归还到池中
        try:
            self._pool.put_nowait(conn)
            self._returned_count += 1
            logger.debug(f"Returned connection to pool (returned: {self._returned_count})")
        except Full:
            # 队列已满（理论上不应该发生）
            logger.warning("Connection pool is full, closing connection")
            conn.close()
            with self._lock:
                self._current_size -= 1

    @contextmanager
    def get_connection(self) -> Iterator[sqlite3.Connection]:
        """
        上下文管理器 - 自动借用和归还连接

        Example:
            pool = SQLiteConnectionPool("data.db")

            # 使用 with 语句，自动管理连接的借用和归还
            with pool.get_connection() as conn:
                conn.execute("INSERT INTO users (name) VALUES (?)", ("Alice",))
                result = conn.execute("SELECT * FROM users").fetchall()

            # 离开 with 块后，连接自动归还，即使出现异常也会归还

        为什么用 with 语句？
        就像借书，你可能忘记还书，但如果有个自动还书机制（with），
        你离开图书馆时自动还书，就不会忘记了。
        """
        conn = self.borrow()
        try:
            yield conn
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            # 无论是否异常，都归还连接
            self.return_connection(conn)

    def close_all(self) -> None:
        """
        关闭所有连接，清空连接池

        应该在程序退出时调用，确保资源释放

        Example:
            import atexit

            pool = SQLiteConnectionPool("data.db")
            atexit.register(pool.close_all)  # 程序退出时自动调用
        """
        logger.info("Closing all connections in the pool...")

        closed_count = 0
        while not self._pool.empty():
            try:
                conn = self._pool.get_nowait()
                conn.close()
                closed_count += 1
            except Empty:
                break

        with self._lock:
            self._current_size = 0

        logger.info(
            f"Connection pool closed: "
            f"closed={closed_count}, "
            f"created={self._created_count}, "
            f"borrowed={self._borrowed_count}, "
            f"returned={self._returned_count}"
        )

    def __del__(self) -> None:
        try:
            self.close_all()
        except Exception:
            pass

    def get_stats(self) -> dict:
        """
        获取连接池统计信息（用于监控）

        Returns:
            包含统计信息的字典
        """
        return {
            "database": self.database,
            "max_connections": self.max_connections,
            "current_size": self._current_size,
            "available": self._pool.qsize(),
            "in_use": self._current_size - self._pool.qsize(),
            "created_count": self._created_count,
            "borrowed_count": self._borrowed_count,
            "returned_count": self._returned_count,
        }


# 全局连接池实例（弱引用缓存，避免测试/短生命周期任务持有临时数据库文件句柄）
_global_pools: WeakValueDictionary[str, SQLiteConnectionPool] = WeakValueDictionary()
_pools_lock = threading.Lock()


def get_pool(database: str, max_connections: int = 10) -> SQLiteConnectionPool:
    """
    获取或创建指定数据库的连接池（单例模式）

    Args:
        database: 数据库文件路径
        max_connections: 最大连接数

    Returns:
        连接池实例

    Example:
        # 多次调用会返回同一个连接池实例
        pool1 = get_pool("data.db")
        pool2 = get_pool("data.db")
        assert pool1 is pool2  # True
    """
    with _pools_lock:
        pool = _global_pools.get(database)
        if pool is None:
            pool = SQLiteConnectionPool(database, max_connections)
            _global_pools[database] = pool
        return pool


def close_all_pools() -> None:
    """
    关闭所有全局连接池

    应该在程序退出时调用

    Example:
        import atexit
        atexit.register(close_all_pools)
    """
    with _pools_lock:
        for pool in list(_global_pools.values()):
            pool.close_all()
        _global_pools.clear()


atexit.register(close_all_pools)
