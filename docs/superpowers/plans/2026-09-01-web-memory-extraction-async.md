# Web 异步记忆接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Web 端在每次完成回复后异步提炼记忆，并通过 `MemoryWriter` 真实落库，同时让记忆轮换、过期和嵌入补偿在 Web 进程里持续运转。

**Architecture:** `web_api.py` 只负责编排：聊天完成后把提炼工作丢给后台任务，启动时创建提炼器和维护 worker，关闭时统一收尾。`agent_core/memory_postprocess.py` 负责把一轮 Web 对话转换成可持久化的 extracted memories，`MemoryWriter` 负责把这些记忆变成真实的 SQLite/Qdrant 记录，避免任何空账或假账。

**Tech Stack:** FastAPI, `asyncio`, SQLite/FTS5, 本地 Qdrant, 现有 `MemoryExtractor`, `MemoryWriter`, `MemoryMaintenanceWorker`, `EmbeddingRetryWorker`.

## Global Constraints

- 只接入 Web，CLI 这次不改。
- 提炼必须在回复后异步执行，不能阻塞 `chat` 响应。
- 提炼结果必须通过 `MemoryWriter` 真实写入 SQLite/Qdrant，不能只记日志或造假记录。
- 记忆轮换必须继续沿用现有 hot/warm/cold 规则和后台维护语义。
- 失败要诚实降级：没有提炼器配置时跳过后台写入，不伪造记忆。

### Task 1: Build the real extracted-memory persistence bridge

**Files:**
- Modify: `agent_core/memory_writes.py`
- Create: `agent_core/memory_postprocess.py`
- Test: `tests/test_memory_postprocess.py`
- Test: `tests/test_memory_writes.py`

**Interfaces:**
- `MemoryWriter.save_extracted_memories(scope, memories, *, user_text, assistant_reply, turn_id, run_id) -> list[dict[str, Any]]`
- `async def extract_and_store_memory_turn(...) -> dict[str, Any]` in `agent_core/memory_postprocess.py`
- Consumes `ExtractedMemory` objects from `agent_core.memory_extract`
- Produces persisted SQLite rows and Qdrant points through the existing writer path

- [ ] **Step 1: Write the failing tests** that prove a fake extracted turn becomes real persisted rows, with provenance stored in metadata and no direct shortcut around `save_memory`.
- [ ] **Step 2: Run the focused tests** and confirm they fail because the bridge does not exist yet.
- [ ] **Step 3: Implement the bridge** so the helper maps each extracted memory into `MemoryWriter.save_memory(...)`, preserves the extractor's content/conflict/confidence/importance/duration, and records provenance in metadata instead of inventing new tables.
- [ ] **Step 4: Run the focused tests again** and confirm the stored rows are real SQLite records with matching Qdrant payloads.

### Task 2: Wire the Web request path to background extraction

**Files:**
- Modify: `web_api.py`
- Test: `tests/test_web_api.py`

**Interfaces:**
- `_schedule_memory_postprocess(...) -> asyncio.Task[None]`
- `_postprocess_memory_turn(...)` or an equivalent async helper that calls the Task 1 bridge
- Reuses the existing `MemoryExtractor` built from Web runtime config

- [ ] **Step 1: Write the failing Web tests** for a completed chat turn that schedules background extraction and returns the reply immediately.
- [ ] **Step 2: Run the Web tests** and verify they fail because Web still never calls the extractor or writer.
- [ ] **Step 3: Implement the async hook** so `/api/chat` schedules post-response extraction with `asyncio.create_task`, passes `user_text`, assistant reply, history, scope, turn id, and run id, and keeps a live task set for shutdown.
- [ ] **Step 4: Add shutdown cleanup** so pending memory tasks are awaited or cancelled on lifespan exit, preventing dropped writes during server shutdown.
- [ ] **Step 5: Run the Web tests again** and confirm the route still returns normally while the background task performs the real memory write.

### Task 3: Start and observe the memory cycle in Web lifespan

**Files:**
- Modify: `web_api.py`
- Test: `tests/test_web_api.py`
- Test: `tests/test_memory_lifecycle.py`

**Interfaces:**
- `_start_memory_workers()` and `_stop_memory_workers()` or equivalent Web lifecycle helpers
- `MemoryMaintenanceWorker` and `EmbeddingRetryWorker` are started once per process and stopped on shutdown
- `/api/health` reports whether memory extraction and maintenance are actually active

- [ ] **Step 1: Write failing tests** for idempotent worker start/stop, health reporting, and a rotation tick that still migrates/expires memory records when the Web path is used.
- [ ] **Step 2: Run the tests** and confirm they fail because the workers are not started from Web yet.
- [ ] **Step 3: Implement Web lifespan wiring** so the memory maintenance worker and embedding retry worker start during startup and stop during shutdown.
- [ ] **Step 4: Extend health reporting** so the Web health payload exposes extractor availability and memory-cycle status instead of silently pretending the system is healthy.
- [ ] **Step 5: Run the lifecycle and Web tests again** and confirm the memory cycle is visible and deterministic.

## Assumptions

- `EXTRACT_API_KEY` and the other extractor env vars are available in the Web environment when real memory extraction is expected; if not, the app should degrade honestly instead of fabricating rows.
- The extracted-memory provenance lives in row metadata, not in a new SQL column, so the persistence path stays aligned with the existing schema.
- The existing lifecycle rules in `agent_core/memory_writes.py` remain the source of truth for hot-to-warm migration, expiry, purge, and embedding retry.
