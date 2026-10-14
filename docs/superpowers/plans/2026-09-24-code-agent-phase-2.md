# 代码代理阶段 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现只能手动提交、在独立快照和真实 Windows 沙箱中修改并验证代码、最终只展示补丁而不回写原项目的后台代码代理。

**Architecture:** `CompositeWorkProvider` 将现有短任务与 `code.agent` 分派到独立 provider；`CodeAgentProvider` 建立快照并驱动精简的 `CodeAgentLoop`。文件、命令、知识和宿主网络工具统一经过 `ActionGateway`，命令落到阶段 1 的 `WindowsSandboxRunner`，审批通过持久检查点恢复。

**Tech Stack:** Python 3.12、asyncio、SQLite、现有模型 Provider、ActionGateway、Windows AppContainer、pytest。

## Global Constraints

- 阶段 2 只支持显式 `code.agent` 提交，不增加确定性路由或模型自动派单。
- 永远修改快照，不直接修改原项目；完成结果只展示补丁。
- 沙箱内不授予网络能力；网络访问只能经过宿主代理。
- 代码任务使用独立并发配额，不占满现有短任务 worker。
- 所有路径拒绝 UNC、设备路径和重解析点；隔离不可用时失败关闭。
- 所有工具动作经过 `ActionGateway`，来源固定为 `code_agent`。
- 审批恢复只能使用持久化的原始动作请求，不能接受调用方替换参数。
- 模型声称验证成功必须有对应命令的成功观察。
- 本阶段不提交、推送、发布或自动回写项目。

---

## 文件结构

- `agent_core/code_agent_contracts.py`：代码任务、预算、检查点和结果契约。
- `agent_core/composite_work_provider.py`：按任务类型分派 provider。
- `agent_core/workspace_snapshot.py`：安全复制、基线清单、差异与工件。
- `agent_core/code_agent_tools.py`：快照文件、命令、知识和网络工具规格。
- `agent_core/code_agent_network.py`：宿主 HTTP 搜索与读取代理边界。
- `agent_core/code_agent_loop.py`：精简模型工具循环、预算和证据规则。
- `agent_core/code_agent_provider.py`：Work 生命周期、快照、取消和审批恢复。
- `agent_core/work_host.py`：按任务类别使用独立队列和 worker 配额。

### Task 1: 代码任务契约与组合分派

**Files:**
- Create: `agent_core/code_agent_contracts.py`
- Create: `agent_core/composite_work_provider.py`
- Modify: `agent_core/work_provider.py`
- Test: `tests/test_composite_work_provider.py`

**Interfaces:**
- Produces: `CodeAgentBudget`、`CodeAgentCheckpoint`、`CodeAgentOutcome`。
- Produces: `CompositeWorkProvider(providers)`，实现 `execute`、`cancel`、`resume`。
- Extends: `WORK_TASK_TYPES` 与输入要求加入 `code.agent: ("project_root",)`。

- [x] **Step 1: 写失败测试**

覆盖重复 task type 拒绝、按类型分派、取消回到原 provider、恢复只交给支持恢复的 provider，以及缺少 `project_root` 的参数拒绝。

- [x] **Step 2: 运行 RED**

Run: `python -m pytest tests/test_composite_work_provider.py -q`  
Expected: 因模块和 `code.agent` 契约不存在而失败。

- [x] **Step 3: 实现契约和分派**

组合层建立不可变的 task type → provider 映射；执行时记录 attempt → provider。未知类型返回失败，重复声明抛 `ValueError`。

- [x] **Step 4: 运行 GREEN 并提交**

```powershell
python -m pytest tests/test_composite_work_provider.py tests/test_work_tools.py -q
git add agent_core/code_agent_contracts.py agent_core/composite_work_provider.py agent_core/work_provider.py tests/test_composite_work_provider.py
git commit -m "feat: add code task contracts and provider routing"
```

### Task 2: 安全工作区快照与基线清单

**Files:**
- Create: `agent_core/workspace_snapshot.py`
- Test: `tests/test_workspace_snapshot.py`
- Test: `tests/test_workspace_snapshot_security.py`

**Interfaces:**
- Produces: `WorkspaceSnapshotService(snapshot_root, allowed_roots)`。
- Produces: `create(attempt_id, project_root) -> WorkspaceSnapshot`、`cleanup(attempt_id)`。
- Snapshot 包含编号、原项目根、快照根和宿主侧清单路径。

- [x] **Step 1: 写复制和排除规则失败测试**

覆盖普通源码与未提交文件复制；排除 `.git`、`.env`、密钥、缓存、构建产物、虚拟环境和任务数据库；清单含相对路径、大小、类型和 SHA-256。

- [x] **Step 2: 写真实路径攻击失败测试**

覆盖允许根越界、UNC、设备路径、重解析点、复制途中出现联接、目标预先存在和清单替换；失败后清理不完整快照。

- [x] **Step 3: 运行 RED**

Run: `python -m pytest tests/test_workspace_snapshot.py tests/test_workspace_snapshot_security.py -q`  
Expected: 因服务不存在而失败。

- [x] **Step 4: 实现安全复制**

目录句柄验证最终路径，逐项 `lstat`，不跟随链接。临时目录完成后原子改名；清单放在模型不可写的宿主状态目录。

- [x] **Step 5: 运行 GREEN 并提交**

```powershell
python -m pytest tests/test_workspace_snapshot.py tests/test_workspace_snapshot_security.py -q
git add agent_core/workspace_snapshot.py tests/test_workspace_snapshot.py tests/test_workspace_snapshot_security.py
git commit -m "feat: create isolated code task snapshots"
```

### Task 3: 快照差异、补丁和保护边界

**Files:**
- Modify: `agent_core/workspace_snapshot.py`
- Test: `tests/test_workspace_diff.py`

**Interfaces:**
- Produces: `WorkspaceSnapshot.diff() -> WorkspaceDiff`。
- Diff 包含统一文本补丁、文件变化、二进制变化、总字节数和当前哈希。

- [x] **Step 1: 写失败测试**

覆盖文本新增、修改、删除、换行与编码；二进制只列元数据；保护文件明确标记；重解析点导致失败。

- [x] **Step 2: 运行 RED**

Run: `python -m pytest tests/test_workspace_diff.py -q`  
Expected: 因 `diff` 不存在而失败。

- [x] **Step 3: 实现差异生成**

只比较基线清单与当前快照，不调用 Git、不读取原项目现状；文本补丁使用相对 POSIX 路径。

- [x] **Step 4: 运行 GREEN 并提交**

```powershell
python -m pytest tests/test_workspace_diff.py -q
git add agent_core/workspace_snapshot.py tests/test_workspace_diff.py
git commit -m "feat: generate snapshot-only code patches"
```

### Task 4: 快照文件与沙箱命令工具

**Files:**
- Create: `agent_core/code_agent_tools.py`
- Test: `tests/test_code_agent_tools.py`
- Test: `tests/test_code_agent_tool_security.py`

**Interfaces:**
- Produces: `make_code_agent_tools(context) -> list[ToolSpec]`。
- 文件工具：`code__list`、`code__read`、`code__search`、`code__write`、`code__apply_patch`。
- 命令工具：`code__run`，调用 `WindowsSandboxRunner.run()`。

- [x] **Step 1: 写文件工具 RED 测试**

覆盖分页列目录、限长读取、搜索、原子写入和补丁应用；拒绝绝对路径、`..`、链接、状态目录和快照外路径；来源只允许 `code_agent`。

- [x] **Step 2: 写命令工具 RED 测试**

入口只能是配置中的 PowerShell、Python 或搜索工具；cwd 必须在快照；结果返回退出码、截断与终止原因；取消终止当前 Job。

- [x] **Step 3: 运行 RED**

Run: `python -m pytest tests/test_code_agent_tools.py tests/test_code_agent_tool_security.py -q`  
Expected: 因工具工厂不存在而失败。

- [x] **Step 4: 实现最小工具集**

每次打开前重新验证最终路径；写入使用同目录临时文件加原子替换；补丁拒绝重命名、Git 元数据和越界目标；命令直接构造 `SandboxCommand`。

- [x] **Step 5: 运行 GREEN 并提交**

```powershell
python -m pytest tests/test_code_agent_tools.py tests/test_code_agent_tool_security.py -q
git add agent_core/code_agent_tools.py tests/test_code_agent_tools.py tests/test_code_agent_tool_security.py
git commit -m "feat: add snapshot and sandbox code tools"
```

### Task 5: 对话知识检索与宿主网络代理

**Files:**
- Create: `agent_core/code_agent_network.py`
- Modify: `agent_core/code_agent_tools.py`
- Test: `tests/test_code_agent_context_tools.py`
- Test: `tests/test_code_agent_network.py`

**Interfaces:**
- `code__knowledge_search` 调用现有 `MemoryRetriever.retrieve(scope, query)`。
- `code__web_search`、`code__web_read` 调用 `CodeAgentNetworkProxy`。

- [x] **Step 1: 写知识作用域失败测试**

证明检索固定使用原提交对话 scope，调用方不能覆盖任何 scope 字段，输出受条数和字节限制。

- [x] **Step 2: 写网络边界失败测试**

拒绝非 HTTP(S)、用户信息 URL、私网/回环/链路本地/保留地址、DNS 重绑定、跨协议重定向、非文本类型和超限响应。

- [x] **Step 3: 运行 RED**

Run: `python -m pytest tests/test_code_agent_context_tools.py tests/test_code_agent_network.py -q`  
Expected: 因代理和工具不存在而失败。

- [x] **Step 4: 实现代理与工具**

连接前和每次重定向后重新解析并检查全部地址；请求固定无凭据、有限超时、有限重定向和有限正文，只返回规范化文本。

- [x] **Step 5: 运行 GREEN 并提交**

```powershell
python -m pytest tests/test_code_agent_context_tools.py tests/test_code_agent_network.py -q
git add agent_core/code_agent_network.py agent_core/code_agent_tools.py tests/test_code_agent_context_tools.py tests/test_code_agent_network.py
git commit -m "feat: add scoped knowledge and host network tools"
```

### Task 6: 精简 CodeAgentLoop

**Files:**
- Create: `agent_core/code_agent_loop.py`
- Test: `tests/test_code_agent_loop.py`

**Interfaces:**
- Produces: `CodeAgentLoop(model, gateway, tools, budget)`。
- Produces: `run(request, snapshot) -> CodeAgentOutcome`、`resume(checkpoint, decision) -> CodeAgentOutcome`。

- [x] **Step 1: 写循环失败测试**

覆盖文本完成、连续工具调用、工具结果回填、步骤/令牌/时间预算、取消、未知工具、永久失败、临时重试和等待审批检查点。

- [x] **Step 2: 写证据真实性失败测试**

没有成功命令观察时，模型声称测试通过仍标记 `unverified`；存在匹配的退出码 0 观察才允许 `verified`。

- [x] **Step 3: 运行 RED**

Run: `python -m pytest tests/test_code_agent_loop.py -q`  
Expected: 因循环不存在而失败。

- [x] **Step 4: 实现循环和检查点**

只接受结构化模型响应；每个动作经网关并追加观察。检查点保存消息、预算、快照编号、原始动作请求和观察摘要，不保存凭据。

- [x] **Step 5: 运行 GREEN 并提交**

```powershell
python -m pytest tests/test_code_agent_loop.py -q
git add agent_core/code_agent_loop.py tests/test_code_agent_loop.py
git commit -m "feat: add bounded code agent loop"
```

### Task 7: CodeAgentProvider、取消与审批恢复

**Files:**
- Create: `agent_core/code_agent_provider.py`
- Test: `tests/test_code_agent_provider.py`
- Test: `tests/test_code_agent_approval_resume.py`

**Interfaces:**
- Provider 能力固定为 `code.agent`，支持取消和恢复。
- Consumes: 快照服务、循环工厂、沙箱 runner 和工件目录。

- [x] **Step 1: 写执行生命周期失败测试**

证明创建快照、发事件、驱动循环、生成补丁工件并清理运行资源；原项目哈希不变；失败保留诊断结果。

- [x] **Step 2: 写取消与恢复失败测试**

取消通知循环并终止 Job。恢复必须绑定快照和原始动作；参数替换、快照缺失或编号不符均失败关闭；拒绝结果返回模型。

- [x] **Step 3: 运行 RED**

Run: `python -m pytest tests/test_code_agent_provider.py tests/test_code_agent_approval_resume.py -q`  
Expected: 因 provider 不存在而失败。

- [x] **Step 4: 实现 provider**

首次执行建立快照；审批暂停保留快照。终态生成补丁和 JSON 摘要工件后清理 AppContainer 与运行句柄；恢复只读 Host 检查点。

- [x] **Step 5: 运行 GREEN 并提交**

```powershell
python -m pytest tests/test_code_agent_provider.py tests/test_code_agent_approval_resume.py -q
git add agent_core/code_agent_provider.py tests/test_code_agent_provider.py tests/test_code_agent_approval_resume.py
git commit -m "feat: run and resume code agent work"
```

### Task 8: WorkHost 独立队列与通用审批检查点

**Files:**
- Modify: `agent_core/work_host.py`
- Modify: `agent_core/work_ledger.py`
- Test: `tests/test_work_host_code_agents.py`
- Test: `tests/test_work_host.py`

**Interfaces:**
- `WorkHost(..., max_workers=2, code_workers=1)`。
- 普通任务进入短任务队列，`code.agent` 进入代码队列。
- 审批记录持久化安全摘要，原始动作留在 Host-owned 检查点。

- [x] **Step 1: 写并发隔离失败测试**

代码 worker 被阻塞时普通短任务仍执行；普通 worker 上限不变；停止 Host 会取消两类 worker。

- [x] **Step 2: 写审批原子性失败测试**

检查点与审批同事务落库；批准创建后继 Attempt 并调用 `resume`；决定不能携带替换动作；重启仍可恢复。

- [x] **Step 3: 运行 RED**

Run: `python -m pytest tests/test_work_host_code_agents.py tests/test_work_host.py -q`  
Expected: 因双队列和通用检查点链不存在而失败。

- [x] **Step 4: 实现双队列和审批链**

队列选择只看持久 task type。worker claim 后，有检查点调用 `resume`，否则调用 `execute`；审批 payload 只展示安全摘要。

- [x] **Step 5: 运行 GREEN 并提交**

```powershell
python -m pytest tests/test_work_host_code_agents.py tests/test_work_host.py -q
git add agent_core/work_host.py agent_core/work_ledger.py tests/test_work_host_code_agents.py tests/test_work_host.py
git commit -m "feat: isolate code workers and durable approvals"
```

### Task 9: 运行时装配与手动提交入口

**Files:**
- Modify: `agent_tools/work.py`
- Modify: `agent_core/modules.py`
- Modify: `web_api.py`
- Modify: `chat_cli.py`
- Test: `tests/test_code_agent_wiring.py`
- Test: `tests/test_work_tools.py`

**Interfaces:**
- `work__submit` 的 task type 包含 `code.agent`。
- 启动时构建组合 provider、代码 provider、目录和代码 worker 配额。

- [x] **Step 1: 写装配失败测试**

显式提交可用、缺项目根被拒、旧任务仍走本地 provider、无新增硬路由、功能关闭时明确不可用。

- [x] **Step 2: 运行 RED**

Run: `python -m pytest tests/test_code_agent_wiring.py tests/test_work_tools.py -q`  
Expected: 因运行时未装配而失败。

- [x] **Step 3: 实现配置和装配**

读取受管运行时目录、快照目录、允许项目根、代码 worker 数和功能开关。缺配置只禁用 `code.agent`；隔离不可用不降级。

- [x] **Step 4: 运行 GREEN 并提交**

```powershell
python -m pytest tests/test_code_agent_wiring.py tests/test_work_tools.py -q
git add agent_tools/work.py agent_core/modules.py web_api.py chat_cli.py tests/test_code_agent_wiring.py tests/test_work_tools.py
git commit -m "feat: wire manual code agent submission"
```

### Task 10: 真实端到端验收与文档收尾

**Files:**
- Create: `tests/test_code_agent_e2e.py`
- Modify: `docs/code-agent-plan.md`
- Modify: `LESSONS_LEARNED.md`

**Interfaces:**
- Produces: 阶段 2 完成证据，不新增生产接口。

- [x] **Step 1: 写真实 Windows 端到端场景**

临时项目提交 `code.agent`，录制模型读取、修改快照、运行测试并完成。断言原项目不变、补丁一致、命令属于目标 AppContainer、沙箱联网失败、原对话可查询结果。

- [x] **Step 2: 验证审批拒绝替代路径**

模型请求需审批动作，拒绝观察返回模型后改用安全方案；批准恢复使用原参数；重启 Host 后仍可恢复。

- [x] **Step 3: 运行阶段测试**

```powershell
python -m pytest tests/test_composite_work_provider.py tests/test_workspace_snapshot.py tests/test_workspace_snapshot_security.py tests/test_workspace_diff.py tests/test_code_agent_tools.py tests/test_code_agent_tool_security.py tests/test_code_agent_context_tools.py tests/test_code_agent_network.py tests/test_code_agent_loop.py tests/test_code_agent_provider.py tests/test_code_agent_approval_resume.py tests/test_work_host_code_agents.py tests/test_code_agent_wiring.py tests/test_code_agent_e2e.py -q
```

Expected: 全部 PASS，真实安全用例不以普通进程替代 AppContainer。

- [x] **Step 4: 运行全量验收**

```powershell
python -m pytest -q
python -m compileall -q agent_core agent_tools web_api.py chat_cli.py
git diff --check
```

Expected: 全量无新增失败，编译与补丁检查退出码为 0。

- [x] **Step 5: 更新路线图和经验**

阶段 2 标成已完成，记录专项与全量基线；经验写明快照不是回写、沙箱网络与宿主代理分离、独立 worker 配额和审批绑定原始动作。

- [x] **Step 6: 提交**

```powershell
git add tests/test_code_agent_e2e.py docs/code-agent-plan.md LESSONS_LEARNED.md
git commit -m "docs: complete code agent phase 2"
```

## 完成条件

- `code.agent` 只能显式提交，现有路由不能自动创建任务。
- 代码任务只修改快照，原项目在成功、失败、取消和审批恢复后都不变。
- 文件和命令不能越出快照，命令使用真实 AppContainer，沙箱内不能联网。
- 知识检索只使用原对话 scope，网络只经过受限宿主代理。
- 代码 worker 与短任务 worker 的并发配额相互独立。
- 审批检查点持久、参数不可替换，拒绝结果能返回模型继续。
- 最终结果包含可信补丁和验证证据，不虚报测试通过。
- 阶段专项、全量测试、编译和补丁检查全部通过。
- `LESSONS_LEARNED.md` 已记录本阶段可复用经验。
