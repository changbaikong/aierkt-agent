"""记忆系统的运行模式开关。

【为什么单独一个模块】
「谁负责提炼、谁负责轮转」是部署形态决定的，不是代码里的逻辑分支。
把这些判断散在 web_api 里，会出现「读了三个地方才知道本地到底跑不跑提炼」；
集中在这里，改部署就只看这一个文件。

默认值刻意保持**现状**（本地提炼开、轮转开、云端同步关）：
不配环境变量时行为与改造前完全一致，出问题关掉开关就能退回纯本地模式。

读取时机是「调用一次读一次」，不做模块级缓存 —— 测试要能在同一个进程里
改环境变量再看结果；缓存住会让测试之间互相污染。
"""
from __future__ import annotations

import os

_FALSEY = {"0", "false", "no", "off", ""}


def _env_flag(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in _FALSEY


def extraction_enabled() -> bool:
    """本地是否自己提炼记忆。云端模式下关掉，记忆统一由云端提炼。"""
    return _env_flag("MEMORY_EXTRACT_ENABLED", default=True)


def maintenance_enabled() -> bool:
    """本地是否自己做过期/归档轮转。云端模式下关掉，轮转统一由云端执行。"""
    return _env_flag("MEMORY_MAINTENANCE_ENABLED", default=True)


def cloud_sync_enabled() -> bool:
    """是否启用与云端记忆系统的双向同步。默认关闭。"""
    return _env_flag("MEMORY_CLOUD_SYNC_ENABLED", default=False)


def semantic_memory_enabled() -> bool:
    """是否启用新版语义记忆（七类候选 + 持久队列 + 任务驱动召回）。

    **默认关闭**：这是 B 计划的新链路，跟旧的陪伴提炼是两条并存的路。
    打开时同一回合只跑新版候选流程，旧陪伴提炼器不再接手；关掉就完全回到
    改造前的行为，新字段留在库里不动。
    """
    return _env_flag("MEMORY_SEMANTIC_ENABLED", default=False)


def processing_endpoint() -> str:
    """新版语义记忆由**哪一端**处理回合：``local`` 或 ``cloud``。

    「两端都开着新版」曾经导致同一个已发布回合被本地队列和云端各提炼一次 ——
    两套记录内容相近但不相同，还互相覆盖。所以处理端必须是**一个**明确值，
    不是「谁开着谁就上」。取值只有两个，写错就抛，不静默退回默认值：
    配置写错却安静降级，正是当初那个 bug 的形态。

    ``MEMORY_PROCESSING_ENDPOINT`` 未设时默认 ``local``：新版链路的验收脚本、
    临时库和本地测试都跑在本端，先让本端成为唯一处理端；要交给云端提炼就显式
    写 ``cloud``。
    """
    raw = os.environ.get("MEMORY_PROCESSING_ENDPOINT")
    value = "local" if raw is None else raw.strip().lower()
    if value not in ("local", "cloud"):
        raise ValueError(
            f"MEMORY_PROCESSING_ENDPOINT 只能是 local 或 cloud，实际是 {raw!r}"
        )
    return value


def local_processing_enabled() -> bool:
    """本端是不是新版语义记忆的处理端。"""
    return semantic_memory_enabled() and processing_endpoint() == "local"


def cloud_processing_enabled() -> bool:
    """云端是不是新版语义记忆的处理端。"""
    return semantic_memory_enabled() and processing_endpoint() == "cloud"


def content_maintenance_enabled() -> bool:
    """是否启用自动内容维护（对已有记录提出并应用更新）。默认关闭。

    与语义记忆总开关**分开**：总开关关掉时整条新链路都不跑；总开关开着、
    这个开关关掉时，新建候选照常，只是不对已有记录提更新。
    分开的价值在于切换可控——关掉它只停止产生新的维护任务，已认领的事务
    安全结束，既有新版本不倒退。
    """
    return _env_flag("MEMORY_CONTENT_MAINTENANCE_ENABLED", default=False)


def semantic_extraction_enabled() -> bool:
    """是否用新版候选提炼。默认跟随总开关，可单独关掉只留召回。"""
    return _env_flag("MEMORY_SEMANTIC_EXTRACT_ENABLED", default=semantic_memory_enabled())


def semantic_recall_enabled() -> bool:
    """是否读取新版语义记录。

    默认跟随总开关。**两个开关都关时新记录对召回不可见** —— 这比「写了却读不到」
    更容易发现：关掉就明确回到旧行为，不会留下一个悄悄写入、悄悄不用的中间态。
    """
    return _env_flag("MEMORY_SEMANTIC_RECALL_ENABLED", default=semantic_memory_enabled())


def main_chat_practical_enabled() -> bool:
    """主聊天三项实用能力开关（实施工作 A + B）。**默认关闭**。

    打开后才有：本轮关键背景贯穿主回答/核查/重写/收尾、当前会话的疑点澄清闭环。
    关闭时原路径不变 —— 不建影子模式、不加管理界面，必要字段都留空默认值，
    旧任务和旧记忆继续可读。隔离脚本显式打开它。
    """
    return _env_flag("MAIN_CHAT_PRACTICAL_ENABLED", default=False)
