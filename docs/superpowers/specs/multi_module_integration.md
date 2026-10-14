# 多模块集成示例：chat_cli 扩展指南

## 概览

`chat_cli.py` 当前只注册了 `search` 模块。本文档展示如何：

1. 注册多个模块（search + netease）
2. 根据用户意图动态选择模块
3. 在不同模块间切换 Scope

## 当前架构回顾

```python
# chat_cli.py 第 87-89 行
modules = ModuleRegistry()
enabled_modules = register_modules(modules) if register_modules is not None else []
registry = modules.tools_for(set(enabled_modules))
```

问题：
- 所有模块的工具都注册到同一个 `registry`
- 所有请求使用同一个 `scope`（`module_id` 固定为 `"companion"`）
- Agent 无法区分哪个工具属于哪个模块

## 方案一：静态多模块注册（推荐用于第一阶段）

保留单个 AgentLoop，但注册多个模块的工具，让 Agent 自己选择合适的工具。

### 1.1 注册多个模块

```python
# agent_tools/__init__.py 修改
def register_modules(registry: ModuleRegistry) -> list[str]:
    """Register optional module definitions without forcing their providers to exist."""
    from .modules import build_search_module, build_netease_module
    
    enabled = []
    
    # Search module (always enabled if BOCHA_API_KEY exists)
    try:
        registry.register(build_search_module())
        enabled.append("search")
    except Exception as exc:
        print(f"[agent_tools] search module disabled: {exc}")
    
    # Netease module (enabled if provider is configured)
    try:
        # Lazy import to avoid hard dependency
        from examples.netease_mock import MockNeteaseCatalog
        provider = MockNeteaseCatalog()
        registry.register(build_netease_module(provider))
        enabled.append("netease")
    except Exception as exc:
        print(f"[agent_tools] netease module disabled: {exc}")
    
    return enabled
```

### 1.2 多模块工具注册

```python
# chat_cli.py 修改
modules = ModuleRegistry()
enabled_modules = register_modules(modules) if register_modules is not None else []
print(f"[chat_cli] 已启用模块：{', '.join(enabled_modules)}")

# 所有启用模块的工具都注册到同一个 registry
registry = modules.tools_for(set(enabled_modules))
print(f"[chat_cli] 已注册工具：{', '.join(spec.name for spec in registry)}")
```

### 1.3 固定 Scope 的限制

当前方案下，所有请求使用同一个 `scope.module_id`：

```python
scope = Scope(
    owner_id="test_owner",
    channel="test_channel",
    conversation_id="test_conversation",
    module_id="companion",  # 固定值
)
```

**后果**：
- ✅ 工具能正常调用（`search__web`、`netease__search_tracks` 都可用）
- ✅ 只读工具不受影响
- ⚠️ 记忆隔离失效：所有模块的记忆写入同一个 `module_id="companion"`
- ⚠️ 副作用工具的状态会混在一起

**适用场景**：
- 第一阶段：只有只读工具（搜索、查询）
- 还未引入副作用工具（播放、队列）

## 方案二：动态 Scope 切换（推荐用于副作用工具上线后）

根据用户意图或工具调用，动态切换 `scope.module_id`。

### 2.1 意图识别层

```python
def infer_module_id(user_text: str, enabled_modules: list[str]) -> str:
    """根据用户输入推测所需模块。"""
    text_lower = user_text.lower()
    
    # 网易云关键词
    if any(kw in text_lower for kw in ["播放", "音乐", "歌曲", "网易云", "play", "music"]):
        if "netease" in enabled_modules:
            return "netease"
    
    # 搜索关键词
    if any(kw in text_lower for kw in ["搜索", "查找", "search", "find", "google"]):
        if "search" in enabled_modules:
            return "search"
    
    # 默认回落到 companion
    return "companion"
```

### 2.2 动态创建 Scope

```python
# chat_cli.py 主循环修改
while True:
    user_input = (await asyncio.to_thread(input, "你: ")).strip()
    if not user_input:
        continue
    
    # 根据用户意图动态选择模块
    module_id = infer_module_id(user_input, enabled_modules)
    current_scope = Scope(
        owner_id="test_owner",
        channel="test_channel",
        conversation_id="test_conversation",
        module_id=module_id,  # 动态值
    )
    
    print(f"[模块: {module_id}]", flush=True)
    
    handle = await manager.submit(
        AgentRequest(
            user_text=user_input,
            scope=current_scope,  # 使用动态 scope
            system_prompt=SYSTEM_PROMPT,
        )
    )
```

### 2.3 记忆召回的跨模块逻辑

动态 Scope 后，记忆召回行为：

| 记忆类型 | visibility | 召回行为 |
|---------|-----------|---------|
| 用户喜欢爵士乐 | shared | ✅ search、netease、companion 都能召回 |
| 搜索历史"Python 教程" | module (search) | ❌ netease 模块召回不到 |
| 正在播放《Take Five》 | module (netease) | ❌ search 模块召回不到 |
| 最近对话（hot） | module | ❌ 只能在同一 module_id 内召回 |

**共享事实的冲突处理**：
- 同一个 `conflict_key="user.preference.music_genre"`
- search 模块写入："用户喜欢爵士乐" (shared)
- netease 模块写入："用户喜欢摇滚" (shared)
- → 后者归档前者到 cold 层，跨模块冲突规则一致

## 方案三：多 AgentLoop 实例（未来扩展）

为每个模块创建独立的 AgentLoop，完全隔离工具和提示词。

```python
# 未来架构预览（当前不实现）
class MultiModuleManager:
    def __init__(self):
        self.loops = {
            "search": AgentLoop(provider, search_registry, search_scope),
            "netease": AgentLoop(provider, netease_registry, netease_scope),
            "companion": AgentLoop(provider, companion_registry, companion_scope),
        }
    
    async def route_request(self, user_text: str):
        module_id = infer_module_id(user_text, list(self.loops.keys()))
        loop = self.loops[module_id]
        return await loop.run(AgentRequest(user_text))
```

## 实现检查清单（方案一 + 方案二）

**第一阶段：静态多模块（只读工具）**
- [x] `agent_tools/__init__.py` 的 `register_modules()` 支持注册多个模块
- [x] `chat_cli.py` 打印启用的模块列表和工具名称
- [x] 创建 `examples/netease_mock.py` 提供 mock catalog provider
- [ ] 测试：search 和 netease 工具同时可用
- [ ] 文档：说明当前所有模块共享 `module_id="companion"` 的限制

**第二阶段：动态 Scope（副作用工具上线后）**
- [ ] 实现 `infer_module_id()` 意图识别函数
- [ ] `chat_cli.py` 主循环改为动态创建 Scope
- [ ] 测试：不同模块的私有记忆互不可见
- [ ] 测试：shared fact 的冲突规则跨模块生效
- [ ] 文档：记忆召回的跨模块行为矩阵

## 示例：Mock NetEase Provider

```python
# examples/netease_mock.py
from agent_tools.netease import NeteaseCatalogProvider, Track


class MockNeteaseCatalog:
    """本地 mock，用于测试多模块集成，无需真实网易云 API。"""
    
    def search_tracks(self, query: str, *, limit: int) -> list[Track]:
        # 简单的关键词匹配
        catalog = [
            Track("1", "Take Five", "Dave Brubeck", "Time Out"),
            Track("2", "So What", "Miles Davis", "Kind of Blue"),
            Track("3", "Blue in Green", "Bill Evans", "Kind of Blue"),
            Track("4", "Autumn Leaves", "Cannonball Adderley", "Somethin' Else"),
        ]
        results = [t for t in catalog if query.lower() in t.title.lower() or query.lower() in t.artist.lower()]
        return results[:limit]
```

## 后续优化方向

1. **意图识别改进**：使用 LLM 而非关键词匹配
2. **会话上下文**：记住用户最近使用的模块，作为默认回落
3. **混合工具调用**：一轮对话中同时调用 search 和 netease 工具
4. **模块优先级**：某些模块（如 companion）始终参与工具选择
