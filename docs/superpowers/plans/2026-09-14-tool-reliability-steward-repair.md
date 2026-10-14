# Tool Reliability and Steward Output Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复任务列表快路径、定时任务错误泄露、媒体取消权限、音量权限不一致和导航错误误报，让主 Chat 只根据可信结构化结果说人话。

**Architecture:** 保留现有 AgentLoop、ToolExecutor、WorkHost 和 Provider 边界。工具层产生结构化状态和稳定错误码；聊天历史、SSE 与管家措辞只消费公开投影，不读取异常原文或本机路径。只读查询继续使用零模型调用的本地模板。

**Tech Stack:** Python 3、FastAPI、SQLite、pytest、现有 React Web 前端。

## Global Constraints

- 本轮不修改 `agent_tools/media.py` 中 TTS/ASR/状态查询/产物工具的 handler 调用签名，不启用 ASR。
- 本轮不修改截图 Provider、截图 artifact、截图下载接口或截图完成气泡。
- 不修改“你在哪”的产品语义，也不处理普通状态查询误发 `accepted` 气泡的问题。
- 只读任务列表与条件提醒列表保持零次模型调用。
- 任何聊天历史、SSE、HTTP 任务详情不得包含异常原文、堆栈、本机绝对路径或 Provider 名称。
- 播放、暂停、继续、下一首保持直接执行；系统音量与网易云音量统一要求确认。
- 不新增消息队列、微服务或第二套任务账本。
- 保留现有脏工作区中的无关改动，不清理或回滚用户文件。

---

### Task 1: 让正式任务列表工具进入只读快路径

**Files:**
- Modify: `agent_tools/tasks.py`
- Modify: `agent_tools/condition_tasks.py`
- Test: `tests/test_fastpath.py`
- Test: `tests/test_task_tools.py`

**Interfaces:**
- Consumes: `AgentLoop._readonly_request()` 已有的 `tasks__list` / `conditions__list` 路由。
- Produces: 正式模块生成的 list ToolSpec 均满足 `read_only=True`、`deterministic_route_allowed=True`。

- [x] **Step 1: 用正式模块工具写失败测试**

  测试不得手工构造 ToolSpec。分别调用 `make_task_tools()` 和 `make_condition_task_tools()`，注册命名空间后的真实工具，然后断言“我的任务列表”“有哪些条件提醒”不会调用模型，回复只包含标题与数量，不包含 `request_text`、`allowed_tools`、`last_error`。

- [x] **Step 2: 运行定向测试并确认当前失败**

  Run: `python -m pytest tests/test_fastpath.py tests/test_task_tools.py -q`

  Expected: 正式 list 工具因缺少只读快路径标记而回落模型或断言失败。

- [x] **Step 3: 修正正式 ToolSpec**

  将 `tasks.list` 和 `conditions.list` 声明为：

  ```python
  ToolSpec(
      "list",
      schema,
      handler,
      risk="low",
      read_only=True,
      deterministic_route_allowed=True,
  )
  ```

  `tasks.get` 同时标记 `read_only=True`，但不启用确定性路由；它仍由模型在需要具体 task_id 时调用。其他 create/cancel/run_now 权限不变。

- [x] **Step 4: 验证公开摘要边界**

  增加断言：正式 list 输出即使包含内部字段，`fastpath.task_list_summary()` 也只保留标题、数量和用户可理解的状态。

- [x] **Step 5: 运行定向测试**

  Run: `python -m pytest tests/test_fastpath.py tests/test_task_tools.py tests/test_condition_tasks.py -q`

---

### Task 2: 统一定时任务的公开完成与失败回执

**Files:**
- Create: `agent_core/task_result_bridge.py`
- Modify: `agent_core/task_service.py`
- Modify: `web_api.py`
- Modify: `agent_core/memory_extract.py`
- Test: `tests/test_task_result_bridge.py`
- Test: `tests/test_web_api.py`

**Interfaces:**
- Consumes: `task.completed` / `task.failed` / `task.cancelled` 事件及 TaskStore 中的内部诊断。
- Produces: `TaskResultBridge`, `public_task_result(status, title, result_code, safe_summary) -> str`。

- [x] **Step 1: 写聊天历史泄露的失败测试**

  构造 `TASK_FAILED`，其中内部 error 含 `RuntimeError`、绝对路径和换行；断言写入 ConversationRepository 的内容不含这些文本，SSE 与刷新后的 history 内容一致。

- [x] **Step 2: 写状态文案测试**

  固定公开表达：

  ```text
  completed → “「标题」已经办好了。……”
  failed    → “「标题」这次没办成。已经记录了情况，稍后可以重试。”
  cancelled → “「标题」已经停下来了。”
  ```

  `safe_summary` 只能来自已确认的业务输出；内部 error 只进入日志和 TaskStore。

- [x] **Step 3: 实现 TaskResultBridge**

  桥订阅任务终态事件，按 Scope 写回原对话，使用稳定键：

  ```text
  task-result:{task_id}:{run_id}:{status}
  ```

  写回时设置 `touch_activity=False`、`kind="task_result"`，并广播现有任务事件供前端刷新。不得把 `event.data["error"]` 传入公开措辞函数。

- [x] **Step 4: 移除 web_api 的旧字符串拼接出口**

  删除 `_task_event_message()` 对 raw error/result 的直接拼接以及旧 history 投影订阅，改由 TaskResultBridge 单点负责。`_public_event_data()` 继续作为 SSE 的第二道过滤。

- [x] **Step 5: 排除长期记忆提炼**

  `kind="task_result"` 与现有 `work_result`、`steward_progress` 一样，不进入长期记忆；当前对话上下文仍保留它。

- [x] **Step 6: 运行定向测试**

  Run: `python -m pytest tests/test_task_result_bridge.py tests/test_web_api.py tests/test_task_service.py tests/test_memory_extract.py -q`

---

### Task 3: 修正媒体取消权限元数据

**Files:**
- Modify: `agent_tools/media.py`
- Test: `tests/test_media_tools.py`
- Test: `tests/test_agent_core.py`

**Interfaces:**
- Consumes: `DefaultRiskPolicy` 的 high-risk 确认流程。
- Produces: `media__cancel` 必须先暂停并等待用户确认。

- [x] **Step 1: 写权限失败测试**

  断言媒体取消 ToolSpec 为 `risk="high"`，确认标题为中文；匿名 Scope 被拒绝，普通 Scope 进入 confirmation，确认前不调用 service。

- [x] **Step 2: 只修改取消工具元数据**

  设置：

  ```python
  risk="high"
  confirmation_title="取消语音任务"
  ```

  不修改媒体 handler 的参数签名，不修 TTS/ASR 执行链，不改变 get_status/get_artifact。

- [x] **Step 3: 验证恢复与重复确认**

  复用 AgentLoop confirmation 流程，断言确认后最多调用一次、拒绝时零调用、已终态任务不会被描述成已撤销。

- [x] **Step 4: 运行定向测试**

  Run: `python -m pytest tests/test_media_tools.py tests/test_agent_core.py -q`

---

### Task 4: 统一音乐与系统音量的权限规则

**Files:**
- Modify: `agent_tools/modules.py`
- Modify: `agent_core/routing.py`
- Modify: `agent_core/work_host.py`
- Test: `tests/test_netease_cloud.py`
- Test: `tests/test_routing.py`
- Test: `tests/test_work_host.py`

**Interfaces:**
- Consumes: ToolSpec risk policy、WorkHost 对 `system.volume.adjust` 的现有审批机制。
- Produces: 所有音量修改均需确认；音乐运输控制保持直接执行。

- [x] **Step 1: 写权限矩阵测试**

  锁定以下规则：

  | 操作 | 权限 |
  | --- | --- |
  | 查询播放器、查询音量 | 直接只读 |
  | 播放、暂停、继续、下一首 | 直接执行 |
  | 设置网易云音量 | 等待确认 |
  | 调整系统音量 | 等待确认 |
  | 创建歌单、添加歌曲 | 等待确认 |

- [x] **Step 2: 修正网易云 ToolSpec**

  将 `set_volume` 设为 `risk="high"`，标题为“调整网易云音量”。保持 play/next/pause/resume 为 low，保持 create_playlist/add_to_playlist 为 high。

- [x] **Step 3: 保持系统音量 Host 审批不可伪造**

  `WorkHost.submit()` 对 chat 来源的 `system.volume.adjust` 继续写入 Host 自有 `_requires_approval`；通用 input/context 中的 `approved`、`authorization`、`web_confirmed` 等字段不得跳过审批。Web 专用入口只授权本次精确百分比。

- [x] **Step 4: 阻止后台计划绕过高风险工具**

  定时任务的 `allowed_tools` 校验继续拒绝高风险 `netease__set_volume`；Control Plan 中的系统音量步骤必须进入 `waiting_approval`。

- [x] **Step 5: 运行定向测试**

  Run: `python -m pytest tests/test_netease_cloud.py tests/test_routing.py tests/test_work_host.py tests/test_task_service.py -q`

---

### Task 5: 导航只返回稳定错误码和安全提示

**Files:**
- Modify: `agent_tools/navigation.py`
- Modify: `agent_core/fastpath.py`
- Modify: `agent_core/loop.py`
- Test: `tests/test_navigation.py`
- Test: `tests/test_steward.py`

**Interfaces:**
- Consumes: LocationStore、AmapNavigationClient 和 ToolResult。
- Produces: 导航失败结构 `{status, output: {reason}, error_kind}`，其中 reason 为稳定错误码。

- [x] **Step 1: 写错误分类测试**

  覆盖：

  ```text
  destination_required
  destination_invalid
  location_unavailable
  location_stale
  destination_not_found
  walking_range_exceeded
  navigation_unavailable
  ```

  断言任何结果都不包含异常类名、HTTP 响应原文、API 返回片段或 traceback。

- [x] **Step 2: 在导航 handler 内完成可信映射**

  参数问题和已知 Provider 响应映射为稳定 reason；未知异常记录完整日志，对 ToolResult 只返回 `navigation_unavailable`。`error_kind` 分别使用 permanent、permission、transient 或 timeout，不能根据展示文案反推。

- [x] **Step 3: 增加导航公开措辞映射**

  固定表达：

  ```text
  location_unavailable     → “我还没有拿到你的当前位置。打开位置共享后，我再给你规划。”
  location_stale           → “我这里的起点有一会儿没更新了。重新共享位置后，我再继续。”
  destination_not_found    → “这个目的地我没定位准。换一个更完整的名称或地址，我再找。”
  walking_range_exceeded   → “这段路超出了步行规划范围。我可以改成驾车路线再看。”
  navigation_unavailable   → “这次路线服务没有响应。我保留了目的地，稍后可以接着查。”
  ```

  不让主模型根据 raw error 自由解释失败原因。

- [x] **Step 4: 验证成功结果与隐私边界**

  成功时保留距离、时长、方式和地图 URL；最终气泡不得主动复述起点/终点精确经纬度。导航调用高德所需坐标仍留在工具内部。

- [x] **Step 5: 运行定向测试**

  Run: `python -m pytest tests/test_navigation.py tests/test_steward.py tests/test_agent_core.py -q`

---

### Task 6: 整链验收与项目经验记录

**Files:**
- Modify: `LESSONS_LEARNED.md`
- Modify: `README.md`
- Test: existing backend and frontend suites

**Interfaces:**
- Consumes: Tasks 1-5 的公开契约。
- Produces: 可复现的验收记录和后续工具接入规则。

- [x] **Step 1: 增加跨层回归场景**

  至少覆盖以下真实入口：

  ```text
  我的任务列表
  有哪些条件提醒
  取消这个语音任务
  把网易云音量调到 30%
  导航到广州塔
  ```

  每个场景从 AgentLoop/HTTP 入口运行到最终聊天历史，避免再次出现“零件测试通过、真实链路失败”。

- [x] **Step 2: 验证公开边界**

  扫描聊天历史、SSE 与任务详情响应，禁止出现：`Traceback`、异常类名、`provider error`、由任务失败带出的本机绝对路径、内部工具名和原始 JSON。

- [x] **Step 3: 运行完整验收**

  Run:

  ```text
  python -m pytest -q --disable-warnings
  python -m compileall -q agent_core agent_tools web_api.py
  npm test -- --run
  npm run build
  git diff --check
  ```

  前端命令在 `fronted/arcueid_daily_50/fronteddd` 中执行。全部命令必须 exit 0。

- [x] **Step 4: 更新说明和经验**

  README 写明只读快路径、音量确认矩阵和导航公开错误码。`LESSONS_LEARNED.md` 追加本次经验：测试必须经过 ToolExecutor，错误原文不能先落聊天再过滤。

## Acceptance Criteria

- “我的任务列表”和“有哪些条件提醒”均零模型调用并自然回答。
- 定时任务失败的实时事件与刷新历史都不含内部错误。
- 媒体取消在确认前不会执行；TTS 链路保持当前状态，不在本轮修复。
- 网易云与系统音量修改都需确认，播放控制不增加确认。
- 导航失败只按稳定错误码说话，不出现外部服务或异常原文。
- 全量后端、前端、构建、编译和 diff 检查通过。

---

## 2026-09-15 复审：本计划有两条"通过了但其实没验到"

验收命令当时全绿，但事后复查发现两处结论是假的。修复计划见
`docs/superpowers/plans/2026-09-15-task-result-and-media-cancel-repair.md`。

### 一、Task 2 的回执桥把"业务终态"当成了刷新通道

Step 3 写的是"写回后**广播现有任务事件**供前端刷新"，也就是把
`task.completed / task.failed / task.cancelled` 原样再发一次。这带来两个后果：

1. 前端在同一个终态上收到**两帧**（TaskService 一帧 + 桥重发一帧），刷两次历史；
2. "业务终态"和"刷新通知"两种语义挤在一个通道里 —— 内部服务订阅者也会被
   重发的那一条打到，只能靠桥自己记 key 兜住不自激。

现在拆开：终态只由 TaskService 发一次，桥写库成功后发一条独立的
`task.result`（只带 task_id / run_id / status / result_code / history_written）。
SSE 与前端只订阅 `task.result`。

### 二、Task 3 只修了元数据，"取消"的真实链路从没被验过

Step 2 明确写了"不修改媒体 handler 的参数签名"。于是测试为了绕开这一点，
自己造了一个生产里不存在的适配器 `_loop_ready_cancel_tool()`：

- 它把关键字签名 `(job_id=..., scope=...)` 掰成 ToolExecutor 的形状，
  再把工具名从 `media.cancel` 换成 `media__cancel`；
- 那组用例因此只证明了**确认闸门**（暂停 / 恢复 / 拒绝）是对的，
  **没有**证明确认之后真的取消了任务 —— 断言里甚至没有"数据库状态变成 cancelled"。

真实链路上 ToolExecutor 是按位置调 `handler(call.arguments, scope)` 的，
取消工具拿到的是整个 arguments 字典，`service.cancel_job` 收到的是字典而不是
job_id：用户点了确认，什么也没发生。

现在取消工具的签名就是执行器的形状，测试删掉适配器、直接注册生产工具，
用真实创建出来的 job_id，批准后断言落库状态为 `cancelled`。
