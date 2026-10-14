# Control Plan 跨任务编排（落地记录）

日期：2026-09-12
状态：已实现并通过验证

## 1. 做了什么

在既有的 `WorkHost + WorkLedger + LocalWorkerProvider` 之上增加一层**持久化**的
`ControlPlan`，让一个用户目标可以拆成多个有依赖关系的 `PlanStep`。每个 `PlanStep`
最终只是一个普通 `WorkItem`，Provider 依旧只认单个 `WorkRequest`。

首个验收场景（已由 `tests/test_control_plan.py` 与 `tests/test_web_plan_api.py` 覆盖）：

> 整理下载目录 → 用户批准后移动文件 → 截图 → 音量调低 10%

没有引入消息队列、Temporal 或新的微服务，仍然是 Python + FastAPI + SQLite + React。

## 2. 模块地图

| 文件 | 职责 |
| --- | --- |
| `agent_core/control_plan.py` | 契约（`ControlPlan` / `PlanStep`）、状态与失败策略集合、提交前校验、`${step:x.output.y}` 引用解析 |
| `agent_core/work_ledger.py` | 新增 `control_plans` / `plan_steps` / `plan_step_dependencies` / `plan_events` 四张表与全部读写方法 |
| `agent_core/plan_scheduler.py` | 计划调度器：解锁、失败传播、审批暂停、取消、重试、重启恢复 |
| `agent_core/work_host.py` | 计划入口（`submit_plan` / `get_plan` / `list_plans` / `cancel_plan` / `retry_plan` / `plan_events`）与 Worker 回调 |
| `agent_core/work_module.py` | 把 `work` 模块与 Host 注册进模块化流程 |
| `agent_tools/work.py` | 主 Chat 工具：`work__plan_submit` / `work__plan_get` / `work__plan_list` / `work__plan_cancel` |
| `web_api.py` | `/api/work/plans*` 六个接口 + 计划 SSE |
| `fronted/.../src/pages/planPresentation.js` | 计划视图的纯展示逻辑（可单测） |
| `fronted/.../src/pages/WorkPage.jsx` | 计划列表 / 顺序步骤列表 / 待审批步骤 / 产物 / 取消重试 |

## 3. 数据流

```text
POST /api/work/plans 或 work__plan_submit
        ↓
validate_plan_definition(...)      ← 未通过校验 = 不落库、不执行
        ↓
ledger.create_plan(...)            ← 计划整体一次性写入
        ↓
PlanScheduler.advance(plan_id)
        ↓  只启动无依赖步骤
WorkItem + Attempt                 ← 复用既有 WorkHost 队列
        ↓
LocalWorkerProvider.execute(WorkRequest)
        ↓
Host 收到 WorkItem 完成事件
        ↓
PlanScheduler.on_attempt_finished  → 更新 PlanStep → 解锁后续步骤
```

## 4. 状态机

计划：`queued → running ⇄ waiting_approval → completed | failed | cancelled | interrupted`

步骤：`pending → ready → running → completed | failed | blocked | cancelled | interrupted`，
以及 `waiting_approval`。

关键判定（`PlanScheduler._recompute_status`）：

- 有任一步骤 `waiting_approval` → 计划 `waiting_approval`
- 全部 `completed` → 计划 `completed`
- 仍有 `pending/ready/running` → 计划 `running`
- 全部终态且有 `cancelled` → 计划 `cancelled`
- 全部终态且有 `failed` → 计划 `failed`
- 全部终态且有 `interrupted` → 计划 `interrupted`

失败策略：

| 策略 | 行为 |
| --- | --- |
| `stop`（默认） | 步骤 `failed`，所有传递依赖方 `blocked`，计划 `failed` |
| `retry` | 同一 `WorkItem` 上新建 Attempt，最多 `MAX_STEP_ATTEMPTS=3` 次 |
| `skip` | 步骤 `failed` 但计划继续；只有**引用了该步骤输出**的依赖方变 `blocked` |

一旦某个 `blocked` 步骤的依赖全部重新 `completed`（例如重试成功后），它会在
`advance()` 中自动回到 `pending` 并继续，不需要新计划。

## 5. Host 侧边界（提交前校验）

`validate_plan_definition` 在落库前拒绝：

- 未知 `task_type`、Provider capability 不支持的 `task_type`
- `step_id` 非法或重复、空 `objective`、空计划、超过 32 步
- `depends_on` 指向不存在的步骤、自依赖、循环依赖
- 引用不存在的步骤，或引用未在 `depends_on` 中声明的步骤（跨 Scope 输入）
- 输入中出现 `approved_moves` / `authorization` / `authorized` / `approval_granted`
- 输入试图覆盖 `owner_id` / `channel` / `conversation_id` / `module_id` / `allowed_roots`
- 输入中出现 `AGENT_WORK_ROOTS`（键或值都会命中）
- `file.list` 这类只读步骤声明 `requires_approval`
- 非法 `failure_policy`

Provider 侧仍然不做任何计划相关的事：不建 WorkItem、不改计划状态、不读其他步骤、不扩权限、
不伪造审批、不改依赖。

## 6. 审批与恢复

- `file.organize` 仍然先生成持久化移动清单，`waiting_approval` 暂停整条链路；
  批准后 Host 用同一 `WorkItem` 创建后继 Attempt 只执行清单内文件。
- 批准 → 从原步骤继续并按依赖推进后续步骤；拒绝 → 步骤 `cancelled`、依赖方 `blocked`、计划 `cancelled`。
- 计划处于终态时，`decide_approval` 会直接拒绝过期审批，避免"僵尸"后继 Attempt 落地。
- 取消计划**不会撤销**已完成的文件移动：`completed` 步骤原样保留，其余标记 `cancelled`。
- 重启时 `recover_runtime()` 标记运行中的 Attempt，`recover_plans()` 把 `queued/running/waiting_approval`
  的计划及其在飞步骤标记 `interrupted` 并写入 `plan.interrupted` 事件；**不自动重放任何未知副作用**，
  只有用户显式 `retry` 才创建新 Attempt。

## 7. 事件

入库在前、推送在后；SSE 只负责触发前端刷新，最终状态一律回读账本。
事件序号在 `plan_events` 中按 `plan_id` 连续，`GET /api/work/plans/{id}/events?after_sequence=` 可补发。

必须支持的事件全部具备：`plan.created` / `plan.started` / `step.ready` / `step.started` /
`step.waiting_approval` / `step.completed` / `step.failed` / `step.blocked` / `plan.completed` /
`plan.failed` / `plan.cancelled` / `plan.interrupted`。
另外补充了三个更具可观测性的扩展事件：`step.retrying` / `step.cancelled` / `step.interrupted`
（避免把重试、审批拒绝、重启中断都压进 `step.failed` 里）。

## 8. 相对原设计的偏差（有意为之）

1. **多了一张 `plan_events` 表。** 原设计只列了三张表，但"保存计划级事件"与
   `?after_sequence=` 补发都需要按计划自增的序号，`work_events` 是按 `attempt_id` 计数的，无法复用。
2. **`plan_steps` 增加 `position` 列。** 否则同一秒内写入的步骤只能按 `step_id` 排序，
   顺序步骤列表会与用户提交顺序不一致。
3. **`requires_approval` 由计划显式声明，Host 不再按渠道强制加码。** 单任务链路里
   "chat 提交的截图/音量必须审批" 的规则不套用到计划上，因为计划要跑的就是
   "整理（审批）→ 截图 → 音量" 这类一次性授权场景；`file.organize` 的移动清单审批
   仍然由 Provider 硬性保证，无法被计划绕过。
4. **计划重试允许用于 `cancelled` 计划。** 取消后重试会把未完成步骤重置为 `pending`（或新建 Attempt），
   这与 Web 页面的"取消 / 重试 / 恢复"按钮语义一致。

## 9. 验收

```bash
python -m pytest -q --disable-warnings        # 558 passed
python -m compileall -q agent_core web_api.py # exit 0
git diff --check                              # exit 0
cd fronted/arcueid_daily_50/fronteddd
npm test                                      # 78 pass
npm run build                                 # ✓ built
```

新增测试：

- `tests/test_control_plan.py`：计划校验（未知类型 / 缺失依赖 / 循环 / 伪造审批 / 跨 Scope /
  `AGENT_WORK_ROOTS`）、只启动无依赖步骤、解锁与阻塞、审批暂停/批准/拒绝、取消保留已移动文件、
  重试保留原计划新建 Attempt、重启 `interrupted` 且不重放、输出引用传递、事件序号与补发、`retry` 策略。
- `tests/test_web_plan_api.py`：`202 + plan_id`、计划详情与单任务详情一致、Scope 隔离、
  `after_sequence` 补发、取消、artifacts 下载的 Scope 与过期校验。

## 10. 已知限制

- 首版只支持 DAG：不支持循环，也不支持运行中动态修改计划。
- 一个 `PlanStep` 只绑定一个 `WorkItem`，重试通过新 Attempt 实现。
- `retry` 策略的退避是"立即重试"，没有指数退避；上限 3 次后按 `stop` 处理。
- 计划暂停只发生在步骤级审批点，没有"计划级人工闸门"。
- **同一份 `AGENT_WORK_DB` 同时只能被一个进程持有。** `WorkHost.start()` 会把账本里
  `running` 的 Attempt 和计划判为 `interrupted`（这是重启恢复的必要前提），所以
  `chat_cli.py` 与 `web_api.py` 不要同时指向同一个 work 库；需要同时跑两个入口时，
  给其中一个设 `AGENT_DISABLED_MODULES=work` 或另指一个 `AGENT_WORK_DB`。
