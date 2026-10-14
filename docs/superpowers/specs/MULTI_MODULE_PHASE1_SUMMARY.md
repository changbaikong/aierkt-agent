# 多模块架构前四步实现总结

## 已完成的核心改造

### ✅ 第一步：扩展 Scope 增加 module_id

**实现位置**：[agent_core/contracts.py:9-16](agent_core/contracts.py:9)

```python
@dataclass(frozen=True)
class Scope:
    owner_id: str
    channel: str
    conversation_id: str
    module_id: str = "companion"
```

**验证**：
- `test_module_memory_isolation.py` 验证不同 module_id 的记忆互不可见
- `test_memory_repositories.py` 验证 scope 匹配规则

---

### ✅ 第二步：新增 ModuleSpec/ModuleRegistry

**实现位置**：
- [agent_core/modules.py](agent_core/modules.py) - 核心模块注册表
- [agent_tools/modules.py](agent_tools/modules.py) - 业务模块定义

**核心能力**：
```python
# 模块定义
ModuleSpec(
    module_id="netease",
    description="网易云音乐搜索和播放",
    tools=[...],
    memory_policy=MemoryPolicy(default_visibility="module"),
)

# 工具命名空间
registry.register(netease_module)
registry.register(search_module)
tools = registry.tools_for({"netease", "search"})
# → tools 包含 netease__search_tracks, search__web
```

**同名冲突保护**：
```python
# agent_core/modules.py:45
if existing := self._specs.get(spec.module_id):
    raise ValueError(f"Module {spec.module_id!r} already registered")
```

**验证**：
- `test_module_architecture.py` 验证模块注册和工具命名空间
- `test_multi_module_integration.py` 验证多模块同时注册无冲突

---

### ✅ 第三步：记忆增加模块归属和可见性

**实现位置**：
- [agent_core/memory_repositories.py:19-31](agent_core/memory_repositories.py:19) - MemoryRecord 数据结构
- [agent_core/memory_filtering.py](agent_core/memory_filtering.py) - 记忆过滤层

**数据结构**：
```python
@dataclass(frozen=True)
class MemoryRecord:
    scope: Scope               # 包含 module_id
    visibility: Literal["shared", "module", "channel"]
    layer: Literal["hot", "cold"]
    conflict_key: str | None
    # ...
```

**可见性规则**：
| visibility | 召回范围 | 冲突范围 |
|-----------|---------|---------|
| shared | 跨模块可见 | 跨模块冲突 |
| module | 仅本模块可见 | 仅本模块冲突 |
| channel | 仅本 channel 可见 | 仅本 channel 冲突 |

**验证**：
- `test_memory_filtering.py` 验证过滤规则
- `test_module_memory_isolation.py` 验证跨模块隔离

---

### ✅ 第四步：FTS/Qdrant 查询阶段直接过滤

**FTS 实现**：[agent_core/memory_retrieval.py:142-157](agent_core/memory_retrieval.py:142)

```python
# SQL WHERE 子句在 BM25 排序前就过滤
sql = f"""
    SELECT record_id, bm25(memory_fts) AS score
    FROM memory_fts
    WHERE memory_fts MATCH ?
      AND visibility IN ({placeholders})
      {' AND module_id = ?' if scope.module_id else ''}
    ORDER BY score
    LIMIT ?
"""
```

**向量检索实现**：[agent_core/memory_retrieval.py:93-105](agent_core/memory_retrieval.py:93)

```python
# Qdrant filter 在检索前就应用
filters = [
    models.FieldCondition(key="visibility", match=models.MatchAny(any=list(visibility_filter))),
]
if scope.module_id:
    filters.append(
        models.FieldCondition(key="module_id", match=models.MatchValue(value=scope.module_id))
    )

results = client.query_points(
    collection_name=QDRANT_COLLECTION,
    query=embedding,
    query_filter=models.Filter(must=filters),  # 过滤在检索阶段
    limit=limit,
)
```

**验证**：
- `test_memory_repositories.py::test_search_fts_filters_by_scope` 验证 FTS 过滤
- `test_memory_repositories.py::test_search_vector_filters_by_scope` 验证向量过滤

---

## 集成示例

### 多模块注册

**位置**：[agent_tools/__init__.py:7-28](agent_tools/__init__.py:7)

```python
def register_modules(registry: ModuleRegistry) -> list[str]:
    enabled = []
    
    # Search module
    try:
        registry.register(build_search_module())
        enabled.append("search")
    except Exception as exc:
        print(f"[agent_tools] search module disabled: {exc}")
    
    # Netease module
    try:
        from examples.netease_mock import MockNeteaseCatalog
        provider = MockNeteaseCatalog()
        registry.register(build_netease_module(provider))
        enabled.append("netease")
    except Exception as exc:
        print(f"[agent_tools] netease module disabled: {exc}")
    
    return enabled
```

### CLI 启动输出

```bash
$ python chat_cli.py
[chat_cli] 已启用模块：search, netease
[chat_cli] 已注册 2 个工具：search__web, netease__search_tracks
伴生已就绪。
```

---

## 当前架构限制

### ⚠️ 固定 Scope 的影响

当前 `chat_cli.py` 所有请求使用同一个 scope：

```python
scope = Scope(
    owner_id="test_owner",
    channel="test_channel",
    conversation_id="test_conversation",
    module_id="companion",  # 固定值
)
```

**后果**：
- ✅ 多模块工具都可用（`search__web`、`netease__search_tracks`）
- ✅ 只读工具正常工作
- ⚠️ 所有模块的记忆写入同一个 `module_id="companion"`
- ⚠️ 模块私有状态会混在一起

**适用场景**：
- 第一阶段：只有只读工具（搜索、查询）
- 还未引入副作用工具（播放、队列、收藏）

---

## 下一步工作

### 方案一：动态 Scope 切换（副作用工具上线前必须完成）

根据用户意图或工具调用，动态切换 `scope.module_id`。

**参考文档**：[docs/superpowers/specs/multi_module_integration.md](docs/superpowers/specs/multi_module_integration.md)

**核心修改**：
```python
# chat_cli.py 主循环
module_id = infer_module_id(user_input, enabled_modules)
current_scope = Scope(
    owner_id="test_owner",
    channel="test_channel",
    conversation_id="test_conversation",
    module_id=module_id,  # 动态值
)
```

### 方案二：TTS/ASR 异步任务接口

参考设计文档中的 `submit_media_job()` / `get_media_job()` 接口。

---

## 测试覆盖矩阵

| 测试场景 | 测试文件 | 状态 |
|---------|---------|------|
| 不同 module_id 的记忆互不可见 | test_module_memory_isolation.py | ✅ |
| shared fact 跨模块可见 | test_module_memory_isolation.py | ✅ |
| FTS 查询阶段过滤 module_id | test_memory_repositories.py | ✅ |
| 向量检索查询阶段过滤 | test_memory_repositories.py | ✅ |
| 同名工具注册冲突报错 | test_module_architecture.py | ✅ |
| 多模块并发写入 SQLite | test_memory_repositories.py | ✅ |
| 多模块同时注册可用 | test_multi_module_integration.py | ✅ |
| 网易云高风险工具确认 | ❌ 待实现 | 🔴 |
| TTS/ASR 异步任务接口 | ❌ 待实现 | 🔴 |
| 动态 Scope 切换 | ❌ 待实现 | 🔴 |

---

## 文件清单

### 核心架构
- [agent_core/contracts.py](agent_core/contracts.py) - Scope、ToolSpec、MemoryPolicy
- [agent_core/modules.py](agent_core/modules.py) - ModuleSpec、ModuleRegistry
- [agent_core/memory_repositories.py](agent_core/memory_repositories.py) - MemoryRecord、可见性规则
- [agent_core/memory_filtering.py](agent_core/memory_filtering.py) - 记忆过滤层
- [agent_core/memory_retrieval.py](agent_core/memory_retrieval.py) - FTS/向量检索过滤

### 业务模块
- [agent_tools/modules.py](agent_tools/modules.py) - search/netease 模块定义
- [agent_tools/netease.py](agent_tools/netease.py) - 网易云工具实现
- [examples/netease_mock.py](examples/netease_mock.py) - Mock catalog provider

### 测试
- [tests/test_module_architecture.py](tests/test_module_architecture.py)
- [tests/test_module_memory_isolation.py](tests/test_module_memory_isolation.py)
- [tests/test_memory_filtering.py](tests/test_memory_filtering.py)
- [tests/test_multi_module_integration.py](tests/test_multi_module_integration.py)

### 文档
- [docs/FILTERING_LAYER.md](docs/FILTERING_LAYER.md) - 记忆过滤层设计
- [docs/superpowers/specs/multi_module_integration.md](docs/superpowers/specs/multi_module_integration.md) - 集成指南
