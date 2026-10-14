# 定时任务重复通知与媒体取消真实链路修复计划

**Goal:** 让定时任务的一个终态在浏览器侧只产生一次刷新；让用户在聊天里点"确认取消"之后，任务真的被取消。

**Architecture:** 保持现有 AgentLoop / ToolExecutor / EventBus / TaskResultBridge 边界不动。把"业务终态"与"写库后的刷新通知"拆成两条独立事件；把取消工具的 handler 形状对齐 ToolExecutor 的调用约定。

**Tech Stack:** Python 3、FastAPI、SQLite、pytest、React（Vite）。

**Range:** 只动 `media__cancel` 一个 handler 的签名。不动 TTS / ASR 执行链、不改截图 Provider 与产物下载、不动"你在哪"的语义。

---

## Task 1: 拆分定时任务内部事件与聊天通知

**Files:**
- Modify: `agent_core/events.py`
- Modify: `agent_core/task_result_bridge.py`
- Modify: `web_api.py`
- Modify: `fronted/arcueid_daily_50/fronteddd/src/hooks/useTaskEventStream.js`
- Modify: `fronted/arcueid_daily_50/fronteddd/src/pages/ChatPage.jsx`
- Test: `tests/test_task_result_bridge.py`

**Interfaces:**
- Consumes: TaskService 发出的 `task.completed` / `task.failed` / `task.cancelled`。
- Produces: 一条新的 `task.result` 刷新事件。

链路固定为：

```text
task.completed / task.failed / task.cancelled
→ TaskResultBridge 写入聊天历史
→ task.result
→ Web 重新读取聊天记录
```

- [x] **Step 1: 新增 TASK_RESULT 事件类型**

  ```python
  TASK_RESULT = "task.result"
  ```

  注释里写清：三个终态是**业务事件**，只给内部服务消费；`task.result` 是**刷新通知**，只给前端。

- [x] **Step 2: 桥不再重发终态**

  `TaskResultBridge._broadcast()` 改为发 `EventType.TASK_RESULT`，`data` 只有：

  ```python
  {"task_id", "run_id", "status", "result_code", "history_written": True}
  ```

  原始 `error`、`result`、异常类名、标题、本机路径一律不进事件。桥只订阅三个终态、
  不订阅 `task.result`，因此不存在自激；`_handled` 集合保留，防的是终态被重放。

- [x] **Step 3: 保留数据库稳定键**

  `task-result:{task_id}:{run_id}:{status}` 不变 —— 事件重放不会重复写气泡。

- [x] **Step 4: SSE 与前端只订阅刷新通知**

  `web_api._task_event_stream()` 去掉三个终态订阅、新增 `TASK_RESULT`（`task.started` 保留）；
  `_public_event_data()` 的三个终态分支删除，换成 `TASK_RESULT` 分支。
  前端 `useTaskEventStream.js` 与 `ChatPage.jsx` 删除三个终态分支，只留 `task.result` 一次刷新。

- [x] **Step 5: 运行定向测试**

  Run: `python -m pytest tests/test_task_result_bridge.py tests/test_web_api.py tests/test_public_boundary.py -q`

---

## Task 2: 修好 `media__cancel` 的真实调用接口

**Files:**
- Modify: `agent_tools/media.py`
- Test: `tests/test_media_tools.py`

**Interfaces:**
- Consumes: `ToolExecutor.execute()` 按位置调用 `handler(call.arguments, scope)`。
- Produces: `media.cancel` 的 handler 与执行器形状一致。

- [x] **Step 1: 对齐签名**

  ```python
  async def handler(arguments: dict[str, Any], scope: Scope) -> ToolResult:
      job_id = str(arguments.get("job_id") or "").strip()
  ```

  行为固定为：

  | 情形 | 结果 |
  | --- | --- |
  | `job_id` 为空 | `failed` + `permanent`，**不调用服务** |
  | 任务不存在 / Scope 不匹配 | `failed` + "任务不存在或无权访问"，不暴露内部数据 |
  | 排队或运行中 | 调用一次 `service.cancel_job(job_id, scope)` |
  | 已完成或已失败 | 返回真实状态，不描述成"已经取消" |

  `risk="high"` 与 `confirmation_title="取消语音任务"` 保持原样。

- [x] **Step 2: 测试删掉适配器**

  删除 `_loop_ready_cancel_tool()`；`AgentLoop` 直接注册生产 `make_media_cancel_tool()`，
  工具名就是 `media.cancel`。工具调用使用**实际创建出来的** `job_id`。

- [x] **Step 3: 断言落到数据库状态**

  批准后必须断言 `store.get_job(job_id).status == "cancelled"` ——
  只断言"服务被调了一次"是不够的，上一次就是被这半截掩盖掉的。

- [x] **Step 4: 运行定向测试**

  Run: `python -m pytest tests/test_media_tools.py -q`

---

## Task 3: 修正文档和验收记录

**Files:**
- Create: `docs/superpowers/plans/2026-09-15-task-result-and-media-cancel-repair.md`
- Modify: `docs/superpowers/plans/2026-09-14-tool-reliability-steward-repair.md`
- Modify: `README.md`
- Modify: `LESSONS_LEARNED.md`

- [x] **Step 1: 原计划末尾追加审查说明**

  明确写出：上一轮的媒体测试只验证了**确认闸门**，没有验证取消真的发生。

- [x] **Step 2: README 说明两种事件**

  `task.result` 是聊天刷新事件；三个原始终态只供内部服务消费。

- [x] **Step 3: 记录两条经验**

  1. 测试不得使用生产不存在的适配器证明真实链路可用；
  2. 写库后的刷新通知必须使用独立事件，不能复用业务终态事件。

---

## Acceptance Criteria

- 一个 `task.failed`：内部订阅者只收到一次；桥写库后只产生一个 `task.result`。
- `task.result` 到达时聊天记录已存在，且事件里没有 `error`、`result`、路径或异常文本。
- 重放同一终态不重复写聊天、不重复发 `task.result`；不同 Scope 的结果不刷新其他对话。
- Web SSE 不再向浏览器发送原始终态；前端收到一个 `task.result` 只刷新一次。
- `media__cancel` 走真实 ToolExecutor：确认前零次调用，批准后一次，重复批准不再执行。
- 空 `job_id`、越权 Scope、任务不存在、已结束任务分别有对应用例。
- 全量后端、前端、构建、编译和 diff 检查通过。

## Verification Commands

```text
python -m pytest -q --disable-warnings
python -m compileall -q agent_core agent_tools web_api.py chat_cli.py
npm test
npm run build
git diff --check
```

前端命令在 `fronted/arcueid_daily_50/fronteddd` 中执行。全部命令必须 exit 0。
