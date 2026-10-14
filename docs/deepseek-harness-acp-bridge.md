# DeepSeek Harness ACP 桥接

## 一句话

主聊天提交的所有 `code.agent` 任务都交给本机安装的 DeepSeek Harness 跑
（模型推理 + 它自己的工具循环），不需要用户主动点名，也不需要显式 `provider`。
Host 仍然拥有任务身份、快照、验证、补丁、审批和用户可见状态。

```
主聊天 ──work__submit(task_type=code.agent)──────▶ WorkHost
                                                    │  队列 / 账本 / 取消 / 恢复
                                                    ▼
                                          DeepSeekHarnessProvider
                                                    │  ACP(JSON-RPC over stdio)
                                                    ▼
                                    DeepSeek Harness.exe --profile acp
                                                    │
                                          任务快照目录（唯一可写范围）
```

未指定 `provider` 和显式指定 `deepseek_harness` 的新任务都走 ACP 桥接。
生产注册表只有 `local-worker`（普通本机任务）和 `deepseek_harness`（代码任务）。
旧循环、工具、网络代理、预算和组装工厂保存在 `agent_core/legacy_code_agent/`，
不被 Web、CLI 或主聊天加载，也没有可选回退入口；显式选择 `code-agent` 会被拒绝。
旧执行者身份保留在历史任务中，不把旧会话或审批恢复成新的 Harness 会话。

## 启动方式

默认从已安装的桌面版推导，无需配置：

| 项 | 默认值 |
| --- | --- |
| 可执行文件 | `%LOCALAPPDATA%\Programs\DeepSeek Harness\DeepSeek Harness.exe` |
| ACP 入口 | `resources\app.asar\dsh\node_modules\@deepseek-ai\dsh-desktop-host\lib\cli.js` |
| 启动参数 | `--expose-internals <入口> --profile acp`，子进程环境 `ELECTRON_RUN_AS_NODE=1` |
| 子进程 cwd | 任务快照目录（Harness 的沙箱 workspaceRoot 就是 `process.cwd()`） |

环境变量（都可选）：

| 变量 | 作用 |
| --- | --- |
| `CODE_AGENT_ENABLED` | 是否开放代码任务，默认开；设为 false 关闭整个代码能力 |
| `AGENT_HARNESS_ENABLED` | 是否启用 Harness，默认开；关闭时代码任务明确失败，不启用旧代理 |
| `AGENT_HARNESS_INSTALL_ROOT` / `AGENT_HARNESS_EXECUTABLE` | 换安装位置或可执行文件 |
| `AGENT_HARNESS_ACP_ENTRY` / `AGENT_HARNESS_ACP_ENTRY_ARGS` / `AGENT_HARNESS_ACP_ARGS` | 换 ACP 入口与参数 |
| `AGENT_HARNESS_MIN_VERSION` / `AGENT_HARNESS_ALLOW_PRERELEASE` | 版本门槛（默认 `>=0.2.0`，允许 `0.2.0-rc.N`） |
| `AGENT_HARNESS_SANDBOX_MODE` | 沙箱模式，默认 `workspace-write`；`danger-full-access` 一律拒绝 |
| `AGENT_HARNESS_ALLOWED_SANDBOX_MODES` | 允许的模式白名单，默认 `workspace-write;read-only` |
| `AGENT_HARNESS_SANDBOX_STATE_ROOT` | 每次尝试的沙箱令牌文件目录 |
| `AGENT_HARNESS_PREFLIGHT_TIMEOUT` / `INIT` / `SESSION` / `PROMPT` / `CANCEL` / `CLOSE` | 各阶段超时（秒） |
| `AGENT_HARNESS_HOME` / `AGENT_HARNESS_ENV` | 传给子进程的 `DSH_HOME` 等额外环境变量（`k=v;k=v`） |

**真实模型调用需要子进程环境里有凭证。** 桌面版把凭证存在自己的凭证服务里，
命令行进程读不到；要跑真实推理，导出 `DEEPSEEK_API_KEY`（或用
`AGENT_HARNESS_ENV` 传进去）。没有凭证时任务会立刻失败，并且**保留 Harness
自己的错误原文**，不会假装成功。

## 失败关闭（fail-closed）

Harness 不在 `initialize` 里声明沙箱能力（实测只有 `mcpCapabilities` /
`promptCapabilities` / `sessionCapabilities`），所以"只接受 Harness 确认过的沙箱"
落实成：用同一个 `DSH_PERMISSION_MODE` 和同一个快照 cwd 先跑一次
`dsh --profile acp --dump-config`，要求它自己打印出的行里同时存在

- `sandbox-policy`：`mode` 取 `workspace-write`（或我们请求的模式），
  `workspaceRoot` 绑定 `process.cwd()`；
- `pwsh-sandbox`：没有被静态禁用；
- `approval`：策略不是"永久放行"。

任何一条读不到、对不上，或者 `--version` 读不出来/低于门槛、ACP `initialize`
协议版本不符、缺 `sessionCapabilities.resume`、快照目录越界、令牌文件写不出来 ——
**一律直接失败，绝不启动 Harness 进程**，也不存在"降级成普通宿主进程"的代码路径。

## 状态与权限

Provider 每次状态变化都会以 `provider_activity` 事件发一份快照，Host 先落库再广播：

`connecting` 连接中 · `thinking` 思考中 · `tool_running` 执行工具 ·
`waiting_permission` 等待授权 · `cancelling` 取消中 · `cancelled` 已取消 ·
`completed` 已完成 · `failed` 失败 · `disconnected` 已断开 ·
`unknown_result` 结果未知

写入 `provider_activity` 表，任务详情接口同时给出顶层（最新 attempt）和每个
attempt 的投影；网页刷新后重新读的就是这份持久化数据，SSE 只负责提示刷新。

权限请求（`session/request_permission`）的判定顺序：

1. 出现 `http(s)://` 等网络标记 → 拒绝（`scope=network`）；
2. 命令里出现 `netsh` / `reg add` / `schtasks` 等系统级标记 → 拒绝；
3. 工具调用里带绝对路径且不在任务快照内 → 拒绝（`scope=outside_snapshot`）；
4. 带路径且都在快照内 → 放行（`scope=snapshot`）；
5. 一个路径都没带 → 放行，但记 `scope=unverified`，并把结果可信度降到 `assumed`。

Harness 自己的沙箱仍然是执行隔离的边界；桥这一层只做"能不能证明它在范围内"。

## 长任务、取消与恢复

- 取消走 `session/cancel`；只有拿到 `stopReason=cancelled` 才算确认（`cancelled`）。
  超时或进程断连 → `unknown_result`，`cancel_status="unknown"`，Host 把 attempt
  收成 `unknown_result` 而不是永远 `running`。
- 进程异常退出时保存最后事件、退出码和 stderr 尾巴（`provider_disconnected`）。
- 应用重启后：账本挑出"还是 interrupted、有 session id、状态不是未知、且没有更新的
  兄弟 attempt"的记录 → 由 Provider 生成恢复 checkpoint → 新 attempt 用
  `session/resume` 接回原会话并继续。
- 恢复失败、取消无确认、进程中途死掉 —— 全部是 `unknown_result`。
  **`unknown_result` 不允许自动重跑**：重试接口默认 409，必须显式
  `confirm_unknown=1`（网页上是「重新执行（结果未知）」按钮）。

## 怎么验证

```bash
# 协议层（模拟 ACP 服务，真子进程、真 ndjson），不需要安装 Harness
python -m pytest tests/test_harness_acp.py tests/test_deepseek_harness_provider.py -q

# Host 层：路由、投影、取消、未知结果、重启恢复
python -m pytest tests/test_harness_only_wiring.py tests/test_work_host_harness.py tests/test_work_api_harness.py -q

# 真实安装版 Harness（协议：initialize / session/new / resume / close）
$env:AGENT_HARNESS_LIVE='1'; python -m pytest tests/test_deepseek_harness_live.py -q

# 再加一次真实提示词（需要 DEEPSEEK_API_KEY，会产生费用）
$env:AGENT_HARNESS_LIVE_PROMPT='1'; python -m pytest tests/test_deepseek_harness_live.py -q -s
```

代码组织：`harness_config.py`（配置）→ `harness_sandbox.py`（失败关闭门禁）→
`harness_acp.py`（stdio JSON-RPC 传输）→ `deepseek_harness_provider.py`（会话、
更新、权限、取消、恢复）→ `code_task_lifecycle.py`（Host 的验证/补丁/审批链路）。
共用的结果与检查点放在 `code_task_contracts.py`，活动链路不依赖旧代理目录。

主聊天提示词路由验收见 [Harness 路由测试计划](deepseek-harness-routing-tests.md)。
