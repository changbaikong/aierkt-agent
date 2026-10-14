# 网易云副作用工具设计

## 目标

将网易云模块拆分为两类工具：

1. **只读工具**：搜索歌曲、查询歌单、获取歌曲信息（已实现：`search_tracks`）
2. **副作用工具**：播放、暂停、加入队列、收藏、切换歌曲（本文档设计）

## 核心原则

副作用工具必须满足：

- **高风险确认**：`risk="high"` → 用户必须确认才能执行
- **幂等键**：支持会话重启后恢复确认状态
- **状态隔离**：播放队列和当前播放状态作为网易云模块私有记忆（`visibility="module"`）
- **不污染共享记忆**：播放控制状态不写入用户全局事实

## 工具设计

### 1. `play_track` - 播放歌曲

```python
PLAY_TRACK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "play_track",
        "description": "开始播放指定的歌曲。这是一个副作用操作，会改变播放状态。",
        "parameters": {
            "type": "object",
            "properties": {
                "track_id": {"type": "string", "description": "歌曲 ID"},
                "position_ms": {"type": "integer", "minimum": 0, "default": 0, "description": "播放起始位置（毫秒）"},
            },
            "required": ["track_id"],
        },
    },
}
```

**风险级别**：`high`  
**确认标题**：`"播放歌曲：{track_title}"`  
**幂等键格式**：`"play:{track_id}:{timestamp}"`  

**状态记忆**：
- `conflict_key="netease.playback.current"`
- `visibility="module"`
- `content="正在播放：{track_title} by {artist}"`

### 2. `queue_track` - 加入播放队列

```python
QUEUE_TRACK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "queue_track",
        "description": "将歌曲加入播放队列。这是一个副作用操作。",
        "parameters": {
            "type": "object",
            "properties": {
                "track_id": {"type": "string"},
                "position": {"type": "string", "enum": ["next", "last"], "default": "last"},
            },
            "required": ["track_id"],
        },
    },
}
```

**风险级别**：`high`  
**确认标题**：`"加入队列：{track_title}"`  
**幂等键格式**：`"queue:{track_id}:{timestamp}"`  

**状态记忆**：
- `conflict_key="netease.queue.state"`
- `visibility="module"`
- `content="队列中有 {count} 首歌"`
- 保留最近 5 条队列变更记录

### 3. `pause_playback` - 暂停播放

```python
PAUSE_PLAYBACK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "pause_playback",
        "description": "暂停当前播放。这是一个副作用操作。",
        "parameters": {"type": "object", "properties": {}},
    },
}
```

**风险级别**：`high`  
**确认标题**：`"暂停播放"`  
**幂等键格式**：`"pause:{timestamp}"`  

**状态记忆**：
- `conflict_key="netease.playback.current"`
- `visibility="module"`
- `content="播放已暂停"`

### 4. `resume_playback` - 恢复播放

**风险级别**：`high`  
**确认标题**：`"恢复播放"`  

### 5. `skip_to_next` - 下一首

**风险级别**：`high`  
**确认标题**：`"切换到下一首"`  

### 6. `skip_to_previous` - 上一首

**风险级别**：`high`  
**确认标题**：`"切换到上一首"`  

### 7. `add_to_favorites` - 收藏歌曲

```python
ADD_TO_FAVORITES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "add_to_favorites",
        "description": "将歌曲添加到收藏夹。这是一个副作用操作。",
        "parameters": {
            "type": "object",
            "properties": {
                "track_id": {"type": "string"},
            },
            "required": ["track_id"],
        },
    },
}
```

**风险级别**：`high`  
**确认标题**：`"收藏歌曲：{track_title}"`  
**幂等键格式**：`"favorite:{track_id}"`  

**状态记忆**：
- `conflict_key="netease.favorites.{track_id}"`
- `visibility="module"`
- `content="已收藏：{track_title}"`

## Provider 契约扩展

```python
class NeteasePlaybackProvider(Protocol):
    """播放控制 Provider（副作用操作）"""
    
    def play_track(self, track_id: str, *, position_ms: int = 0) -> PlaybackState: ...
    def pause_playback(self) -> PlaybackState: ...
    def resume_playback(self) -> PlaybackState: ...
    def skip_to_next(self) -> PlaybackState: ...
    def skip_to_previous(self) -> PlaybackState: ...
    def queue_track(self, track_id: str, *, position: str = "last") -> QueueState: ...
    def add_to_favorites(self, track_id: str) -> FavoriteResult: ...


@dataclass(frozen=True)
class PlaybackState:
    status: str  # "playing" | "paused" | "stopped"
    track: Track | None
    position_ms: int
    queue_length: int


@dataclass(frozen=True)
class QueueState:
    tracks: list[Track]
    current_index: int


@dataclass(frozen=True)
class FavoriteResult:
    success: bool
    message: str
```

## 模块注册

```python
def build_netease_module(
    catalog: NeteaseCatalogProvider,
    playback: NeteasePlaybackProvider | None = None,
) -> ModuleSpec:
    """构建网易云模块，包含只读工具和可选的副作用工具。"""
    
    tools = [make_netease_search_tool(catalog)]
    
    if playback is not None:
        tools.extend([
            make_play_track_tool(playback),
            make_queue_track_tool(playback),
            make_pause_playback_tool(playback),
            make_resume_playback_tool(playback),
            make_skip_to_next_tool(playback),
            make_skip_to_previous_tool(playback),
            make_add_to_favorites_tool(playback),
        ])
    
    return ModuleSpec(
        "netease",
        lambda: tools,
        memory_policy="private_state",  # 播放状态是模块私有的
    )
```

## 状态持久化策略

播放状态作为网易云模块的私有记忆，使用 `MemoryWriter.save_memory()` 保存：

```python
def _persist_playback_state(writer: MemoryWriter, scope: Scope, state: PlaybackState):
    if state.track:
        content = f"正在播放：{state.track.title} by {state.track.artist}"
    else:
        content = f"播放{state.status}"
    
    writer.save_memory(
        scope,
        content,
        conflict_key="netease.playback.current",
        confidence=0.95,
        importance=0.8,
        duration="session",  # 会话级状态，短寿命
        visibility="module",  # 只对网易云模块可见
    )


def _persist_queue_state(writer: MemoryWriter, scope: Scope, queue: QueueState):
    writer.save_memory(
        scope,
        f"队列中有 {len(queue.tracks)} 首歌",
        conflict_key="netease.queue.state",
        confidence=0.9,
        importance=0.5,
        duration="session",
        visibility="module",
    )
```

## 确认流程

1. Agent 调用副作用工具 → `ToolExecutor` 检测 `requires_confirmation=True`
2. 创建 `ConfirmationRequest`，返回 `action_id` 和确认码
3. Agent 暂停，等待用户输入确认码
4. 用户确认后，`ToolExecutor` 执行实际操作
5. 执行成功后，`MemoryWriter` 持久化播放状态为模块私有记忆

## 恢复与幂等性

- 会话重启后，可根据 `action_id` 恢复待确认的播放操作
- 幂等键防止重复执行（例如同一首歌被重复播放）
- 播放状态从模块私有记忆中恢复，不影响其他模块

## 与共享记忆的边界

| 内容 | 存储位置 | 可见性 |
|------|---------|--------|
| 用户喜欢爵士乐 | warm/fact/confirmed | shared（跨模块） |
| 正在播放《Take Five》 | warm/fact/confirmed | module（仅网易云） |
| 播放队列状态 | warm/candidate | module（仅网易云） |
| 收藏的歌曲列表 | warm/fact/confirmed | module（仅网易云） |

## 实现检查清单

- [ ] 定义 `NeteasePlaybackProvider` 协议
- [ ] 实现 7 个副作用工具的 `make_*_tool()` 工厂函数
- [ ] 每个工具标记 `risk="high"`
- [ ] 工具 handler 调用 `_persist_*_state()` 持久化状态
- [ ] 更新 `build_netease_module()` 支持可选的 playback provider
- [ ] 添加集成测试：确认 → 执行 → 状态持久化 → 跨模块隔离
- [ ] 文档示例：如何实现一个简单的 playback provider（mock 或本地播放器）
