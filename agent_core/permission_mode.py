"""账号级权限模式：标准 / 完全访问。

这个模块回答一个问题：**这次动作要不要停下来等人点头。**
它不回答"这个工具危不危险"（那是 ``ToolSpec.risk`` 的事），也不回答
"这个动作做不做得成"（那是 ``AGENT_WORK_ROOTS`` 和 provider 的事）。

三个概念必须分开，否则会互相污染：

- ``configured_mode`` —— 用户选了什么。诚实记录，写进数据库。
- ``effective_mode`` —— 本机环境允许它生效到什么程度。**唯一的降级点**。
  非回环监听时一律降到 standard，但不动 configured —— 用户换回回环监听后
  不需要重新点一次。
- ``ToolSpec.risk`` —— 工具本身的危险等级。**完全访问一个字都不改它。**
  改它等于"切回标准模式后恢复不了"，因为恢复依赖的就是这组标记。

为什么是账号级而不是会话级：模式描述的是"我信任这台机器上的这个账号"，
不是"我信任这次对话"。所以同一 owner 的所有会话（含新建的）一起生效，
策略读取发生在**每次授权时**，不做进程内缓存 —— 缓存一层就会让
"切换后要重启才生效"这种问题重新长出来。

为什么 ``FULL_ACCESS_EXCLUSIONS`` 是排除表而不是"更危险的工具"：
它挡的是**流程短路**，不是危险。``work__plan_submit`` 提交的计划会派生出
新的需要审批的步骤 —— 自动放行它，等于让审批层自己批自己。

（``work__approve`` 曾经也在这个表里。它 2026-09-22 从模型工具面整个删掉了：
让模型替用户批准高风险操作，等于让两道闸门互相抵消。审批只能由用户走
统一审批控制面，所以这个名字不再需要被"排除"——它根本不存在。）
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

from .connection_pool import get_pool
from .contracts import Scope, ToolSpec
from .tools import DefaultRiskPolicy, RiskPolicy

MODE_STANDARD = "standard"
MODE_FULL_ACCESS = "full_access"
PERMISSION_MODES = frozenset({MODE_STANDARD, MODE_FULL_ACCESS})

#: 完全访问**也**不放行的工具。挡的是流程自我短路，不是危险等级。
#: 表里的工具仍然正常注册、正常可调用 —— 只是任何模式下都先问一句。
#: （2026-09-22 起只剩 `work__plan_submit`：`work__approve` 已从模型工具面
#: 整个删除，一个不存在的工具不需要被"排除"。原因见本文件模块头的说明。）
FULL_ACCESS_EXCLUSIONS: frozenset[str] = frozenset({"work__plan_submit"})

#: 完全访问只服务本机账号。匿名 Scope 没有可追责的主体，
#: 放行它等于把这个开关变成"任何匿名访客都能用"。
LOCAL_OWNER_ID = "local-user"

#: 允许启用完全访问的监听地址。写死而不是"检测是不是内网"：
#: 内网也能被同事和端口转发摸到，"本机"只能是回环。
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})

#: 不可用原因用**稳定错误码**而不是散文：
#: 码可以被前端翻译、被测试断言；散文改一个字就破一批测试，
#: 而且没法往前端传（前端要做中英/措辞调整时只能改后端）。
REASON_NON_LOOPBACK = "non_loopback_host"
REASON_READY = ""

_REASON_TEXT = {
    REASON_NON_LOOPBACK: "Web 服务监听地址不是本机回环，完全访问无法启用（当前监听：{host}）",
}


def is_loopback_host(host: str | None) -> bool:
    """监听地址是不是回环。空字符串按"不是"处理 —— 不知道就当作不安全。"""
    return str(host or "").strip().lower() in LOOPBACK_HOSTS


def _normalize_owner(owner_id: Any) -> str:
    return str(owner_id or "").strip()


def _is_anonymous(owner_id: str) -> bool:
    return owner_id.startswith("anonymous:")


class AccountPermissionStore:
    """``agent_core_account_permissions`` 的读写。按 owner_id 一行。

    表建在 ``agent_core.db`` 里，和确认记录、运行快照同一个库 ——
    Web 与 CLI 共享同一份权限，不需要两边各自配置。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._ensure_table()

    def _ensure_table(self) -> None:
        with get_pool(self.path).get_connection() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS agent_core_account_permissions (
                    owner_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )

    def mode_for(self, owner_id: str) -> str:
        """读某个账号的模式。没记录 / 记录坏了都返回 standard。

        返回 standard 而不是抛异常：权限查询在任何一条授权路径上都会走，
        这里抛出去会把一次工具调用变成一次 500。不认识 = 不给权限。
        """
        owner = _normalize_owner(owner_id)
        if not owner:
            return MODE_STANDARD
        with get_pool(self.path).get_connection() as db:
            row = db.execute(
                "SELECT mode FROM agent_core_account_permissions WHERE owner_id=?",
                (owner,),
            ).fetchone()
        if row is None:
            return MODE_STANDARD
        mode = str(row["mode"] or "")
        return mode if mode in PERMISSION_MODES else MODE_STANDARD

    def set_mode(self, owner_id: str, mode: str) -> str:
        if mode not in PERMISSION_MODES:
            raise ValueError(f"unknown permission mode: {mode}")
        owner = _normalize_owner(owner_id)
        if not owner:
            raise ValueError("owner_id is required")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with get_pool(self.path).get_connection() as db:
            db.execute(
                "INSERT INTO agent_core_account_permissions (owner_id, mode, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(owner_id) DO UPDATE SET mode=excluded.mode, updated_at=excluded.updated_at",
                (owner, mode, now),
            )
        return mode

    def updated_at_for(self, owner_id: str) -> str:
        owner = _normalize_owner(owner_id)
        with get_pool(self.path).get_connection() as db:
            row = db.execute(
                "SELECT updated_at FROM agent_core_account_permissions WHERE owner_id=?",
                (owner,),
            ).fetchone()
        return str(row["updated_at"]) if row else ""


class AccountPermissionController:
    """把"用户选了什么"和"这台机器允许它生效到什么程度"合起来。

    唯一做决定的地方是 :meth:`snapshot` —— API 层、策略层、Host 层都调它，
    不各自实现一遍判断（各写一遍就会各漏一条）。
    """

    def __init__(
        self,
        store: AccountPermissionStore,
        *,
        web_host: str | None = None,
        clock: Any = None,
    ) -> None:
        self.store = store
        self.web_host = web_host
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def snapshot(self, owner_id: str) -> dict[str, Any]:
        owner = _normalize_owner(owner_id)
        configured = self.store.mode_for(owner)
        loopback = is_loopback_host(self.web_host)

        if configured == MODE_FULL_ACCESS and not loopback:
            return {
                "owner_id": owner,
                "configured_mode": configured,
                "effective_mode": MODE_STANDARD,
                "available": False,
                "reason": REASON_NON_LOOPBACK,
                "reason_text": _REASON_TEXT[REASON_NON_LOOPBACK].format(host=self.web_host or "(unknown)"),
                "exclusions": sorted(FULL_ACCESS_EXCLUSIONS),
            }
        return {
            "owner_id": owner,
            "configured_mode": configured,
            "effective_mode": configured,
            "available": True,
            "reason": REASON_READY,
            "reason_text": "",
            "exclusions": sorted(FULL_ACCESS_EXCLUSIONS),
        }

    def effective_mode(self, owner_id: str) -> str:
        """运行时唯一该用的模式读法。"""
        return str(self.snapshot(owner_id)["effective_mode"])

    def set_mode(self, owner_id: str, mode: str) -> dict[str, Any]:
        """写模式并返回**实际**结果。

        非回环时照样写库（诚实记录用户的选择），但返回的 effective 是 standard
        并带原因 —— 让前端能明确说"配置已保存，但当前监听地址下不会生效"，
        而不是假装成功、等用户回聊天页发现确认框还在。
        """
        if mode not in PERMISSION_MODES:
            raise ValueError(f"unknown permission mode: {mode}")
        self.store.set_mode(owner_id, mode)
        return self.snapshot(owner_id)


class PermissionLookup(Protocol):
    """策略与 Host 只依赖这一个方法，不依赖 Controller 的具体形状。"""

    def effective_mode(self, owner_id: str) -> str: ...


class LookupFromStore:
    """把裸 ``AccountPermissionStore`` 包成一个 lookup。

    用途只有一个：**标准模式**下的策略不需要关心监听地址 —— 非完全访问时
    effective 恒等于 configured，降级判断不可能改变结果。所以给一个 store
    就能当 lookup 用，不必为了构造策略而先造控制器。
    """

    def __init__(self, store: AccountPermissionStore) -> None:
        self.store = store

    def effective_mode(self, owner_id: str) -> str:
        return self.store.mode_for(owner_id)


def _as_lookup(source: Any) -> PermissionLookup:
    """容错地接受 Controller / Store / Lookup 三种输入。

    为什么要容错：调用点分得很散（Web、CLI、子代理、测试），
    强制每个调用点都先造 Controller 会让"我只想验一下策略"变成
    "我得先知道 web_host 是什么"。而这三者语义完全一致，
    多一层包装不会掩盖任何真实差异 —— 真正该显式传的是 web_host，
    那发生在 Controller 那一层。
    """
    if isinstance(source, AccountPermissionStore):
        return LookupFromStore(source)
    if hasattr(source, "effective_mode"):
        return source
    raise TypeError("policy requires an AccountPermissionController or AccountPermissionStore")


class AccountAwareRiskPolicy:
    """闸门一：按账号决定聊天侧工具要不要停下来确认。

    分支只有三条，顺序有意义：

    1. 排除表里的工具 → **原样交回** ``DefaultRiskPolicy``（任何模式都不放行）；
    2. 非本机账号（匿名 / 别人） → 交回 ``DefaultRiskPolicy``；
    3. 本机账号 + 完全访问 → ``allow``。

    其余一律回落到默认策略。这样"放宽"永远只是**在默认策略之上加一条 allow**，
    不可能因为新增工具或改动注册而意外变宽。
    """

    def __init__(
        self,
        source: Any,
        *,
        fallback: RiskPolicy | None = None,
        local_owner_id: str = LOCAL_OWNER_ID,
    ) -> None:
        self.controller = _as_lookup(source)
        self.fallback = fallback or DefaultRiskPolicy()
        self.local_owner_id = local_owner_id

    def _is_local_account(self, scope: Scope) -> bool:
        owner = _normalize_owner(getattr(scope, "owner_id", ""))
        return owner == self.local_owner_id and not _is_anonymous(owner)

    def authorize(self, spec: ToolSpec, scope: Scope, arguments: dict[str, Any]) -> str:
        # ① 排除表优先于模式。放在最前面是为了让"模式怎么变都拦得住"不依赖后面的分支。
        if getattr(spec, "name", "") in FULL_ACCESS_EXCLUSIONS:
            return self.fallback.authorize(spec, scope, arguments)
        # ② 不是本机账号就没有完全访问这回事（匿名在 fallback 里还会被 deny）。
        if not self._is_local_account(scope):
            return self.fallback.authorize(spec, scope, arguments)
        # ③ 每次调用都重新读模式：不做进程内缓存，切换后同一会话立即生效。
        if self.controller.effective_mode(self.local_owner_id) != MODE_FULL_ACCESS:
            return self.fallback.authorize(spec, scope, arguments)
        if not spec.requires_confirmation:
            return self.fallback.authorize(spec, scope, arguments)
        return "allow"


class StaticPermissionLookup:
    """测试与子代理用：把已知模式包成一个只读的 lookup。"""

    def __init__(self, mode: str = MODE_STANDARD) -> None:
        self.mode = mode if mode in PERMISSION_MODES else MODE_STANDARD

    def effective_mode(self, owner_id: str) -> str:
        return self.mode


class FullAccessCleanup:
    """开启完全访问时，把该账号**所有会话**的旧审批状态收干净。

    为什么必须存在：切到完全访问之后，之前积压的确认框如果留着，
    它们点了就会执行 —— 而用户点"开启完全访问"时的心智是"从现在起不用点了"，
    不是"顺便把之前攒的都跑一遍"。所以这里只做一件事：**拒绝，不执行**。

    为什么必须按 owner_id 而不是 Scope：权限是账号级的。只清当前
    conversation_id（甚至只清当前 module_id）会留下一堆同账号其它会话里的
    陈旧确认框，而它们和新模式共享同一个账号身份 —— 清一半等于没清。

    清理是"尽力而为 + 有账可查"：任何一步失败都不抛，而是记进
    ``failed`` 列表由调用方返回给前端。静默吞掉失败会让用户看到
    "已开启完全访问"却发现旧确认框还在，比报错更难查。
    """

    def __init__(
        self,
        *,
        confirmation_store: Any = None,
        work_host: Any = None,
    ) -> None:
        self.confirmation_store = confirmation_store
        self.work_host = work_host

    def run(self, owner_id: str) -> dict[str, Any]:
        owner = _normalize_owner(owner_id)
        result: dict[str, Any] = {
            "owner_id": owner,
            "rejected_confirmations": 0,
            "deleted_locks": 0,
            "rejected_work_approvals": 0,
            "cancelled_plan_steps": 0,
            "failed": [],
        }
        if not owner:
            result["failed"].append("owner_id is required")
            return result

        # ① 聊天侧：拒绝该账号所有会话的 pending confirmation，并删掉它们的会话锁。
        if self.confirmation_store is not None:
            reject = getattr(self.confirmation_store, "reject_pending_for_owner", None)
            if callable(reject):
                try:
                    result["rejected_confirmations"] = int(reject(owner) or 0)
                except Exception as exc:
                    result["failed"].append(f"chat_confirmations: {type(exc).__name__}: {exc}")
            else:
                result["failed"].append("chat_confirmations: store has no reject_pending_for_owner")

        # ② Host 侧：拒绝该账号所有 pending approval，并终止等待中的计划步骤。
        #    ``WorkHost`` 的这个方法是 async（它要通知 PlanScheduler），
        #    这里同步调用它只会拿到一个协程 —— 那样清理等于没做，而且不会报错。
        #    所以按返回值类型分派：协程必须在事件循环里跑掉，没有循环就直接开一个。
        if self.work_host is not None:
            reject_work = getattr(self.work_host, "reject_pending_for_owner", None)
            if callable(reject_work):
                try:
                    outcome = self._run_maybe_async(reject_work(owner)) or {}
                    result["rejected_work_approvals"] = int(outcome.get("rejected_approvals", 0))
                    result["cancelled_plan_steps"] = int(outcome.get("cancelled_plan_steps", 0))
                    for item in outcome.get("failed") or []:
                        result["failed"].append(f"work_approvals: {item}")
                except Exception as exc:
                    result["failed"].append(f"work_approvals: {type(exc).__name__}: {exc}")
            else:
                result["failed"].append("work_approvals: host has no reject_pending_for_owner")

        return result

    @staticmethod
    def _run_maybe_async(value: Any) -> Any:
        """同步/异步都收：已在事件循环里就用独立线程跑，避免 ``run()`` 返回未 await 的协程。"""
        if not inspect.isawaitable(value):
            return value

        def _drive() -> Any:
            return asyncio.run(_await(value))

        # 已经在事件循环里（Web 请求线程）时不能再 asyncio.run 同一个循环，
        # 换个线程跑；没有循环时直接跑。两条路都保证协程被真正执行完。
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return _drive()
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_drive).result()


async def _await(value: Any) -> Any:
    return await value


class PermissionLayer:
    """把 store / controller / policy / cleanup 打包成一份，供各入口复用。

    为什么要有这个类：Web 与 CLI 是两个独立进程，但必须是**同一套语义** ——
    两边各自拼一遍"store 指向哪个库、web_host 从哪读、策略怎么装"，
    迟早会分叉（以前 work 工具就在 Web 上缺失过）。这里把口径统一写死：
    库跟 ``AGENT_CORE_DB`` 走，监听地址跟 ``WEB_HOST`` 走。
    """

    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        web_host: str | None = None,
        confirmation_store: Any = None,
        work_host: Any = None,
    ) -> None:
        resolved = db_path or os.getenv("AGENT_CORE_DB", "agent_core.db")
        self.db_path = Path(resolved)
        if web_host is None:
            web_host = os.getenv("WEB_HOST", "127.0.0.1")
        self.store = AccountPermissionStore(self.db_path)
        self.controller = AccountPermissionController(self.store, web_host=web_host)
        self.policy = AccountAwareRiskPolicy(self.controller)
        self.cleanup = FullAccessCleanup(
            confirmation_store=confirmation_store,
            work_host=work_host,
        )

    @property
    def effective_mode(self) -> str:
        return self.controller.effective_mode(LOCAL_OWNER_ID)

    def attach(self, *, confirmation_store: Any = None, work_host: Any = None) -> "PermissionLayer":
        """补上在构造时还拿不到的依赖（Host 通常晚于权限层建好）。"""
        if confirmation_store is not None:
            self.cleanup.confirmation_store = confirmation_store
        if work_host is not None:
            self.cleanup.work_host = work_host
        return self
