# 第三阶段计划：会话恢复与运行时事件总线

**开始时间**: 2026-08-20  
**目标**: 实现 Agent 会话的暂停/恢复机制和统一的运行时事件系统

## 一、背景与动机

### 当前问题

1. **高风险确认期间无锁** - 用户在确认 `netease.play` 时，后续消息可能越过未完成的操作
2. **无法从持久化状态恢复** - `resume()` 依赖内存对象，进程重启后无法恢复 paused 状态
3. **缺少统一事件通知** - 监控台、日志系统各自轮询状态，缺少主动推送
4. **媒体任务不能恢复** - 重启后 pending media jobs 丢失，用户体验中断

### 第二阶段已有基础

- ✅ `MediaTaskStore` 持久化任务和 artifact
- ✅ `Scope` 包含完整的隔离信息（owner_id, channel, conversation_id, module_id）
- ✅ `ToolResult` 标准化为 `output`/`error` 格式
- ✅ 异步 worker 池支持任务取消

### 第三阶段要解决什么

- **会话锁** - paused 期间拒绝新 turn，确保操作顺序
- **状态重建** - 从 SQLite 恢复 module/tool/provider 配置
- **事件总线** - 统一发布 turn/tool/media 生命周期事件
- **进程恢复** - 重启后自动恢复 pending jobs 和 paused conversations

## 二、核心设计

### 2.1 会话锁机制

**目标**：防止 paused 会话被后续 turn 越过。

#### 数据结构

```python
@dataclass
class ConversationLock:
    conversation_id: str
    locked_at: float  # timestamp
    reason: str  # "waiting_confirmation", "media_task_pending"
    action_id: str  # 等待的 action_id
```

#### ConversationManager 扩展

```python
class ConversationManager:
    def __init__(self, ...):
        self._locks: Dict[str, ConversationLock] = 
    
    async def submit_turn(self, scope: Scope, user_message: str) -> TurnResult:
        # 检查会话是否被锁
        if scope.conversation_id in self._locks:
            lock = self._locks[scope.conversation_id]
            return TurnResult(
                status="rejected",
                message=f"会话已锁定：{lock.reason}，请先完成 action {lock.action_id}"
            )
        
        # 正常提交
        return await self._queue_turn(scope, user_message)
    
    def lock_conversation(self, conversation_id: str, reason: str, action_id: str):
        self._locks[conversation_id] = ConversationLock(
            conversation_id=conversation_id,
            locked_at=time.time(),
            reason=reason,
            action_id=action_id
        )
    
    def unlock_conversation(self, conversation_id: str):
        self._locks.pop(conversation_id, None)
```

#### AgentLoop 集成

```python
# runtime.py
if outcome.status == TurnStatus.PAUSED:
    # 锁住会话
    conversation_manager.lock_conversation(
        scope.conversation_id,
        reason="waiting_confirmation",
        action_id=outcome.paused_action_id
    )

# 恢复时解锁
async def resume(action_id: str, decision: str) -> TurnOutcome:
    # ... 执行恢复逻辑 ...
    conversation_manager.unlock_conversation(scope.conversation_id)
```

### 2.2 状态重建机制

**目标**：从 SQLite 恢复完整运行时状态。

#### 持久化 schema 扩展

```sql
-- 新增 module_config 表
CREATE TABLE module_config (
    conversation_id TEXT NOT NULL,
    module_id TEXT NOT NULL,
    config_json TEXT NOT NULL,  -- ModuleSpec 的 JSON 序列化
    created_at REAL NOT NULL,
    PRIMARY KEY (conversation_id, module_id)
);

-- 新增 provider_config 表
CREATE TABLE provider_config (
    conversation_id TEXT NOT NULL,
    provider_type TEXT NOT NULL,  -- "chat", "embedding"
    config_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (conversation_id, provider_type)
);

-- paused_action 表增加字段
ALTER TABLE paused_action ADD COLUMN module_snapshot TEXT;  -- JSON
```

#### 序列化接口

```python
class ModuleSpec:
    def to_dict(self) -> dict:
        return {
            "module_id": self.module_id,
            "tools": [tool.to_dict() for tool in self.tools],
            "memory_policy": self.memory_policy,
            "config": self.config
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "ModuleSpec":
        # 从字典重建 ModuleSpec
        ...

class PersistenceLayer:
    def save_module_config(self, conversation_id: str, module_spec: ModuleSpec):
        config_json = json.dumps(module_spec.to_dict())
        # INSERT INTO module_config ...
    
    def load_module_configs(self, conversation_id: str) -> List[ModuleSpec]:
        # SELECT * FROM module_config WHERE conversation_id = ?
        # 返回所有 ModuleSpec
        ...
```

#### resume() 实现

```python
async def resume(action_id: str, decision: str) -> TurnOutcome:
    # 1. 从 SQLite 加载 paused_action
    action = persistence.load_paused_action(action_id)
    if not action:
        return TurnOutcome(status=TurnStatus.FAILED, error="action not found")
    
    # 2. 重建 ModuleRegistry
    module_specs = persistence.load_module_configs(action.conversation_id)
    module_registry = ModuleRegistry()
    for spec in module_specs:
        module_registry.register(spec)
    
    # 3. 重建 ToolRegistry
    tool_registry = module_registry.build_tool_registry([m.module_id for m in module_specs])
    
    # 4. 重建 Provider
    provider_configs = persistence.load_provider_configs(action.conversation_id)
    chat_provider = rebuild_chat_provider(provider_configs["chat"])
    
    # 5. 恢复 Scope
    scope = Scope(
        owner_id=action.owner_id,
        channel=action.channel,
        conversation_id=action.conversation_id,
        module_id=action.module_id
    )
    
    # 6. 继续执行
    outcome = await agent_loop.resume_from_action(
        action, decision, scope, tool_registry, chat_provider
    )
    
    # 7. 解锁会话
    conversation_manager.unlock_conversation(scope.conversation_id)
    
    return outcome
```

### 2.3 运行时事件总线

**目标**：统一发布 Agent 生命周期事件。

#### 事件定义

```python
from enum import Enum
from dataclasses import dataclass
from typing import Optional, Any

class EventType(Enum):
    TURN_STARTED = "turn.started"
    TURN_PAUSED = "turn.paused"
    TURN_COMPLETED = "turn.completed"
    TURN_FAILED = "turn.failed"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    MEDIA_QUEUED = "media.queued"
    MEDIA_COMPLETED = "media.completed"
    MEDIA_FAILED = "media.failed"

@dataclass
class RuntimeEvent:
    event_type: EventType
    timestamp: float
    scope: Scope
    data: dict  # 事件特定数据
```

#### 事件总线实现

```python
from typing import Callable, List
import asyncio

EventHandler = Callable[[RuntimeEvent], None]

class EventBus:
    def __init__(self):
        self._handlers: Dict[EventType, List[EventHandler]] = {}
        self._async_handlers: Dict[EventType, List[Callable]] = {}
    
    def subscribe(self, event_type: EventType, handler: EventHandler):
        """订阅同步事件"""
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)
    
    def subscribe_async(self, event_type: EventType, handler: Callable):
        """订阅异步事件"""
        if event_type not in self._async_handlers:
            self._async_handlers[event_type] = []
        self._async_handlers[event_type].append(handler)
    
    def publish(self, event: RuntimeEvent):
        """发布事件（同步）"""
        handlers = self._handlers.get(event.event_type, [])
        for handler in handlers:
            try:
                handler(event)
            except Exception as e:
                # 事件处理失败不应阻断主流程
                logging.error(f"Event handler failed: {e}")
    
    async def publish_async(self, event: RuntimeEvent):
        """发布事件（异步）"""
        # 先执行同步 handler
        self.publish(event)
        
        # 再执行异步 handler
        async_handlers = self._async_handlers.get(event.event_type, [])
        tasks = [handler(event) for handler in async_handlers]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
```

#### AgentLoop 集成

```python
class AgentLoop:
    def __init__(self, ..., event_bus: EventBus):
        self.event_bus = event_bus
    
    async def run(self, scope: Scope, user_message: str) -> TurnOutcome:
        # 发布 turn.started
        self.event_bus.publish(RuntimeEvent(
            event_type=EventType.TURN_STARTED,
            timestamp=time.time(),
            scope=scope,
            data={"message": user_message}
        ))
        
        try:
            # ... 执行 turn ...
            
            if outcome.status == TurnStatus.PAUSED:
                self.event_bus.publish(RuntimeEvent(
                    event_type=EventType.TURN_PAUSED,
                    timestamp=time.time(),
                    scope=scope,
                    data={"action_id": outcome.paused_action_id}
                ))
            elif outcome.status == TurnStatus.COMPLETED:
                self.event_bus.publish(RuntimeEvent(
                    event_type=EventType.TURN_COMPLETED,
                    timestamp=time.time(),
                    scope=scope,
                    data={"response": outcome.response}
                ))
            
            return outcome
        
        except Exception as e:
            self.event_bus.publish(RuntimeEvent(
                event_type=EventType.TURN_FAILED,
                timestamp=time.time(),
                scope=scope,
                data={"error": str(e)}
            ))
            raise
```

#### MediaTaskService 集成

```python
class MediaTaskService:
    def __init__(self, ..., event_bus: Optional[EventBus] = None):
        self.event_bus = event_bus
    
    async def submit_tts_job(self, text: str, voice: str, scope: Scope) -> str:
        job_id = self._generate_job_id("tts")
        # ... 保存任务 ...
        
        if self.event_bus:
            self.event_bus.publish(RuntimeEvent(
                event_type=EventType.MEDIA_QUEUED,
                timestamp=time.time(),
                scope=scope,
                data={"job_id": job_id, "kind": "tts"}
            ))
        
        return job_id
    
    async def _execute_tts(self, job: MediaJobRecord):
        try:
            # ... 执行 TTS ...
            
            if self.event_bus:
                self.event_bus.publish(RuntimeEvent(
                    event_type=EventType.MEDIA_COMPLETED,
                    timestamp=time.time(),
                    scope=Scope.from_dict(job.scope),
                    data={"job_id": job.job_id, "artifact_id": artifact_id}
                ))
        
        except Exception as e:
            if self.event_bus:
                self.event_bus.publish(RuntimeEvent(
                    event_type=EventType.MEDIA_FAILED,
                    timestamp=time.time(),
                    scope=Scope.from_dict(job.scope),
                    data={"job_id": job.job_id, "error": str(e)}
                ))
            raise
```

### 2.4 进程重启恢复

**目标**：重启后自动恢复 pending jobs 和 paused conversations。

#### MediaTaskService 启动时恢复

```python
class MediaTaskService:
    async def start(self):
        """启动服务并恢复未完成任务"""
        # 1. 查询所有 queued/running 任务
        pending_jobs = self.store.list_jobs_by_status(["queued", "running"])
        
        # 2. 根据策略处理
        for job in pending_jobs:
            # 策略 A：重新执行（默认）
            await self._requeue_job(job)
            
            # 策略 B：标记为 failed（可配置）
            # self.store.update_job_status(job.job_id, "failed", error="process restarted")
        
        # 3. 启动 worker 池
        for _ in range(self.max_workers):
            asyncio.create_task(self._worker_loop())
    
    async def _requeue_job(self, job: MediaJobRecord):
        """将任务重新加入队列"""
        # 重置为 queued 状态
        self.store.update_job_status(job.job_id, "queued")
        
        # 发布事件
        if self.event_bus:
            self.event_bus.publish(RuntimeEvent(
                event_type=EventType.MEDIA_QUEUED,
                timestamp=time.time(),
                scope=Scope.from_dict(job.scope),
                data={"job_id": job.job_id, "kind": job.kind, "requeued": True}
            ))
```

#### ConversationManager 启动时恢复

```python
class ConversationManager:
    async def start(self):
        """启动时恢复 paused conversations"""
        # 1. 查询所有 paused actions
        paused_actions = self.persistence.list_paused_actions()
        
        # 2. 重建会话锁
        for action in paused_actions:
            self.lock_conversation(
                conversation_id=action.conversation_id,
                reason="waiting_confirmation",
                action_id=action.action_id
            )
        
        # 3. 可选：通知用户有待确认的操作
        # （通过事件总线或其他方式）
```

## 三、实施步骤

### Step 1: 会话锁机制 (任务 #6)

1. 在 `ConversationManager` 增加 `_locks` 字典
2. 实现 `lock_conversation()` / `unlock_conversation()`
3. 在 `submit_turn()` 检查锁状态
4. 在 `AgentLoop.run()` 的 PAUSED 分支调用 `lock_conversation()`
5. 在 `resume()` 完成后调用 `unlock_conversation()`
6. 编写测试：paused 会话拒绝新 turn

### Step 2: 状态重建机制 (任务 #7)

1. 创建 `module_config` 和 `provider_config` 表
2. 为 `ModuleSpec`/`ToolSpec` 实现 `to_dict()`/`from_dict()`
3. 在 `PersistenceLayer` 增加 `save_module_config()` / `load_module_configs()`
4. 在 `AgentLoop` 启动时保存 module/provider 配置
5. 实现 `rebuild_chat_provider()` / `rebuild_module_registry()`
6. 重写 `resume()` 使用重建逻辑
7. 编写测试：从 SQLite 重建完整运行时

### Step 3: 运行时事件总线 (任务 #8)

1. 创建 `agent_core/event_bus.py`
2. 定义 `EventType` 枚举和 `RuntimeEvent` 数据类
3. 实现 `EventBus` (subscribe/publish)
4. 在 `AgentLoop` 所有关键点发布事件
5. 在 `MediaTaskService` 所有关键点发布事件
6. 在 `monitor_console.py` 订阅事件并显示
7. 编写测试：验证事件发布顺序和内容

### Step 4: 进程重启恢复 (任务 #9)

1. 在 `MediaTaskService.start()` 实现 `_requeue_job()`
2. 在 `ConversationManager.start()` 实现 paused action 恢复
3. 增加配置项：`media.restart_policy` ("requeue" / "fail")
4. 编写测试：模拟重启后恢复

### Step 5: 集成测试 (任务 #10)

1. 端到端测试：提交高风险工具 → paused → 尝试新 turn（应被拒绝）→ resume → 成功
2. 端到端测试：提交 TTS → 重启服务 → 任务自动恢复并完成
3. 事件流测试：验证完整生命周期的事件序列
4. 监控台集成：验证事件实时显示

## 四、验收标准

| 验收点 | 测试方法 |
|-------|---------|
| paused 会话拒绝新 turn | 提交高风险工具 → paused → 提交新消息 → 收到 rejected |
| resume() 从持久化重建 | 进程重启 → 调用 resume() → 验证 module/tool/provider 正确 |
| 事件总线发布完整事件 | 订阅所有事件 → 执行 turn → 验证收到 started/tool/completed |
| 媒体任务重启恢复 | 提交 TTS → 重启服务 → 验证任务自动重新执行 |
| 监控台实时显示事件 | 启动监控台 → 执行操作 → 验证事件日志实时更新 |

## 五、风险与依赖

### 风险

1. **序列化复杂度** - ModuleSpec/ToolSpec 包含函数引用，需要设计可序列化的注册表
2. **事件顺序保证** - 异步环境下事件可能乱序，需要明确时序依赖
3. **恢复策略选择** - running 状态的任务重启后无法知道进度，需要明确是重试还是失败

### 依赖

- ✅ 第一阶段：Scope 和模块化基础
- ✅ 第二阶段：MediaTaskService 和持久化
- ⚠️ 外部依赖：真实 TTS/ASR 服务（当前为 mock，不影响架构验证）

### 延后至第四阶段

- Qdrant server 部署
- 模块热插拔（动态加载/卸载模块）
- 熔断降级策略
- 监控告警（Prometheus/Grafana）

## 六、时间估算

- Step 1 (会话锁): 1 天
- Step 2 (状态重建): 2 天
- Step 3 (事件总线): 2 天
- Step 4 (进程恢复): 1 天
- Step 5 (集成测试): 1 天

**总计**: 7 天

## 七、成功标准

- [ ] 所有新增测试通过（预计 20+ 个）
- [ ] 现有 186 个测试继续通过
- [ ] 监控台能实时显示事件流
- [ ] 重启后 pending jobs 自动恢复
- [ ] paused 会话正确锁定和解锁
- [ ] 代码覆盖率不低于 85%
