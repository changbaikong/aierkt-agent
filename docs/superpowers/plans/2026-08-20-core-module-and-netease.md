# 核心模块运行时与 NetEase 实现计划

> **面向 Agent 工作进程：**必须使用 `superpowers:executing-plans` 子技能逐项执行本计划。步骤使用复选框（`- [ ]`）跟踪。

**目标：**明确模块组合方式，增加与 provider 无关的 NetEase 模块；其私有播放状态必须持久化、具备幂等性，并受现有高风险确认策略控制。

**架构：**以声明方式扩展 `ModuleSpec`。新增按完整 `Scope` 建键的 SQLite 模块状态仓库，并将其注入 NetEase 写工具。使用 `module__tool` 作为符合 provider 要求的命名空间编码。

**技术栈：**Python 3.11、dataclasses、SQLite WAL、unittest，以及现有 Agent 核心契约。

## 全局约束

- 保留 `Scope` 默认值和现有公开工具契约。
- 使用 `module__tool`，不要使用带点号的工具名，因为当前 provider 会拒绝点号。
- NetEase provider 依赖必须通过明确的协议注入；模块工厂不得读取环境配置。
- NetEase 播放状态不得使用 `MemoryWriter` 或 `memory_records`。
- 每个生产行为都必须先有聚焦的失败 unittest，并在进入下一个行为前验证为绿色。
- 本计划不包含确认锁、`resume(action_id)`、媒体任务和扩展运行时事件。

---

### 任务 1：完善声明式模块元数据

**文件：**修改 `agent_core/modules.py`、`agent_core/__init__.py` 和 `tests/test_module_architecture.py`。

**接口：**提供 `ModuleHealth`，以及 `ModuleSpec` 的 `provider_config`、`timeout_seconds`、`max_concurrency`、`rate_limit_per_minute`、`health_check` 字段和 `ModuleRegistry.health(module_id)`。

- [ ] **步骤 1：编写失败测试**

```python
def test_module_registry_returns_a_healthy_status_from_the_module_check(self) -> None:
    modules = ModuleRegistry()
    modules.register(ModuleSpec("catalog", lambda: [], health_check=lambda: ModuleHealth.ok()))
    self.assertEqual(modules.health("catalog").status, "healthy")
```

- [ ] **步骤 2：运行测试确认失败**

Run: `pytest tests/test_module_architecture.py -k health -v`
预期：失败，因为 `ModuleHealth` 和 `ModuleRegistry.health` 尚不存在。

- [ ] **步骤 3：编写最小实现**

增加不可变的 `ModuleHealth(status, detail="")` 和 `ModuleHealth.ok()`。增加声明式 `ModuleSpec` 元数据。未知 ID、缺少检查器、健康结果格式错误或检查器抛异常时，返回 `ModuleHealth("unavailable", detail)`，不得继续抛出异常。重新导出 `ModuleHealth`。

- [ ] **步骤 4：运行测试确认通过**

Run: `pytest tests/test_module_architecture.py -k health -v`
预期：通过。

- [ ] **步骤 5：提交**

Run: `git add agent_core/modules.py agent_core/__init__.py tests/test_module_architecture.py` then `git commit -m "feat: add declarative module health metadata"`.

### 任务 2：持久化模块私有状态

**文件：**修改 `agent_core/persistence.py`、`agent_core/__init__.py`；创建 `tests/test_module_state_store.py`。

**接口：**提供 `ModuleStateStore` 和 `SQLiteModuleStateStore(path)`，包含 `get(scope)`、`put(scope, state)` 和 `compare_and_set(scope, idempotency_key, state)`。

- [ ] **Step 1: Write the failing test**

```python
def test_state_is_private_to_the_complete_scope(self) -> None:
    store = SQLiteModuleStateStore(self.path)
    first = Scope("u", "cli", "one", "netease")
    second = Scope("u", "cli", "two", "netease")
    store.put(first, {"status": "playing"})
    self.assertEqual(store.get(first), {"status": "playing"})
    self.assertEqual(store.get(second), {})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_module_state_store.py -v`
Expected: FAIL because `SQLiteModuleStateStore` does not exist.

- [ ] **Step 3: Write minimal implementation**

Create `agent_core_module_state` with primary key `(owner_id, channel, conversation_id, module_id)`, `state_json`, `last_idempotency_key`, and `updated_at`. Use `_database`; atomically reject a matching key and otherwise replace state plus key. Re-export the protocol and implementation.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_module_state_store.py -v`
Expected: PASS.

- [ ] **Step 5: Run persistence regressions**

Run: `pytest tests/test_agent_core.py tests/test_module_architecture.py tests/test_module_state_store.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

Run: `git add agent_core/persistence.py agent_core/__init__.py tests/test_module_state_store.py` then `git commit -m "feat: persist module private state"`.

### 任务 3：增加 NetEase 读取与副作用工具契约

**Files:** Modify `agent_tools/netease.py`, `agent_tools/modules.py`, `agent_tools/__init__.py`; create `tests/test_netease_tools.py`.

**接口：**提供 `NeteasePlaybackProvider`、`make_netease_playback_tools(provider, state_store)` 和 `build_netease_module(provider, state_store)`。

- [ ] **Step 1: Write the failing test**

```python
async def test_playback_tool_is_high_risk_and_updates_private_state(self) -> None:
    provider = FakePlaybackProvider()
    state = SQLiteModuleStateStore(self.path)
    spec = make_netease_playback_tools(provider, state)[0]
    result = await ToolExecutor().execute(spec, ToolCall("c1", spec.name, {"track_id": "42", "idempotency_key": "p1"}), self.scope)
    self.assertTrue(spec.requires_confirmation)
    self.assertEqual(result.status, "ok")
    self.assertEqual(state.get(self.scope)["current_track_id"], "42")
    self.assertEqual(provider.play_calls, ["42"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_netease_tools.py -v`
Expected: FAIL because playback tools do not exist.

- [ ] **Step 3: Write minimal implementation**

Add a provider protocol with `play`, `pause`, `add_to_queue`, and `favourite`. Implement a high-risk tool for each with bounded track IDs and required idempotency keys. A new key calls the provider then persists state. A provider exception returns a normalized failed result without a state change. A repeated key returns stored state and `idempotent_replay=True` without calling the provider. Keep catalog search low risk and inject dependencies through `build_netease_module`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_netease_tools.py -v`
Expected: PASS.

- [ ] **Step 5: Verify confirmation integration and optional composition**

Run: `pytest tests/test_netease_tools.py tests/test_module_architecture.py tests/test_web_search_tool.py -v`
Expected: PASS; write tools require confirmation through `DefaultRiskPolicy`, catalog tools remain low risk, and search composition works.

- [ ] **Step 6: Commit**

Run: `git add agent_tools/netease.py agent_tools/modules.py agent_tools/__init__.py tests/test_netease_tools.py` then `git commit -m "feat: add idempotent netease playback tools"`.

### 任务 4：验证并记录可复用经验

**文件：**修改 `LESSONS_LEARNED.md`。

- [ ] **步骤 1：运行支持的测试套件**

Run: `pytest -v`
预期：通过；`tests/legacy_backend` 仍由 `pytest.ini` 排除。

- [ ] **步骤 2：运行 SQLite 状态回归测试**

Run: `pytest tests/test_module_state_store.py tests/test_memory_repositories.py -v`
Expected: PASS without `database is locked` errors.

- [ ] **步骤 3：追加带日期的经验记录**

记录：NetEase 播放状态和副作用幂等性属于模块私有运行数据，必须按完整 Scope 存储，不得使用 `MemoryWriter`。未来的副作用模块应使用模块状态存储和显式注入的 provider。

- [ ] **步骤 4：提交**

Run: `git add LESSONS_LEARNED.md` then `git commit -m "docs: record netease module state lesson"`.
