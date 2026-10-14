# 用户驱动的记忆生命周期实现计划

> **面向 Agent 工作进程：**必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 逐项实现本计划。步骤使用复选框（`- [ ]`）跟踪。

**目标：**为 hot、warm、cold 记忆增加轻量的用户驱动生命周期，不引入自动反思或三维排序。

**架构：**SQLite 仍是唯一事实源。维护服务执行确定性的 hot 到 warm 迁移、候选过期和延迟清除；Qdrant 清理可补偿，且发生在硬删除之前。检索保留现有 hot 窗口和 RRF 行为，并通过 SQL 过滤过期记录。

**技术栈：**Python、SQLite/FTS5、本地 Qdrant、现有 `MemoryWriter`、`MemoryRetriever` 和控制台日志。

## 全局约束

- 本版本不增加自动 LLM 反思。
- 不用三维分数替换当前 RRF 检索器。
- 已确认事实不能仅因时间流逝而自动过期。
- 过期记录必须先从检索中软移除，之后才硬删除。
- 删除 SQLite 行之前必须先删除 Qdrant 点。

---

### 任务 1：增加生命周期元数据和检索访问追踪

**Files:**
- Modify: `agent_core/memory_repositories.py`
- Modify: `agent_core/memory_writes.py`
- Modify: `agent_core/memory_retrieval.py`
- Test: `tests/test_memory_lifecycle.py`

**Interfaces:**
- `memory_records` gains `last_accessed_at`, `access_count`, `importance`, `expires_at`, `purge_after`, `retention_locked`, and `deletion_reason`.
- `MemoryWriter.touch_records(record_ids)` updates access metadata in one SQLite transaction.
- `MemoryRetriever.retrieve()` touches records that are selected for injection, while excluding expired rows.

- [ ] **Step 1: Write failing tests** for schema defaults, candidate expiry eligibility, and access count updates after retrieval.
- [ ] **Step 2: Run `pytest tests/test_memory_lifecycle.py -v` and verify the new tests fail because the fields and API do not exist.
- [ ] **Step 3: Add idempotent schema columns and indexes; assign importance defaults by kind and initialize `last_accessed_at` on insert.
- [ ] **Step 4: Implement `touch_records()` and call it after final token-budget selection; add `status != 'expired'` authorization filters.
- [ ] **Step 5: Run the focused lifecycle and existing retrieval tests; verify all pass.

### 任务 2：实现确定性维护任务

**Files:**
- Modify: `agent_core/memory_writes.py`
- Test: `tests/test_memory_lifecycle.py`

**Interfaces:**
- `MemoryWriter.run_maintenance(now=None, *, limit=100) -> dict` returns `migrated`, `expired`, `purged`, and `deferred` counts.
- `MemoryMaintenanceWorker.start()` and `.stop()` explicitly control a daemon worker; construction does not start it.

- [ ] **Step 1: Write failing tests** for hot records older than 24 hours moving to warm, candidates older than 14 idle days becoming expired, confirmed facts remaining active, and expired records waiting 30 days before purge.
- [ ] **Step 2: Run the focused tests and verify expected failures.
- [ ] **Step 3: Implement one-transaction SQL state changes for migration/expiry, preserving UUIDs and marking vectors pending when layer changes.
- [ ] **Step 4: Implement purge as Qdrant delete first, then SQLite delete; leave records and increment `deferred` if vector deletion fails.
- [ ] **Step 5: Add explicit start/stop worker with a configurable interval defaulting to 60 seconds.
- [ ] **Step 6: Run lifecycle, layer-consistency, and worker tests.

### 任务 3：记录并验证用户驱动策略

**Files:**
- Create: `docs/memory-lifecycle.md`
- Test: `tests/test_memory_lifecycle.py`

- [ ] **Step 1: Add policy documentation covering hot consolidation, candidate expiry, confirmed-fact protection, cold evidence retention, explicit deletion, and vector compensation.
- [ ] **Step 2: Add tests proving expired records do not appear in warm/cold retrieval and explicit deletion remains the only automatic deletion path for confirmed facts.
- [ ] **Step 3: Run the full test suite with `pytest -q` and record the result.
