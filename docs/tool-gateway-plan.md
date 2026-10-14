# 工具链封口与动作网关 —— 逐文件实施清单（修订版）

状态（2026-09-22 更新）：**§0 ～ §4 已全部落地；§5（工具筛选）与"全仓禁止绕过网关"
的总守门测试仍未开始。**
基线：`agent_core/` 66 个模块、`tests/` 119 个文件 1453 个用例、前端 24 个测试文件 196 个用例。
**大改造前基线（2026-09-21 建立）：1473 passed / 64 subtests / 144.28s / 退出码 0。**
**2026-09-22 收尾基线：Python 1583 passed / 64 subtests、前端 197 passed、`vite build` 通过。**
三方对账：初版计划 + 我的审计 + 酥主反驳，已在下面逐条落成事实。

各章状态一览：§0 ✅ / §1 ✅ / §2 ✅ / §3 ✅ / §4 ✅ / §5 ⏳ / §6 见章内逐条 / §7 待最终验收。

---

## 零、先修的事实错误（已按酥主拍板独立完成）

### 0.1 `routing.py:61` 常量名错，静默废掉一条软路由 —— 已修 ✅

```
routing.py:59  WORK_SUBMIT_TOOL = "work__submit"     ← 对
routing.py:60  APPROVE_TOOL     = "work__approve"    ← 对
routing.py:61  PLAN_SUBMIT_TOOL = "plan_submit"      ← 错，缺 work__ 前缀
routing.py:62  SUBAGENT_TOOL    = "subagent__dispatch" ← 对
```

（上面是**当时**的代码快照。`APPROVE_TOOL` 已于 2026-09-22 随 §4
「模型侧工具面收窄」整个删除 —— 它对应的工具不再存在。
快照保留原样，不回溯修改。）

实测：

```
registry.resolve("plan_submit")      -> None
router._has_tool(PLAN_SUBMIT_TOOL)   -> False
router._has_tool("work__plan_submit")-> True
```

后果：`routing.py:473` 那条「多步 + 依赖 → 建议 plan_submit」的分支永远进不去，
带顺序依赖的多步目标被静默降级成单步提交，走不到计划编排。
误标原因：现有路由测试手工造了无前缀的 `ToolSpec`，把生产命名空间掩盖掉了
（`test_routing.py:full_registry()` 把常量**自己**喂回注册表，测的是"常量与它自己一致"）。

**已做的改动**：

| 文件:行 | 改动 |
|---|---|
| `agent_core/routing.py:64` | `"plan_submit"` → `"work__plan_submit"` |
| `agent_core/routing.py:487` | hint 里硬编码的裸 `plan_submit` 改用 `{PLAN_SUBMIT_TOOL}` |
| `agent_core/routing.py:501` | 硬编码 `subagent__dispatch` 改用 `{SUBAGENT_TOOL}` |
| `tests/test_routing_real_registry.py` | **新建**，用真实 Work 模块构建注册表（7 条） |
| `tests/test_routing.py:585` | 补注释禁止用常量喂注册表 |

**红绿过程中顺带发现的第二个缺陷**：`:487` 的 hint 一直在教模型调一个裸名 `plan_submit`。
改常量前两者恰好都是裸名所以自洽，一改常量就暴露 —— 这条若不修，
修复了探测却仍然给模型指一个不存在的工具名。

**验证**：`test_routing.py` + `test_routing_real_registry.py` = **58 passed**。

### 0.2 `agent_tools/__init__.py:53-55` 静态清单缺 `weather` —— 已修 ✅

```
BUILTIN_MODULE_MANIFESTS 实际键 = [search, netease, system, media,
                                   location, navigation, tasks, conditions, work]
```

`weather` 不在这份清单里，但 `__init__.py:244`（工厂失败）照样调
`_record_unavailable(registry, "weather", ...)`，而它第一行就是
`description, declared_tools = BUILTIN_MODULE_MANIFESTS[module_id]`。

实测复现 `KeyError: 'weather'`：天气工厂抛异常（缺高德 key 就是这条）。

后果不是"天气坏了"，而是**天气坏了之后，负责报告这件事的代码自己崩了** ——
能力快照拿不到任何答案，`/api/health` 上的表现是"莫名其妙少一块"，而不是"天气不可用，原因 X"。

**已做的改动**：

| 文件:行 | 改动 | 性质 |
|---|---|---|
| `agent_tools/__init__.py:55` | `[module_id]` → `.get()` + 缺失兜底 | 止血（彻底修法见 §2） |
| `agent_tools/__init__.py:43` | 清单补 `"weather": ("高德天气查询", ("query",))` | 止血（`CapabilityCatalog` 落地时删） |
| `agent_tools/__init__.py:252-262` | 天气的 disabled 分支补 `else: _record_unavailable(...)` | **第二个缺陷** |
| `tests/test_module_diagnostics.py` | **新建**（13 条） | 守门 |

**红绿过程中挖出的第二个缺陷**：`tasks` / `conditions` / `location` 都有 disabled 的 `else`
分支，**唯独 weather 没有** → 天气被 `AGENT_DISABLED_MODULES` 禁用时**从诊断里整个消失**。
前端能力面板看不到它，用户只会觉得"莫名其妙少了天气"，而不是"天气被配置禁用了"。

核对过：天气工具的**真实本地名是 `query`**（注册后为 `weather__query`），不是模块 id。

**验证**：`test_module_diagnostics.py` 13 passed；`test_capabilities.py` 17 passed；
`test_weather_location_chain.py` + `test_fastpath.py` 125 passed。

### 0.3 解释器分歧（不是 bug，是验收命令必须写死）—— 已确认

同一台机器两条 PATH：

| 来源 | `python` 解析到 | pytest |
|---|---|---|
| 酥主 shell | 托管 `3.13.12` | 无 |
| 我这个沙箱 bash | 系统 `3.12.0` | 9.1.1 |

所以"`pytest -q` 能不能跑"取决于谁在敲。**验收命令统一写
`python -m pytest -q` 并在 CI/脚本里显式指定解释器**，
`docs` 里注明本项目要 3.12（依赖装在它下面）。

**大改造前基线已建立**（第 5 步）：`python -m pytest -q` → **1473 passed**、
64 subtests、144.28s、退出码 0、无 `SAFE_DELETE_BULK_REJECTED`。

---

## 一、动作协议与网关（✅ 已完成，全绿）

**结果**：新增 `agent_core/actions.py`、`agent_core/action_journal.py`、
`tests/test_action_gateway.py`(32)、`tests/test_action_journal.py`(12)、
`tests/test_action_gateway_boundary.py`(4)。全量 `python -m pytest -q` →
**1518 passed**、64 subtests、141.01s、退出码 0（= 基线 1473 + 新增 45）。

**做红绿时挖出三处原计划没有的缺陷，已一并修掉**

1. `agent_core/__init__.py` 的 `__all__` 里 `"ToolExecutor"` 没跟着 import 一起删
   → `hasattr(agent_core,'ToolExecutor')` 是 False 但 `__all__` 在撒谎，
   `from agent_core import *` 会炸。**这正是"只改一半"的典型症状**：
   两处都得改，改一处看起来是好的。
2. `_validate_arguments` 对 `required` 违规只回父路径 → 报
   `invalid_arguments: arguments: required`，用户看不出缺哪个字段。
   补 `_field_path()` 从 `exc.validator_value` 抠出缺失字段名。
3. 边界扫描器一开始按属性名 `.handler` 一刀切 → 误报 `events.py:206/209`
   （那是 `EventBus` 的 `sub.handler(event)`，与动作执行毫无关系）。
   判据收紧成"接收者像工具规格（`spec` / `*_spec`）"。
   **宁可窄也不要假警报**：一条会误报的守门测试很快会被加 `skip` 绕过，等于没有。

**`ToolExecutor` 降级的具体做法**

改成 `_ToolExecutor` + 从 `agent_core.__init__` 摘掉导出 + `tools.__all__` 不含它，
但**保留过渡别名** `ToolExecutor = _ToolExecutor`：五个测试文件仍按旧名 import，
它们在 §6 统一重写。现在删旧名会把"测试还没重写"和"生产代码有洞"两种红混在一起。

四个生产调用点（`loop.py` 三处、`condition_tasks.py` 一处）留给 §3 逐个迁。
`tools.py` 本身也从边界扫描范围排除（§3 迁完随调用点一起走）。

**`requirements.txt`** 补 `jsonschema>=4,<5`（已装 4.26.0）。此前只是别人的传递依赖 ——
一旦上游调整依赖树，网关那条 `except ImportError` 会**静默放行**，
表现为"畸形参数不再被拦"，且不报任何错。

**`replayed` 字段（新增，原计划没有）**

审计表刻意不存返回内容，所以重复编号回放时 `output` 必然是空的。
原测试断言 `second.output == first.output`，这其实是**在两个都为空的意义上成立**的假通过。
改成断言 `first.replayed is False` / `second.replayed is True` + `second.output == {}`：
把"拿不回产物"这件事**明说出来**，而不是伪装成一次产出空结果的正常完成。

---

### 原设计条目（保留供对照）

### 新建 `agent_core/actions.py`

- `ActionGateway.invoke(request: ActionRequest) -> ActionObservation`
  执行顺序（这个顺序就是正确性，不要调换）：
  1. 动作编号原子登记 → 重复编号直接返回既有观察，**不重跑**；
  2. 来源校验（`request.origin` vs `ToolSpec.allowed_origins`）；
  3. JSON Schema 参数校验（`jsonschema>=4,<5`）；
  4. 权限判定（三态 allow / confirm / deny）；
  5. `confirm` → 落 `running` + 返回 `waiting_approval`（不执行）；
  6. 执行前先把 `running` 写进行为记录（这一步是对账的前提，见 §1.2）；
  7. 私有 handler 执行（超时 + 异常边界都在这里，不再有第二个）；
  8. 状态归一化 → `ActionObservation`；
  9. 落终态 + 发事件。
- `ActionGateway.resume(approval_id, decision) -> ActionObservation`
- 状态映射表（唯一真相，前端只认右列）：

| 现有状态 | 动作状态 |
|---|---|
| `ok`、`completed` | `completed` |
| `failed` | `failed` |
| `rejected`、`not_executed` | `rejected` |
| `paused`、`waiting_approval` | `waiting_approval` |
| `cancelled` | `cancelled` |
| `interrupted`、非只读超时、重启遗留 `running` | `outcome_unknown` |
| 未知状态 | `failed` + `invalid_action_status` |

- 新增错误类别 `indeterminate`，与 `TOOL_ERROR_KINDS` 并列。
  **模型不得自动重试 `outcome_unknown`** —— 这条要写成提示词约束 + 网关拒绝重放两重。

### 新建 `agent_core/action_journal.py`

动作审计表（只加表，不动现有 SQLite 结构）：动作编号、名称、来源、完整作用域、
run/turn/tool-call 标识、状态、耗时、错误码、审批编号。
**不存参数值**，只存参数的名字与是否通过校验。

- 登记 / 状态转换 / 对账三个方法。
- `reconcile_on_start()`：进程启动时把遗留的 `running` 全部改判 `outcome_unknown`，
  且标记为不可重放。**这一步是必需件**：没有它，`outcome_unknown` 只是嘴上说说，
  重启后那条记录还挂着 `running`，没人知道副作用到底发生没有。

### 修改

- `agent_core/contracts.py`
  - 新增 `ActionOrigin`（`chat_model` / `chat_fastpath` / `scheduler` / `work_host` / `subagent` / `api`）。
  - 新增 `ActionRequest`（动作编号、名称、参数、完整 `Scope`、来源、run/turn/tool-call 标识）。
  - 新增 `ActionObservation`（状态、结构化产物、错误、错误类别、审批编号）。
  - `ToolSpec` 增 `capability_id`、`visibility`、`allowed_origins`。
  - **明确写进注释**：请求参数不得携带任何授权状态（沿用 `work_host.py:38-46`
    `_HOST_OWNED_SUBMIT_KEYS` 那套"连别名一起挡"的做法，把名单上收到契约层，
    这样网关是唯一校验点，不用每个任务类型各挡一遍）。
- `agent_core/tools.py`
  - `ToolExecutor` 降为模块内私有（`_ToolExecutor`），删除公共导出。
  - 保留 `classify_tool_error` 公开（网关要用）。
- `agent_core/__init__.py`：不再导出 `ToolExecutor`。

### 新建测试

- `tests/test_action_gateway.py`：参数错误、未知动作、来源拒绝、权限三态、审批、
  超时、异常、取消、重复编号不重跑、`outcome_unknown` 不重放。
- `tests/test_action_journal.py`：登记幂等、状态转换合法性、重启对账把 `running` → `outcome_unknown`。

---

## 二、唯一能力目录（✅ 已完成，全绿）

**结果**：新增 `agent_core/capability_catalog.py`、`agent_core/capability_runtime.py`、
`tests/test_capability_catalog.py`(11)、`tests/test_runtime_bootstrap.py`(8)。
全量 `python -m pytest -q` → **1537 passed**、64 subtests、142.76s、退出码 0。

`BUILTIN_MODULE_MANIFESTS` 已删（`tests/test_capability_catalog.py` 从动态属性
与源码 AST 两层守着它不许复活），`_permission_layer` 全局槽与 `set_permission_layer` 已删，
`register_tools()` / `TOOL_BUILDERS` 已删。§0.2 的 `KeyError` 随之一并从根上消失。

### 做完成后抓到的东西

**旧静态清单本来就漏了两个工具。** 拿目录跟真实 `build_tools()` 对账时发现
`netease` 的实际产出是 19 个工具，清单只写了 17 个 —— 漏了 `get_volume` / `set_volume`。
漏的后果只是能力面板少显示两项，**所以从来没被人发现**。这正是"两份真相"的代价，
也是这次对账测试的价值：它发现的不是我新写的代码，是已经存在了很久的洞。

`build_media_module(db_path=...)` 要 `Path` 不是 `str`（`MediaTaskStore.__init__`
直接对 `db_path.parent` 调 mkdir）。旧装配恰好传的是 Path，坑被掩盖着。

### 我自己写出来的两个 bug（都被测试抓住）

1. `build_build_context` 里 `source = os.environ if env is None else env`
   → 传部分 env 就把整份环境抹了。兼容层（只传两个任务库路径）一调，
   `AGENT_DISABLED_MODULES` **静默失效**，症状是"配了禁用却完全不生效"且不报错。
   改成"覆盖层不是替换层"（`dict(os.environ)` 再 `update(env)`），
   并补 `test_env_overrides_are_a_layer_not_a_replacement`。
2. 兼容层 `register_modules` 没接上 `registry_provider` 的延迟闭包。
   旧实现是 `lambda: registry.tools_for(set(enabled))`（在 enabled 长全之后才求值），
   我传的是 None → 落到空注册表。表现：`task_service.registry_provider()` 返回空表，
   定时任务触发时转派"什么都不做"且不报错。
   被 `test_register_modules_wires_task_registry_after_composition` 抓住。

这两个都不是"迁移时的排版差异"，是**会静默改变运行时行为**的洞。

### 入口改动

- `chat_cli.py`：删掉自建 `ModuleRegistry()` + `tools_for()` + 硬编码 db 默认值，
  改调 `build_runtime`；`TaskRouter` 传进 AgentLoop 的 **`router=`**（注意参数名，
  不是 `task_router`）；装配失败报"运行时初始化失败"并返回退出码 1。
- `web_api.py`：同样改调 `build_runtime`；删掉自己那句
  `TaskRouter(registry=registry, allowed_roots=roots)` —— 那是分叉的源头，
  自建实例拿到的是另一份 roots；`roots` 改调共享的 `resolve_work_roots()`。

### TaskRouter 不得降级（酥主拍板 2）

`_install_task_router` 把失败包成 `RuntimeBootstrapError` 抛出。两重写法：

- 静态扫描（`test_task_router_is_not_treated_as_an_optional_degradation`）：
  不许被包进会吞异常的 `try`；
- 实跑（`test_task_router_failure_surfaces_as_bootstrap_error`）：
  直接让构造炸掉，确认异常真的冒出来。

二者缺一不可：静态扫不到"异常在上游就被吞了"，实跑扫不到"下次有人改成静默降级"。

**实跑证据**：46 个工具、10 个模块全部启用、0 失败、TaskRouter 已安装。

### 过渡保留

`agent_tools.register_modules` / `discover_modules` 保留作兼容层（签名与行为不变），
内部转调目录。它们**不装 TaskRouter**，所以新代码一律用 `build_runtime`。

### 原设计条目（保留供对照）

### 新建 `agent_core/capability_catalog.py`

`CapabilityCatalog` 成为模块与工具的**唯一构建来源**：谁提供哪些工具、工具的
`capability_id`、`visibility`、`allowed_origins`、是否 `required_for_readiness`，
全部由这里声明。运行时注册表构建完成后**冻结**（构造后不可再 `register`）。

### 新建 `agent_core/capability_runtime.py`

统一 `CapabilityRuntime`：Web、CLI、子代理调**同一个**构建函数，依赖通过显式上下文传入
（provider / event_bus / 各 db 路径 / 权限层 / roots），不再有模块级全局槽。

### 修改

- `agent_core/modules.py`：只管生命周期与健康状态；`declared_tools` 由目录供给，
  模块自己不再重复声明工具名。
- `agent_tools/__init__.py`
  - 删 `BUILTIN_MODULE_MANIFESTS`（**§0.2 的 KeyError 随之消失**）。
  - 删 `_permission_layer` 全局槽与 `set_permission_layer`。
  - 删 `register_tools()` / `TOOL_BUILDERS`。
  - `discover_modules` 收进 `capability_runtime`。
- `agent_tools/modules.py`：通过显式构建上下文接收依赖。
- `agent_core/work_module.py`：删 `WORK_TOOL_NAMES` 依赖。
- `web_api.py:530-575` / `chat_cli.py:428-457`：都改为调 `capability_runtime` 的同一个构建函数。
- `agent_core/modules.py` 的模块构建失败路径：报"模块不可用 + 原因"，**不再伪造预期工具明细**。

### 新建测试

- `tests/test_capability_catalog.py`：目录是唯一真相（模块集合与工具集合各只有一处声明）。
- `tests/test_runtime_bootstrap.py`：Web 与 CLI 构建出**同一个**工具集（现在不是，见 §3.4）。

---

## 三、迁移执行调用点

### 3.1 `agent_core/loop.py`（✅ 已完成，全量 1550 全绿）

三处 `self.executor.execute(...)` → 网关：

| 行 | 场景 | 改成 |
|---|---|---|
| 1128 | 确定性只读路由 | `gateway.invoke`（origin=`chat_fastpath`） |
| 2183 | 模型自由调用 | `gateway.invoke`（origin=`chat_model`） |
| 2289 | 审批恢复 | `gateway.resume(approval_id, decision)` |

- `AgentLoop` 只负责编排消息、快照、模型续跑。删 `self.executor`（`loop.py:203`）。
- 事件改由网关统一发：动作开始 / 等待审批 / 完成 / 失败。删现在散在各处的
  `TOOL_STARTED`/`TOOL_COMPLETED` 手工 emit（`loop.py:1245-1259`、`2160-2197`）。
- `ToolResult` 降为"模型消息适配结果"：网关产出 `ActionObservation`，
  回填模型前经一个适配函数转成 `ToolResult.as_dict()`。这是**单向**的，
  不许反向（不许再用 ToolResult 当执行内核）。
- **`loop.resume` 的判定逻辑要跟着改**：现在靠"快照里最近一条 tool_calls + 比对 call_id"
  来定位该执行哪个工具（`loop.py:2270-2301`）。接入网关后应改成按 `approval_id` 取
  `ActionRequest`，快照只用于续跑对话。第二段判定留着会变成双链路。

### 3.2 `agent_tools/condition_tasks.py`（✅ 已完成，全绿）

- 删 `ToolExecutor().execute(...)`（`:377`），改为网关调用，origin=`scheduler`。
- `self.registry_provider()` 的用途从"自己 resolve 再自己执行"变成"查候选"。
- `allowed_tools` 白名单语义保留（`:326-330` 那条"只许 search__web"的校验继续有效）。

### 3.3 `agent_core/work_host.py` + `agent_core/local_worker_provider.py`（✅ 已完成，全绿）

- ~~四种 Work 任务注册为内部动作~~ → **实做成了五个内部动作**，比计划多一个，理由见下。
- Worker 的真实动作经网关（`local_worker_provider.py:115-119` 的四个分派改走网关）。
- Host 侧（`work_host.py`）**一行没改**：队列、账本、审批清单、重试、产物全部留在 Host。
  这是有意的 —— Host 的闸门留在 Host，网关只管"这一次调用"。
- 现有 `_HOST_OWNED_SUBMIT_KEYS` 的拦截保留为纵深防御，未动。

#### 为什么是五个而不是四个

计划写的是"四种任务 → 四个动作"。照字面做会把整个任务体（含 `emit` 事件、取消检查、
产物落盘）塞进 handler，而 handler 的入参只有 `(arguments, scope)`，
于是那些东西只能走 **arguments** 这条唯一的通道偷偷传 ——
把事件回调塞进"执行入参"，等于给一条本该封闭的通道开扇暗门。

所以拆的是**primitive（真正动东西的那一下）**，任务编排留在 worker：

| 内部动作 | 对应的那一下 | 只读 | 超时 |
|---|---|---|---|
| `workhost__file_list` | `files.list_files` | 是 | 60s |
| `workhost__file_preview_sort` | `files.preview_sort_by_extension` | 是 | 60s |
| `workhost__file_apply_moves` | `files.apply_moves` | **否** | 180s |
| `workhost__screen_capture` | 注入的截屏实现 | 是 | 60s |
| `workhost__volume_adjust` | 注入的音量实现 | **否** | 30s |

`file.organize` 因此是两个动作 —— **这个拆分正是重点**：
只有"执行移动"那一下需要过 Host 的审批闸门，"预览"永远可以先跑给用户看。

#### 为什么没塞进全仓那张注册表（与原计划的一处偏差）

计划 §1 写过"Chat 与后台共用同一个网关实例"。这里没有做到，三条理由：

1. **主注册表建完就冻结**（能力目录的前提），运行期再塞就是第二份真相重新长出来；
2. **处理函数绑在 worker 实例上**（允许根目录、注入的截屏/音量实现），
   塞进全仓共享的表等于让整个进程共用某一个实例的配置；
3. **`work__` 是 work 模块的命名空间**（唯一注册来源有守门测试），
   所以这里用 `workhost__` 前缀。

代价写清楚：worker 自带的这个网关**不带审计账本**，所以"同一个动作编号不跑第二次"
在这条链上不靠它 —— 靠 Host 的 `claim_attempt`（原子的），它本来就是 Work 那份耐用记录。

#### 必须补的一刀：`stale` / `unavailable` 要在动作层说清楚

`FileWorkService.apply_moves` 在源文件被改过时返回 `{"status": "stale", ...}`。
handler 若直接把这个字典交回网关，网关会因为"有 status 但没有 output"而把它
当成一整个普通返回值（=成功）—— `stale` 会静默变成成功。
现在 `_act_file_apply_moves` / `_act_volume_adjust` 自己先把业务词翻译成动作状态，
`stale` 与 `unavailable` 都已登记在业务状态词表里，会原样穿过并落到 failed。
两条新的守卫测试把它钉住了。

### 3.4 `agent_core/subagent.py`（按酥主设计）

- ✅ **子代理默认看不到 `subagent__dispatch`**，防递归派发（见下）。
- ✅ 通过目录注册，不再自己造 `ToolSpec`（`:335-343`）—— 见下。
- ⏳ 子代理的候选工具**重新按委派目标选择** —— 依赖 §5 的 `ToolSelector`，
  那一步才有"按目标挑工具"的能力；在那之前先做到"不直接继承主 Chat 那张表"。
- 权限继承澄清：酥主说得对，继承 `child_risk_policy` 后
  `FULL_ACCESS_EXCLUSIONS`（含 `work__approve`、`work__plan_submit`）继续拦住自我放行 ——
  它是**流程短路**不是危险评级，继承是安全方向。（本次未改，保持现状。）

#### 已做：子代理有自己的一张工具表

原来 `subagent.py` 把**父 registry 那个对象本身**传给了子 loop
（构造子 loop 处 + handler 闭包捕获处，两处都是同一个对象）。
后果是子代理看到的工具和主 Chat 一模一样，**包括 `subagent__dispatch` 自己**：
"子代理再派一个子代理"是模型看得见、并且真的会去调的，挡它的只有 `max_depth`
那一刀 —— 而且是在跑起来之后才挡。

看得见但跑不动比看不见更糟：模型会一轮轮试着调它，每轮撞一次墙，
把子代理那点步数预算（默认 10 步）全烧在明知不可能成功的事情上。

改法：`child_tool_registry(registry)` 当场给子 loop 造一张自己的表，
默认隐藏 `CHILD_HIDDEN_TOOLS = {"subagent__dispatch"}`；prompt 里列出的工具也用它
（说得出却调不动的工具比不说更糟）。`max_depth` 留着当第二道。

守卫：`tests/test_subagent_isolation.py`（两条 —— 数据层与"这份数据真的被用上了"）。

#### 已做：唯一注册来源收进能力目录

`web_api.py` 与 `chat_cli.py` 原先各调一次 `register_subagent_tool(...)`，
跟 work 2026-09-14 之前的问题一模一样，连后果都一样：

- Web 那条包在 `try/except` 里只记 warning —— "Web 上没有子代理"是**静默降级**，
  能力快照上看不出来；
- 两处传的参数不同（Web 显式复用 run_store / confirmation_store，CLI 不传
  → 走默认 `agent_core.db`），`AGENT_CORE_DB` 一改，两个入口连的库就岔了。

现在：新增 `agent_core/subagent_module.py`（`build_subagent_module`）与目录条目
`_subagent_entry`，`agent_core/subagent.py` 拆出 `make_subagent_tool`（**只造不注册**，
工具本地名 `dispatch`，由 `ModuleRegistry` 拼前缀）。
`register_subagent_tool` 降级成它的薄包装，只留给"手上已经有一张表"的场景（单测、
独立脚本），生产入口不许再用 —— 守门 `tests/test_subagent_registration.py`。

一个必须注意的点：`make_subagent_tool` 拿的是 **callable**（`registry_provider`）
而不是一张表。目录装配模块时全仓那张注册表还没建出来（它就是从模块产出拼出来的），
此刻固化一个表对象，等于让子代理拿着一张空表去干活。

### 3.5 Web/CLI 工具集分叉（**已拍板：两端都装 `TaskRouter`**）

`chat_cli.py` **完全没有 `TaskRouter`**：它只建了 `ModuleRegistry` + `tools_for`，
没有 `SQLiteRouteStore` / `TaskRouter` 的构造。Web 有（`web_api.py:624-625`）。
后果：CLI 上"结构化任务交给 WorkHost"这条路由不存在。

**拍板口径**（酥主 2026-09-21）：两端必须使用

- 相同的工具注册表
- 相同的 Work 根目录
- 相同的路由器构建方式
- 相同的失败策略

`TaskRouter` 是**本地核心组件**，不应像外部可选服务一样静默降级。
**构建失败要明确报告运行时初始化失败**（不是记一条 warning 继续跑）。
实现上归入 §2 的 `capability_runtime` —— 依赖从显式上下文来，两端跑同一个构建函数。

另外 `web_api.py:600-618` 的子代理注册外面套了 `try/except` 只 warning，
`chat_cli.py:460-469` 是硬失败。按同一口径：**子代理本轮定为可选能力，
两端都按相同方式降级并写入能力快照**（不再一边静默、一边硬失败）。

---

## 四、审批控制面与前端（✅ 完成）

### 新建 `agent_core/approval_coordinator.py`（✅ 完成）

统一查询与决策接口，Web / CLI 都按 `approval_id` 操作。聊天确认与 Work 审批
各自的持久化语义**保留不变**，协调器只提供单一用户入口。

对外词表只有 `confirmed` / `rejected`。Work 那套 `approved / rejected / expired`
由协调器翻译 —— `approved` → `confirmed`；`expired` **不接受**
（让外界能把一条审批标成"过期"，等于给了一条绕过拒绝的路，过期只能由账本按时间判）。

**先认领，再续跑**：聊天确认的认领是一条
`UPDATE agent_core_confirmations ... WHERE status='pending'` 加 rowcount 判断（`ConfirmationStore.decide` 本来就是原子的）。
反过来的话，两次几乎同时到达的点击会一起穿过"还是 pending"的检查，
那时只剩网关的原子登记在兜底 —— 而兜底不该是常态。

代价（已接受）：`AgentLoop.resume` 里原本只容忍"状态已经是 confirmed"，
现在要对称地容忍 `pending.status == decision`，否则**第一次拒绝**
会被判成"已经不是待确认了"，一次都执行不了。

### `web_api.py`（✅ 完成）

- 新增统一接口：`GET /api/approvals`、`GET /api/approvals/{approval_id}`、
  `POST /api/approvals/{approval_id}`（编号在路径上，体里只有 `decision` 一个字段；
  账号/会话走查询串，与 `/api/work/*` 其余路由同一约定）。
- 删 `/api/work/tasks/confirmed`（确认后直接提交那条路）。
- 迁移期代理：旧聊天确认接口（`.../confirmations`、`.../decision`）
  与旧 Work 审批接口（`/api/work/approvals/{id}`）都已改成转发到协调器，
  标了 `deprecated=True`，前端迁完即删。
- 协调器**每次现建不缓存**：它只持有两个引用、没有自己的状态，
  缓存它唯一的效果是多一个"持有过期存储/过期 Host"的机会，
  而 Work Host 是懒建的，那种过期一定会发生。
- 顺手删掉了一个**从来没有被赋值过**的全局 `runner`：旧 `/confirmations`
  里那句 `getattr(runner, "pending", None)(scope) if runner is not None else ...`
  永远走 else。留着一个只会读成 `None` 的全局，会让下一个人以为这是条活链路。

### `WorkHost`（✅ 完成）

- 新增 `get_approval(approval_id, scope)` 与 `pending_approvals(scope)`：
  前者的"取不到"与"不属于你"回同一个 None（分开能让调用方枚举别人的审批）。
- 删 `submit_web_confirmed`，并删掉 `submit` 的 `web_confirmed` / `source` 两个参数。
  这条豁免原本落在**请求里**（调用方自报"我已经问过用户了"），执行完不留任何
  可再查到的审批记录 —— 正是网关要收掉的那类旁路。
  `web_confirmed` 这个名字仍留在 `_HOST_OWNED_SUBMIT_KEYS` 里当别名一起挡。

### 前端（✅ 完成）

- `WorkPage.jsx`：删掉 `submit(..., confirmed)` 那个开关 —— 现在先正常提交到
  `/api/work/tasks`，服务端说要审批就按它返回的 `approval_id` 走统一接口；
  `quick()` 里的 `window.confirm` 之后由前端**替用户投这一票**。
  审批动作改打 `/api/approvals/{id}`（界面上的"批准"在这里翻成 `confirmed`）。
- `ChatPage.jsx`：改打 `/api/approvals?conversation_id=...`，只取 `kind === 'chat'`
  那部分（Work 审批在 WorkPage 上办）。
- `ConfirmationDialog.jsx`：按编号取那一条、按编号投票，不再拉整个会话的待办本地筛。

### 模型侧工具面收窄（✅ 完成，独立批次）

模型面前**恰好四个** work 工具：

| 工具 | 作用 | 授权 |
|---|---|---|
| `work__submit` | 提交单步任务 | low + `hard_routable` + 可确定性路由 |
| `work__plan_submit` | 提交多步计划 | high（排除表成员，任何模式都要人点头） |
| `work__status` | 统一查询（`kind` + 可选 `id` + 可选 `limit`） | 只读、low |
| `work__cancel` | 统一取消（`kind` + **必填** `id`） | high，**不可**确定性路由 |

- `status`：`kind` 必填（`task`/`plan`，吃大小写与首尾空白）；`id` 有则查单项、
  无则列一批。"没有这个编号"是**正常查询结果**（`found: false`），不是失败。
  `kind` 拼错时明确失败并回 `error_kind=permanent`，**不默认成某一种** ——
  默认成 task 的后果是"查了另一个东西还以为查到了"。
- `cancel`：缺 `id` 时**不猜"最近那个"**。猜的后果是用户说一句"取消"，
  停掉了一个他根本没提的任务。
- `approve` 整个从模型面消失。审批只能由 Web/CLI 用户走统一审批控制面。
- `make_work_plan_tools()` 删除；只留唯一公开入口 `make_work_tools(host)`，
  拆分实现用私有 `_submit_tool` / `_plan_submit_tool` / `_status_tool` / `_cancel_tool`。

同步清理的旧引用（不止 `agent_tools/work.py`）：

- `agent_core/routing.py`：删 `APPROVE_TOOL` 与那句"提交完再调 `work__approve`"的提示，
  改成"先向用户确认，再 `work__submit`；不要假定用户已同意"。
- `agent_core/permission_mode.py`：`FULL_ACCESS_EXCLUSIONS` → `{"work__plan_submit"}`。
- `agent_core/capability_catalog.py`：work 条目声明四工具，删掉
  `tool_visibility`（以前只有 `approve` 需要标 `internal`）。
- `agent_core/work_module.py` / `work_bridge.py`：文档注释。
- `README.md`、`tools/verify_permissions_live.py`。
- 前端 `permissionPresentation.js` 文案 + 其测试。
- `tools/append_permission_lesson.py` **不改**：它是 2026-09-20 那一课的一次性生成器，
  内容是当时的快照，改它等于篡改历史记录。真正的检查脚本是
  `tools/verify_permissions_live.py`（已改）。

遗留 `work__approve` 待确认记录的可执行性由
`tests/test_work_tools.py::test_a_legacy_approve_confirmation_cannot_execute_anything` 钉住：
不执行、不自动转换成新审批、模型拿到的回执带稳定错误码 `unknown_action`。

### 新建测试

`tests/test_approval_coordinator.py`（9 条）：跨作用域拒绝、重复点击只执行一次、
重启恢复、拒绝不执行、伪造授权字段无效、词表外的决定被拒、
以及两条端到端（真 loop 挂起 → 协调器确认/拒绝）。
`tests/test_web_api.py` 新增 4 条：统一列表合并两种来源、单一词表、
旧路由作为代理仍然可用、`/api/work/tasks/confirmed` 确实没了。
`tests/test_work_tools.py` 重写为四工具面（32 条），其中"精确四个"用 `==` 而非 `>=`。
前端新增 1 条（文案不再提 `work__approve`）。

---

## 五、工具筛选与旧链删除

### 新建

- `agent_core/tool_selection.py`：`ToolSelector` 返回本轮模型可见工具**及选择原因**；
  该结果同时是模型调用的**执行白名单**（模型伪造未公开工具名必须被网关拒绝）。
  - 纯聊天 → 0 个工具（**只约束主 Chat**，不含子代理）。
  - 明确领域 → 只公开该领域相关工具。
  - 无法可靠判断 → 记录原因 + 回退完整集合（保守策略，不以减数量为由隐藏能力）。
- `tests/test_tool_selection.py`、`tests/test_architecture_guards.py`

### 删除或停止暴露

- `register_tools()` 与 `TOOL_BUILDERS`
- 网易云 `search_tracks` 模型兼容工具
- ~~`make_work_plan_tools()` 兼容入口~~ → **§4 已删**
- ~~`work__approve`（模型工具面）~~ → **§4 已删**
- 旧聊天与 Work 审批接口（迁移完成后；现在还在做代理，标了 `deprecated=True`）
- ~~`/api/work/tasks/confirmed`~~ → **§4 已删**
- `ToolExecutor` 公共入口

### 架构守门测试（**分批**：每条跟着它所守的那一步落地）

**不一次写出一整套长期红灯。** 每个迁移阶段做
「一个守门测试变红 → 最小实现 → 变绿」，测试主干始终可信。

前三项跟着对应阶段落地：

1. 入口文件不得直接注册工具（`web_api.py` / `chat_cli.py`）→ 随 §2 落地。
2. 禁止手工造 `ToolSpec` 代替真实运行时构建（锁 §0.1 那类掩盖）→ 随 §2 落地。
3. Web/CLI 工具集一致 → 随 §3.5 落地。

**留到最后才加**（酥主拍板：最后一条执行路径迁完之后）：

4. 全仓禁止绕过网关 —— 网关之外不得实例化底层执行器或调用 handler。
   这条会一次性打红所有残余调用点，所以**必须等到 §3 全部迁完**再加，
   否则主干长期红着，判断不出后续失败是预期还是回归。

**验收口径诚实说明**：Python 没有语言级私有。第 4 条只能做到
「移除公共导出 + 全仓只有网关文件允许调用 handler + AST 静态检查 + CI 执行」，
做不到语言层强制。不要宣称更强的东西。

---

## 六、需要改写的现有测试

| 文件 | 改动 |
|---|---|
| `tests/test_capabilities.py` | 删静态 `declared-tools` 假设（`:341,348,351,356,357`）；`BUILTIN_MODULE_MANIFESTS` 引用（`:348,351,357`）随清单删除改写 |
| `tests/test_web_search_tool.py` | 删 `register_tools()` 三个兼容测试 |
| `tests/test_work_tools.py` | ✅ 已改写：从 9 个模型工具改为 4 个；删 approve 与兼容工厂测试 |
| `tests/test_work_host.py` | ✅ 已改写：删 `submit_web_confirmed` 流程（改成"连参数都传不进来 + 必须留下审批记录"） |
| `tests/test_routing.py`、`tests/test_routing_real_registry.py` | ✅ 已复核（真实命名空间注册表，未受影响） |
| `tests/test_web_api.py`、`tests/test_web_plan_api.py` | ✅ 已迁移统一审批接口 |
| `tests/test_permissions.py`、`tests/test_permissions_api.py` | ✅ 已改：排除表收成 `{"work__plan_submit"}` |
| `tests/test_route_guardrails.py` | ✅ 已改：提示词里不再出现批准工具 |
| `fronted/.../permissionPresentation.test.js` | ✅ 已改：文案与断言里不再有 `work__approve` |

**已结清**（原先这里列着"仍未做"的两项，2026-09-22 收尾时核实二者都不成立）：

- `BUILTIN_MODULE_MANIFESTS` / `register_tools()` / `TOOL_BUILDERS` 早已从
  `agent_tools` 消失 —— `tests/test_capability_catalog.py:47` 有一条
  `assert not hasattr(agent_tools, "BUILTIN_MODULE_MANIFESTS")` 在守着，
  `tests/test_web_search_tool.py:138` 也已按"目录取代主动登记"改写。
- `tests/test_capabilities.py` 的 work 部分已改为**从能力目录读预期名单**
  （`_work_tool_names()`），测试自己不再是第四份名单。

---

## 七、验收

```powershell
python -m pytest -q     # 写死解释器（见 §0.3），预期 1453 + 新增
npm test                # 24 文件 196 用例 + 新增
npm run build
```

再跑权限、路由、工作流三个矩阵。最终全仓扫描确认：原始 handler 只有网关能调用，
工具只能从能力目录注册。

---

## 八、需要酥主拍板的事项

### 已拍板并完成

1. ~~§0.1/§0.2 是否先单独修~~ → **已按拍板独立完成**：两个独立变更、各自先红后绿、分别验证。
2. ~~CLI 的 `TaskRouter` 缺失~~ → **拍板：两端都装**。相同的工具注册表 / Work 根目录 /
   路由器构建方式 / 失败策略；`TaskRouter` 是本地核心组件，**不许静默降级**，
   构建失败要明确报运行时初始化失败。详见 §3.5。
3. ~~执行顺序~~ → **拍板：坚持先红后绿，但不一次写出全部长期红灯**。
   每阶段"一个守门测试变红 → 最小实现 → 变绿"；
   **全仓禁止绕过网关的总守门测试留到最后一条执行路径迁完之后**才加。
   理由（酥主原话）：一开始同时放入十几个数周后才转绿的架构测试，
   会让测试主干长期失去可信度，也无法判断后来的失败是预期还是回归。
   止血的紧急程度改变**优先级**，不改变测试先行原则。

### 仍待拍板

无。§3.5 已由本次拍板结清（两端都装、不许静默降级）。

---

## 九、本次止血的边界与实际改动

**改了的**（只这 2 个源文件 + 2 个新测试文件）：

| 文件 | 改动 |
|---|---|
| `agent_core/routing.py` | 3 行：常量前缀、两处 hint 改用常量 |
| `agent_tools/__init__.py` | 3 处：`.get()` 兜底、清单补 weather、disabled 补 `else` |
| `tests/test_routing_real_registry.py` | 新建（7 条） |
| `tests/test_module_diagnostics.py` | 新建（13 条） |

**没做的**：

- 没有进动作网关改造（`CapabilityCatalog` 落地后要回来删 §0.2 的临时清单修复）。
- 没有新增文件编辑、终端、联网研究、脚本执行能力。
- 没有改进程模型。
- 没有动现有会话数据、Work 账本、任务记录和前端业务功能。
- 没有提交。`agent_core/routing.py` 等文件**从未进过版本库**（untracked），
  `agent_tools/__init__.py` 带着前若干轮的未提交积压（本次仅占其中几行），
  无法干净切分本次改动，等酥主决定提交策略。

**注**：本节记录的是**第一批止血**（§0.1/§0.2）当时的边界，其中"没做的：没有进动作网关改造"
等语句描述的是那一刻的状态。截至 2026-09-22，动作网关（§1–§4）已全部落地，
`CapabilityCatalog` 也已经是模块与工具声明的唯一来源。

原先这里挂着三条"后续影响"，2026-09-22 收尾时逐条核实，**三条都已结清，故删除**：

- `BUILTIN_MODULE_MANIFESTS` 依赖 → 该清单已删，`tests/test_capability_catalog.py:47` 有守门。
- `tests/test_capabilities.py` 漏 weather 的逐模块断言 → 已由能力目录的对账用例覆盖全部模块。
- 前端 `permissionPresentation` 硬编码 `work__approve` → 文案与断言都已改（§4）。

