# 爱尔奎特 Agent（aierkt-agent）

一个自带**三层记忆**的 Agent 伴生系统：SQLite 作为唯一事实源，FTS5 全文索引 + Qdrant 向量库做混合检索；对话入口 `chat_cli.py`，工具通过 `agent_tools/` 即插即用，另配一个只读终端监视器 `monitor_console.py` 随时查看运行与记忆健康。

当前归档版本：**V0.1（0.1.0，开发快照）**。这表示保存当前成果，不表示所有闭环已完成或生产验收通过。整体状态、已知缺口与核对结果见 [V0.1 易懂交接报告](docs/V0.1-整体情况与闭环检查报告.md)。

许可：[MIT](LICENSE)。环境变量模板见 [.env.example](.env.example)。

```
┌───────────────────────┐        ┌─────────────────────────────┐
│   chat_cli.py         │        │  monitor_console.py         │
│   对话入口 / 并发输入   │        │  只读监视器（随时可开）       │
└──────────┬────────────┘        └──────────────┬──────────────┘
           │                                   │ 只读（mode=ro）
           ▼                                   ▼
┌──────────────────────────────────────────────────────────────┐
│  agent_core  核心包（无业务色彩）                               │
│  ├─ loop.py               Agent 主循环（工具调用 / 持久化运行） │
│  ├─ conversation_manager  并发对话排队                         │
│  ├─ memory_repositories   SQLite + FTS5 + Qdrant              │
│  ├─ memory_writes         写入：hot/warm/cold + 向量同步       │
│  ├─ memory_retrieval      三层 RAG：召回 → 重排 → 过滤精排     │
│  ├─ memory_filtering      过滤精排层（时间衰减 + 质量/去重）   │
│  └─ memory_logging        日志写文件（监控台展示）             │
└──────────────────────────────────────────────────────────────┘
```

## 目录结构

```
.
├── chat_cli.py            对话入口（中文控制台，自动 UTF-8）
├── monitor_console.py     外部只读监视器
├── agent_core/            核心包（loop、记忆、工具、持久化）
├── agent_core_adapters/   适配器（BigModel 嵌入、OpenAI 兼容对话、SQLite 运行时）
├── agent_tools/           即插即用模块工具包（当前含联网搜索，拔掉目录即卸载）
├── data/
│   ├── memory_records.db  记忆主库（SQLite，事实源）
│   ├── memory.log         记忆运行日志（监控台只读展示）
│   └── memory_vectors/    Qdrant 本地向量目录
├── agent_core.db          对话运行状态（Agent 断点续跑用）
├── docs/                  设计文档（记忆生命周期 / 检索 / 嵌入）
├── pytest.ini             测试配置（跳过 legacy_backend 归档）
└── tests/
    ├── *.py               本仓库测试（pytest 收集）
    └── legacy_backend/    归档：另一个项目的 backend 测试（不参与收集）
```

## 快速开始

```bash
# 1. 安装依赖（建议在虚拟环境里）
pip install -r requirements.txt

# 2. 配置 .env（项目根目录，仓库已有示例；缺了哪个功能就配哪个）
#    对话模型（OpenAI 兼容接口，当前接 Gemini 网关）
GEMINI_API_KEY=...
GOOGLE_GEMINI_BASE_URL=...     # 例：https://jizhiapi.site/v1
GEMINI_MODEL=gemini-3.7-flash  # 选不思考的型号，避免回复里带思考链
#   （对话三项为「懒检查」：不配也能启动，发消息时才报缺哪个变量）
#    聊天人设（可选；不配默认猫娘，例：AGENT_SYSTEM_PROMPT=你是一个傲娇助手）
# AGENT_SYSTEM_PROMPT=
#    记忆向量嵌入（智谱 BigModel，维度要和 Qdrant 集合一致，默认 2048）
ZHIPUAI_API_KEY=...
ZHIPUAI_EMBEDDING_MODEL=embedding-3
ZHIPUAI_EMBEDDING_BASE_URL=https://open.bigmodel.cn/api/paas/v4
ZHIPUAI_EMBEDDING_DIMENSIONS=2048
#    联网搜索（可选：不配则 web_search 工具自动禁用，不影响主程序）
BOCHA_API_KEY=...
#    Bing RSS 默认开启，和博查并行检索；设为 false 可关闭
BING_RSS_ENABLED=true

# 3. 启动
python chat_cli.py
```

### 多入口身份与统一出口

生产入口必须由认证层注入账号身份，不从用户文本推断。CLI 可用以下环境变量模拟一个已认证入口：

```text
AGENT_ACCOUNT_ID=your-account-id
AGENT_CHANNEL=wechat
AGENT_CONVERSATION_ID=chat-id
AGENT_DELIVERY_TARGET=unified
```

同一 `AGENT_ACCOUNT_ID` 的微信、网页等入口共享任务和待确认请求，但各自保留聊天历史。到点事件先写入 `AGENT_OUTBOX_DB` 指定的 SQLite Outbox；统一网关接入前只能保证“已进入待发送队列”，不能宣称已送达。

旧版使用 `test_owner` 的数据库需要在生产启动前显式迁移到首个正式账号：

```bash
python -m agent_core.migrate_account --account-id your-account-id \
  --db agent_core.db --db agent_tasks.db --db agent_condition_tasks.db
```

迁移按 `owner_id` 幂等执行，不删除原入口字段。未配置正式账号时只适合本地开发，生产启动应由宿主拒绝默认身份。

## 多模块架构（第一阶段已完成）

核心 Agent 进程负责对话、工具路由、运行状态和记忆；每个业务能力以 `ModuleSpec` 注册，工具使用命名空间（例如 `search.web`、`netease.search`），同名注册会在注册阶段报错而不是静默覆盖。

### Scope 隔离

`Scope` 的完整身份是 `owner_id + channel + conversation_id + module_id`：

- **runtime 状态**、**confirmation**、**run store**、**hot 记忆** → 按完整 scope 隔离
- **warm/cold 记忆** → 按 `visibility` 区分：
  - `shared`：用户偏好、身份、长期事实（跨模块可见）
  - `module`：播放队列、TTS voice、模块临时状态（仅当前模块可见）
  - `private`：单次会话临时数据（不持久化到 warm/cold）

### 检索阶段过滤

FTS 和 Qdrant 在查询阶段直接按 `module_id` / `visibility` 过滤，不是先全局召回再过滤：

```python
# FTS
WHERE (visibility = 'shared' OR (visibility = 'module' AND module_id = ?))

# Qdrant
query_filter = Filter(should=[
    FieldCondition(key="visibility", match=MatchValue(value="shared")),
    Filter(must=[
        FieldCondition(key="visibility", match=MatchValue(value="module")),
        FieldCondition(key="scope.module_id", match=MatchValue(value=scope.module_id)),
    ]),
])
```

### 已实现模块

- **search** (可选)：联网核实，含四个工具——网页搜索（博查 + Bing RSS 多源汇总）、B站搜索、B站视频详情、网页原文读取（Jina Reader，与 Agent-Reach 同款阅读服务）。普通网页搜索会并行查询两个独立来源，按网址去重并交替返回；单个来源失败时保留另一个来源，并在结果里标出来源和失败说明。B站后端首选 Agent-Reach 管理的 `bili` 命令行（bilibili-cli，路径用 `BILI_COMMAND` 固定），`bili` 未安装或调用失败时自动降级到内置 B站公开接口（输出里 `backend` 字段标明实际后端：bili-cli / web-api-direct；博查为 bocha）。失败三态可区分：`uninstalled`（工具未安装）、`timeout`/`unreachable`（渠道不可达）、`empty`（无可靠结果）。Agent-Reach 的 doctor 体检较慢（需逐渠道联网检查），走进程级 10 分钟缓存加后台刷新，搜索永远不等它；缓存未知时覆盖说明如实标「未知」。日常问答可直接检索，范围（平台、时间、地域）默认由系统决定；只有重度研究且范围明显影响答案时才追问一次（确认流程保留，回复"按全网，最近一年"即可生效）。结果带平台、发布时间与覆盖说明。未配置 `BOCHA_API_KEY` 时网页搜索缺席，B站与网页读取不受影响。若本机安装了 Agent-Reach，会读取 `agent-reach doctor --json` 检测零配置渠道并写入覆盖说明（执行路径可用 `AGENT_REACH_COMMAND` 指定）；渠道检查通过不代表搜索成功，以实际调用结果为准。登录态平台（小红书等）不会自动调用。

### 回答前搜索门（主聊天搜索改造）

主对话在候选回复展示前增加一道检查（`agent_core/search_gate.py`）：先做**意图分流**，把请求分成四类——`direct_chat`（闲聊、情绪、创作、本地操作）、`fact_search`（是谁/是什么/价格等事实）、`fresh_search`（最新/新闻/实时等时效）、`deep_search`（要求多来源、对比、调研）。规则能定性的请求零模型调用：闲聊一次搜索都不发，事实与时效问题先检索再回答；规则认不出的疑问句、以及候选回复自己在猜（"可能是…"/给了"X 是一个…"这类断言）时，才走一次轻量判定。判定需要核实而本轮没搜过时，系统经现有工具网关代为检索（网页多来源 + 预算内补查 B站），读取最相关的原文，再让模型依据证据重写；候选回复在通过检查前不推给前端（回复暂存），内部判定内容不展示。

**多来源与证据口径**（`agent_core/search_evidence.py`）：每次检索的结果都进统一证据集，逐条记录来源标识、域名、平台、发布时间、相关性、是否独立、冲突状态。判定规则是：同一域名只算一个独立来源；不同域名但同一篇稿件（标题/摘要高度重复）按转载合并；普通事实问题的目标是**至少 2 个独立来源**（`SEARCH_GATE_TARGET_SOURCES`）。停止条件是"覆盖达标 / 预算用尽 / 连续没有新增独立信息"，不再搜到一个结果就收工；热梗类问题还会要求本次新增一个别的平台（B站）。数字或"已发布/未发布"这类正反说法冲突时，证据集标出冲突并写进重写提示词，回答必须并列说明、不替用户选边。回答末尾附来源口径（几个独立来源、是否单一来源、是否有冲突）；一条来源都没有又给了确定说法时，回复会被改写成"暂时无法确认"，并明确这是没查到而不是不存在。

- 预算：每回合最多 3 次不同关键词检索、2 条原文/视频详情读取；检索次数用尽后屏蔽搜索工具并要求模型基于已有证据收尾。
- 开关与配置（.env）：`SEARCH_GATE_ENABLED=false` 整体关闭恢复旧流程；`SEARCH_GATE_MAX_QUERIES`（默认 3）、`SEARCH_GATE_MAX_READS`（默认 2）、`SEARCH_GATE_TARGET_SOURCES`（默认 2，独立来源目标数）；`AGENT_REACH_COMMAND` 指定 Agent-Reach 执行路径；`WEB_READ_PROXY` / `JINA_READER_API_KEY` / `WEB_READ_MAX_CHARS` 调整读取行为（系统代理自动跟随 Windows 设置）。
- 日志与观测：`agent_core.search_gate` logger 记录意图判定、每次检索（含新增独立来源数）、停止原因、证据覆盖与回答口径，事件都带 `turn_id`。指标聚合用 `agent_core.search_evidence.summarize_metrics(load_gate_events(日志路径))`，输出搜索触发率、平均独立来源数、单源回答率、多来源尝试率（`multi_attempt_rate`）与冲突率；真实链路验收脚本 `scripts/verify_search_gate_live.py`（用"梁文锋和梁文谷是什么"实测，并顺带验证闲聊回合零检索）。
- **netease**：网易云音乐，通过独立 NetEase Music MCP 提供登录、搜索、推荐、歌单、播放和播放器控制；副作用工具使用高风险确认
- **media** (✅ 第二阶段完成)：TTS/ASR 异步任务服务
  - 提交工具：`media.submit_tts`、`media.submit_asr` — 提交语音合成/识别任务，立即返回 `job_id`
  - 查询工具：`media.get_status` — 查询任务状态（queued/running/completed/failed）
  - 管理工具：`media.cancel`、`media.get_artifact` — 取消任务、获取媒体文件信息
  - **不阻塞 Agent turn**：任务在后台异步执行，不直接返回音频二进制
  - **Scope 隔离**：任务/artifact 按 `owner_id`/`channel`/`module_id` 隔离，跨模块/用户无法访问
  - **并发控制**：Semaphore 限制 worker 数量，支持任务取消和超时保护
  - **持久化**：SQLite (WAL 模式) + artifact 文件系统存储，支持过期清理

- **Host Work（文件 Worker 首个 Provider）**：主 Chat 和 Web 工作页都通过 Host 提交后台工作；Host 持有 Scope、状态、权限、SQLite 账本和恢复逻辑，`LocalWorkerProvider` 只执行受限的结构化文件查询与分类移动。
  - 提交：`POST /api/work/tasks`，立即返回 `202`、`work_item_id` 和 `attempt_id`
  - 查询：`GET /api/work/tasks`、`GET /api/work/tasks/{id}`；详情包含所有 Attempt、事件和审批记录
  - 控制：取消、重试、批准/拒绝移动；移动任务先生成持久化清单，批准后只执行清单中的文件
  - 恢复：`GET /api/work/tasks/{id}/events?attempt_id=&after_sequence=` 用于断线补发；Host 重启会把运行中 Attempt 标记为 `interrupted`
  - 范围：首轮只支持允许根目录内的文件查询、按名称/大小/修改时间排序，以及直属文件按扩展名分类；不接 Codex，不把任意 Shell 命令交给 Provider
  - 聊天入口（**单一注册来源**）：`agent_tools/work.py` 的 `make_work_tools(host)` 产出**恰好 4 个**聊天工具 ——
    `work__submit`（单步提交，`kind`-无关）/ `work__plan_submit`（多步计划）/
    `work__status`（统一查询，`kind=task|plan` + 可选 `id`）/ `work__cancel`（统一取消，`kind` + 必填 `id`）。
    work 模块（`agent_core/work_module.py`）是唯一注册点，CLI 与 Web 拿到的工具集完全一致，Web 只保留 HTTP 适配
    （`_resolve_work_host` 复用模块 runtime + `/api/work/*` 路由）。
    **2026-09-22 从 9 个收窄到 4 个**：`approve` 整个从模型面删掉 ——
    模型替用户批准高风险操作，会让"提交"与"审批"两道闸门互相抵消
    （`work__submit` 是 low 风险，本身不经过确认）。审批现在只能由用户走统一审批控制面
    （`agent_core/approval_coordinator.py` + `/api/approvals`）。
    查询与取消也不再分两套名字，差别落在一个 `kind` 参数上。
  - 聊天工具的身份口径：把 scope 投影成 **`owner_id`/`channel`/`conversation_id` 原样保留、`module_id` 固定为 `work`**，
    所以聊天提交的任务、计划、审批与 Web 工作页读写同一份账本；同时 Host 广播仍带完整四元组，回传桥才能把结果
    写回发起对话（而不是写回 work 模块自己的 scope）。
  - 授权口径：`work__submit` 是 low + `effect=persistent`（提交后台任务是持久动作，
    但硬路由已摘掉 —— 路由层最多给一条 SOFT 建议，不能替用户发起），
    `work__status` 是 `effect=read`，`work__cancel` / `work__plan_submit` 高风险、必须经用户确认，
    且两者都**不允许确定性路由直调**（那条路不经过用户确认）。
  - 显式 `AGENT_DISABLED_MODULES=work` 时聊天工具整体不可用（能力快照仍会列出这 4 个声明，标记 `module_disabled`），
    `/api/work/*` 与 `_resolve_work_host()` 的兜底行为不变。

- **Control Plan 跨任务编排**：在 `WorkHost + WorkLedger + LocalWorkerProvider` 之上加一层持久化的 `ControlPlan`，把一个用户目标拆成多个有依赖关系的 `PlanStep`，每个步骤仍然只是一个普通 `WorkItem`。
  - 计划入口：主 Chat 的 `work__plan_submit`（提交）+ `work__status{kind:"plan"}` / `work__cancel{kind:"plan"}`
    （由 work 模块统一产出，见上一节的"单一注册来源"）；Web 的 `POST /api/work/plans` 等（见下）
  - 契约与校验：`agent_core/control_plan.py` —— 拒绝未知 task_type、缺失依赖、循环依赖、重复 step_id、跨 Scope 输入、伪造审批字段（`approved_moves` / `authorization` / `authorized`）以及修改 `AGENT_WORK_ROOTS` 的输入
  - 持久化：`control_plans` / `plan_steps` / `plan_step_dependencies` / `plan_events` 四张表；计划整体一次性落库后才创建首个 `WorkItem`
  - 调度：`agent_core/plan_scheduler.py` —— 无依赖步骤先就绪，依赖全部完成后解锁；`stop` / `retry` / `skip` 三种失败策略；审批暂停整条链路，批准后从原步骤继续、拒绝则结束计划
  - 步骤输入只允许引用前置步骤的结构化输出（`${step:list-files.output.directory}`），引用由 Host 解析，Provider 看不到模板
  - 恢复：重启后运行中的计划标记 `interrupted`，未知副作用不自动重放；`POST /api/work/plans/{id}/retry` 才创建新的 Attempt
  - 事件：`plan.created` / `step.ready` / `step.waiting_approval` / `plan.completed` 等先入库再经 SSE 推送，断线可用 `?after_sequence=` 补发
  - 首个验收场景：整理下载目录 → 用户批准后移动文件 → 截图 → 音量调低 10%
  - 注意：`WorkHost.start()` 会把账本里 `running` 的 Attempt 与计划判为 `interrupted`（重启恢复的前提），
    因此 `chat_cli.py` 与 `web_api.py` **不要同时**指向同一个 `AGENT_WORK_DB`；
    需要同时跑两个入口时给其中一个设 `AGENT_DISABLED_MODULES=work`，或另指一个 `AGENT_WORK_DB`。

### 后续阶段

- **第三阶段**（下一步）：会话恢复、运行时事件总线、paused/HITL 状态锁
- **第四阶段**：生产优化（Qdrant server、模块热插拔、熔断降级）

详见：
- [第一阶段完成报告](docs/MULTI_MODULE_PHASE1_COMPLETE.md)
- [第二阶段完成报告](docs/superpowers/plans/PHASE_2_COMPLETION.md)
- [Control Plan 跨任务编排](docs/superpowers/plans/2026-09-12-control-plan-orchestration.md)

> 配置在 `agent_core/project_env.py` 里固定加载**项目根目录**的 `.env`，所以从任何工作目录启动都能读到配置。

## 网页聊天助手

网页是推荐的主要入口，使用独立的 `web` channel 和固定的爱尔奎特人物卡；`chat_cli.py` 保留为备用入口，使用自己的 CLI persona 和 `desktop` channel，两者不会共享短期会话历史或人物卡。

网页开场白自带 100 条普通问候预备池，在生成失败时提供稳定问候。生成过程可根据近期线索执行一次只读查询，网页等待上限是 31 秒，服务端预算是 30 秒；超时、失败或接口返回空内容时，会显示预备问候。

```bash
# 终端 1：启动 agentcore 网关（等待 /api/health 就绪）
python start_backend.py

# 终端 2：启动前端
cd fronted/arcueid_daily_50/fronteddd
npm run dev
```

打开 Vite 输出的地址即可在网页输入并接收回复。网页会把会话 ID 保存到浏览器本地存储，刷新后继续当前会话。

导航中的“情景对话”使用独立会话：先把想法交给真实模型驱动的情景分析师，和它讨论并持续改写提示词卡；卡片进入“待确认”后，点击“开始情景”才会切换到角色对话。它不读取网页聊天的跨会话记忆，刷新可回到当前情景。情景页始终按本地时间在 06:00 和 18:00 切换浅深色。

情景页的“提示词卡”可查看和修改本情景的说话风格与场景提示，修改从下一轮对话生效。角色对话使用情景卡中的人物设定，Chatbot 使用本情景的人设栏目和互动偏好，不固定套用主聊天的人物卡。输入框左侧可添加一张图片或文档，也可只发附件。支持 PNG、JPEG、WebP、GIF、PDF、DOCX、TXT、MD，单个文件最多 10 MB；文档最多读取前 2 万字，PDF 最多 30 页。扫描版 PDF 如无可选中文字，需先转换为文字版。附件只供当前情景使用，不进入主聊天循环；未发送的上传文件 24 小时后清理。

可将表情包放进项目根目录的 `data/scenario_stickers/`（支持 PNG、JPEG、WebP、GIF，单张不超过 5 MB）。文件名可写出表情含义供模型挑选；目录为空时仍正常回复文字。该目录中的文件属于本地数据，不随代码仓库提交。

如果前端提示代理连接失败，先运行 `python start_backend.py`；它会报告端口已占用、后端提前退出或健康检查超时。也可以直接访问 `http://127.0.0.1:8765/api/health` 查看运行状态。

### 一键启动（Windows）

`scripts/start_agent.py` 会按顺序把后端和前端拉起来，并在每一步给出明确结果。默认端口：后端 `127.0.0.1:8765`，前端 `127.0.0.1:5173`。

```powershell
# 启动（PowerShell）
powershell -ExecutionPolicy Bypass -File scripts/start_agent.ps1

# 换端口
powershell -ExecutionPolicy Bypass -File scripts/start_agent.ps1 -BackendPort 8801 -FrontendPort 5180

# 停止（只停本脚本记下的进程，不碰别人起的服务）
powershell -ExecutionPolicy Bypass -File scripts/stop_agent.ps1
```

退出码说明：`0` 成功，`1` 启动失败（细节看日志），`2` 端口被占用，`3` 缺必需的环境变量。日志写在 `logs/` 下（`backend.log` / `frontend.log`），进程号记在 `logs/backend.pid` / `logs/frontend.pid` —— `stop_agent.ps1` 只认这两个文件，所以不会去猜、也不会误杀别人的进程。

重复执行是安全的：如果后端已经在跑（`/api/health` 有响应），脚本会复用它而不是再起一个。

#### 开机自启（可选）

```powershell
# 注册（当前用户登录后延迟 20 秒启动，不需要管理员）
powershell -ExecutionPolicy Bypass -File scripts/install_agent_startup.ps1

# 改延迟
powershell -ExecutionPolicy Bypass -File scripts/install_agent_startup.ps1 -DelaySeconds 60

# 取消
powershell -ExecutionPolicy Bypass -File scripts/install_agent_startup.ps1 -Uninstall
```

计划任务名固定为 `AgentLocalStartup`，重复注册不会产生多条。它触发的是 `start_agent.ps1` 本身，密钥仍然从 `.env` 读 —— 不会写进脚本或任务参数里。

> 注意：在这台机器的沙箱环境里，后端进程会随启动它的 shell 一起被清理，所以"脚本报就绪后服务随即消失"属于环境限制、不是脚本缺陷。正常在桌面手动执行（或由计划任务触发）不受影响。

## 使用 chat_cli

- `/exit` / `/quit` —— 退出
- `/history` —— 查看本会话历史
- `/clear` —— 清空提示历史
- 回复生成期间可以继续输入，多轮消息按顺序处理（`ConversationManager` 排队）

启动后控制台会打印 `伴生已就绪。输入 /exit 退出，/history 查看本会话历史，/clear 清空提示历史。`

## 记忆系统

| 层 | 角色 | 说明 |
| --- | --- | --- |
| `hot` | 当前会话消息 | 最近对话，按完整 scope（owner/channel/conversation/module）存取；提示组装时按 token 预算截取 |
| `warm` | 跨会话记忆 | shared confirmed facts 可跨模块读取；candidate/private state 仅当前模块读取；FTS + 向量混合召回 |
| `cold` | 历史证据 | 被替换/归档的旧事实，仅历史类问题或温层无结果时参与 |

### RAG 完整链路（三层架构）

```
用户查询
   ↓
┌─────────────────────────────────────────────────────────┐
│ 第一层：召回层（Retrieval）                              │
│ • FTS5 全文检索（关键词匹配）                            │
│ • Qdrant 向量检索（语义相似）                            │
│ • RRF (k=60) 融合排序                                   │
│ 输出：30 条候选记忆（warm/cold 各自独立召回）            │
└─────────────────────────────────────────────────────────┘
   ↓
┌─────────────────────────────────────────────────────────┐
│ 第二层：重排序层（Reranking）                            │
│ • 按 confirmed 事实优先                                 │
│ • 按 scope 全匹配优先                                   │
│ • 按 RRF 分数 + confidence 排序                         │
│ 输出：排序后的候选列表                                   │
└─────────────────────────────────────────────────────────┘
   ↓
┌─────────────────────────────────────────────────────────┐
│ 第三层：过滤精排层（Filtering & Final Ranking）          │
│ • 时间衰减：EMA 指数衰减（半衰期 180 天）                │
│ • 质量过滤：RRF 分数 < 0.01 的直接扔掉                  │
│ • 长度过滤：太短（< 2 字符）或太长（> 800 字符）扔掉    │
│ • 去重过滤：embedding 相似度 > 0.85 的只保留高分者      │
│ • 最终精排：按时间衰减后的分数降序                       │
│ 输出：高质量记忆（warm 6+4 条，cold 5 条）              │
└─────────────────────────────────────────────────────────┘
   ↓
Token 预算分配（默认 1200 tokens）
   ↓
返回给 Agent
```

**实现位置**：
- 召回层 + 重排序层：[`agent_core/memory_retrieval.py`](agent_core/memory_retrieval.py) 的 `_hybrid_candidates()` 方法
- 过滤精排层：[`agent_core/memory_filtering.py`](agent_core/memory_filtering.py) 的 `FilteringRanker` 类
- 完整流程集成：[`agent_core/memory_retrieval.py`](agent_core/memory_retrieval.py) 的 `retrieve()` 方法

**关键参数**（可在 `FilteringRanker` 构造时调整）：
- `half_life_days=180`：时间衰减半衰期（天数）
- `min_score_threshold=0.01`：最低质量分数阈值（RRF k=60 时正常命中约 0.011~0.033，此值只杀几乎无命中的凑数匹配）
- `min_length=2`：最短字符数（中英文统一按去空格后的字符数计）
- `max_length=800`：最长字符数
- `duplicate_threshold=0.85`：去重相似度阈值（余弦距离）

- **检索详解**：`MemoryRetriever` 先取 hot，再对 warm/cold 做 FTS5 与 Qdrant 向量双路召回，经 SQLite 重新水合并校验授权后，用 RRF（k=60）融合排序，然后通过 `FilteringRanker` 做过滤精排，最后按层限额和 token 预算组装上下文。详见 [docs/memory-retrieval.md](docs/memory-retrieval.md)。
- **生命周期**：hot 超过 24h 迁移到 warm；warm candidate 连续 14 天未访问过期；冷数据保留 180 天；过期是"先标记 → 过审计期 → 先删向量再删主表"两阶段流程。详见 [docs/memory-lifecycle.md](docs/memory-lifecycle.md)。
- **嵌入**：向量用智谱 `embedding-3` 生成，写入对应层集合；失败自动重试（指数退避），超过 10 次进 dead_letter，由后台 worker 补偿。详见 [docs/memory-embedding.md](docs/memory-embedding.md)。
- **内容维护（新版，默认关闭）**：七类语义内容 + 记录自己的版本号；一个回合只由一个端处理（`MEMORY_PROCESSING_ENDPOINT`），提炼一次同时给出新建候选与对已有记录的更新建议，写入事务里核对现行版本，旧版本归档保留。三个开关、版本怎么读、失败与拒绝的区别、怎么自查都在 [docs/cloud-memory-usage.md](docs/cloud-memory-usage.md)。

## 设计备忘：记忆与任务的职责边界（未接入，仅记录决策）

> **当前状态**：任务只以 `user.task.current` 语义键的形式存在于记忆里（陪伴记忆维度之一）。本文记录的是**将来**把"任务工具"接进来时记忆与任务各司其职的边界，**现在不开展任务工具接入工作**。

**生活类比**：记忆 = 便利贴（记"TA 现在很烦""TA 在赶开题报告"，过期撕掉很正常）；任务工具 = 待办清单 + 闹钟 + 执行器（记"答应过要完成的事"，有状态、有负责执行的 worker，完成才划掉）。便利贴不该承担"必须提醒我"的职责，待办清单也不该被记忆过期误杀。

| 维度 | 记忆（`user.task.current`） | 任务工具（将来） |
| --- | --- | --- |
| 身份 | 语义键 `conflict_key`，同键只留 N=3 条 | 每个任务独立 `work_item_id` |
| 寿命 | 随记忆过期政策（`duration=session` → 3h~48h） | 自己的状态机，完成才划掉 |
| 职责 | 陪伴：接得住话茬、"知道用户最近在忙什么" | 承诺：真正去执行 / 到时提醒 |
| 执行 | 被动，无动作 | 后台 worker 主动执行 |

**"独立进程"的两层含义**：

1. **独立的状态生命周期**——任务不该被记忆的过期政策管着。"明天提醒我买水果"如果只是记忆里一条 `importance` 低的记录，48 小时就被清了，明天怎么提醒？任务系统里有自己的状态机（pending → running → completed / cancelled / blocked），不受记忆过期影响，这就是"独立于记忆的进程"。
2. **独立的后台执行进程**——一个真正去干活的 worker 读任务的计划步骤、逐步执行、更新步骤状态、最后置为终态；这不是跟用户对话的循环，是独立的后台流程。

**任务工具已有雏形（实现在另一个项目，本仓库只有测试）**：`tests/legacy_backend/test_task_planner.py` / `test_task_tools.py` / `test_task_worker.py`（归档自另一个项目的 `backend.task_*`，不参与本仓库测试收集）暴露了完整架构——`task_planner.should_track_work_item` 判断"值不值得跟踪"（一次性请求如"帮我查天气"不建任务）；`task_tools` 提供 `task_create/list/get/cancel`（写操作要授权、读操作放开）；`agent_runtime` 维护 work_item 账本（CAS 抢占、终态白名单）；`task_worker._process_work_item` 后台执行。

**关于"目标主题细分"**：曾担心 `user.task.current` 只留 3 条，不相干的目标（改简历 / 买咖啡 / 写周报）会互相挤掉；"按主题细分"（拆 `conflict_key` 或加 `metadata.topic` 让挤兑按主题分开）是只动记忆的笨招。任务工具治本：真正值得跟踪的目标直接 `task_create` 拿独立身份，`user.task.current` 退化成一张便利贴、只记"用户最近在忙：改简历"，一条就够。

## 系统日志（写文件，监控台查看）

记忆子系统的运行日志统一**写入 `data/memory.log`**，chat_cli 聊天时不刷屏；每次读取/写入发生了什么，在**监控台 `monitor_console.py` 的 "Memory Log" 面板**里查看（它只读该文件尾部，不改写任何东西）。日志默认级别 `INFO`。

格式：

```
2026-08-16 19:18:00,932 INFO agent_core.memory.retrieve 检索完成：hot=32 条 warm=0 条 cold=0 条 冷层启用=True 引用=0 预估token=276
2026-08-16 19:18:01,591 INFO agent_core.memory.write 写入完成：记录=1678b2fb 层=hot 状态=active 嵌入=就绪
2026-08-16 19:18:01,591 INFO agent_core.memory.agent 组装上下文：hot=32 条 warm=0 条 cold=0 条 引用=0
2026-08-16 19:18:02,010 INFO agent_core.memory.extract 提炼开始：用户输入 18 字，历史 4 条，助手回复 12 字
2026-08-16 19:18:03,122 INFO agent_core.memory.extract 提炼完成：共 1 条记忆 user.state.mood×1，token 消耗 输入=128 输出=64 总=192
```

| Logger 名 | 含义 |
| --- | --- |
| `agent_core.memory.write` | 写入/向量同步/维护任务（写入完成、嵌入同步、维护完成） |
| `agent_core.memory.retrieve` | 检索结果（检索完成 hot/warm/cold…） |
| `agent_core.memory.filtering` | 过滤精排层（质量过滤、长度过滤、去重过滤、最终精排） |
| `agent_core.memory.agent` | 每次对话组装进上下文的记忆（组装上下文、助手回复已持久化） |
| `agent_core.memory.extract` | 记忆提炼（提炼开始/提炼完成/提炼调用失败） |

**日志示例（过滤精排层）**：

```
INFO agent_core.memory.filtering 📥 过滤精排开始：输入 5 条候选
INFO agent_core.memory.filtering   🔍 质量过滤：5 → 4（过滤掉 1 条低分）
INFO agent_core.memory.filtering   📏 长度过滤：4 → 3（过滤掉 1 条长度异常）
INFO agent_core.memory.filtering   🧹 去重过滤：3 → 2（过滤掉 1 条重复）
DEBUG agent_core.memory.filtering    🔄 替换重复记忆：id=123 (分数 0.876) 替换 id=456 (分数 0.654)，相似度=0.912
INFO agent_core.memory.filtering   🎯 最终精排：按时间衰减分数排序完成
INFO agent_core.memory.filtering ✅ 过滤精排完成：5 → 2（总过滤率 60.0%）
```

**为什么日志进了文件**：`agent_core/memory_logging.py` 的 `configure_memory_logging()` 会给 `agent_core.memory` logger 挂一个 `FileHandler`（UTF-8 编码，默认写 `data/memory.log`）。`MemoryWriter`、`MemoryRetriever`、`FilteringRanker`、`MemoryAgentSession`、`MemoryExtractor` **构造时自动调用它**，日志始终就绪。chat_cli 一创建这些对象，日志就开始写入文件（提炼层目前独立运行、未接入主循环，用到 `MemoryExtractor` 时才写提炼日志）。监控台每轮刷新读该文件尾部展示。

**提炼层的动态 token 预算**：`max_tokens` 限制的是「推理 + 正文」的总输出预算。`deepseek-v4-flash` 这类推理模型单轮思考量不固定（实测 111～512+），一旦推理占满预算，`message.content` 就会空手而归。`MemoryExtractor` 从 `EXTRACT_MAX_TOKEN`（默认 512）起步：只有检测到「正文为空 **且** 输出 token 顶到本次预算」这一明确的截断信号时才 `×2` 加码重试（512 → 1024 → 2048），普通轮次一次通过、不白花 token，极端情况最多加码 2 次，超上限（2048）即停。

**中文不乱码**：chat_cli 启动时会把 stdout / stderr 统一重配置为 UTF-8（`_ensure_utf8_stdio`），所以中文内容在控制台正常显示。

## 外部监视器 monitor_console

只读终端面板：实时展示 Agent 运行（phase/steps/tokens）、待确认操作数、记忆分层与嵌入状态（含失败/死信数）、SQLite / FTS5 / 向量库健康检查，以及一个 "Memory Log" 面板滚动展示最近的记忆运行日志（读 `data/memory.log` 尾部）。以 `mode=ro` 的 SQLite URI 连接，**只读不改写任何数据库**。

```bash
# 直接运行即可（默认已指向 data/ 下的三个数据源）
python monitor_console.py

python monitor_console.py --once    # 只渲染一帧后退出
```

参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--state-db` | `agent_core.db` | Agent 运行状态库 |
| `--memory-db` | `data/memory_records.db` | 记忆主库路径（与 `memory_repositories` 默认一致，开箱即读） |
| `--memory-log` | `<memory-db 目录>/memory.log` | 记忆运行日志路径（默认跟随 `--memory-db` 所在目录） |
| `--vector-path` | `data/memory_vectors` | Qdrant 本地目录（与 `memory_repositories` 默认一致，开箱即读） |
| `--refresh-seconds` | `2.0` | 刷新间隔 |
| `--stale-after-seconds` | `15.0` | 超过该秒数未更新且非终态的 run 标记为 STALE |
| `--once` | — | 单帧渲染后退出（便于脚本/CI 采集） |

## 可插拔工具包 agent_tools

工具放 `agent_tools/` 目录，通过 `register_tools(registry)` 注册，**任一工具 import/构建/注册失败或未配置都优雅跳过，绝不影响主循环**。删掉整个目录，主体行为与现在完全一致。

当前内置工具：

- `search__web` —— BoCha 联网搜索（`BOCHA_API_KEY`），未配置 key 时禁用 `search` 模块并跳过。
- `netease__status`、`netease__login`、`netease__search`、`netease__daily_recommend`、`netease__my_playlists`、`netease__playlist_detail`、`netease__album_info`、`netease__artist_info`、`netease__my_subscriptions` —— 通过外部 NetEase Music MCP 提供的登录状态、扫码登录、目录查询和收藏读取能力。
- `netease__create_playlist`、`netease__add_to_playlist` —— 歌单写入，标记为高风险，会进入确认流程。
- `netease__play`、`netease__next`、`netease__pause`、`netease__resume` —— 播放器运输控制，直接执行、不打断（点歌是连续动作，每一下都弹确认没法用）。
- `netease__get_volume` —— 读取网易云应用音量，只读。
- `netease__set_volume` —— 修改网易云应用音量，标记为高风险，需先确认（与系统音量同一口径）。
- `netease__player_status` —— 读取本机网易云播放器状态。
- `netease__search_tracks` —— 兼容旧版 provider 的只读目录查询工具。

网易云模块通过 `agent_tools/netease_cloud.py` 启动独立目录中的 MCP 服务：

```text
uv.exe run --locked --directory <你的 MCP 目录> python -m cloud_music_mcp
```

MCP 自己管理 `uv.lock` 和网易云 cookie；自建 Agent 不复制依赖，也不把登录凭据写入本项目。Web 入口启动 `web_api.py`，CLI 入口启动 `chat_cli.py` 后会自动发现并注册该模块。

### 浏览器定位与高德导航（可选）

配置服务端环境变量后启用：

```bash
AMAP_WEB_SERVICE_KEY=your-amap-web-service-key
```

启用后会注册 `location__current` 和 `navigation__route`。前端聊天页的“共享位置”开关必须由用户主动开启并授权浏览器定位；位置只保存在服务进程内，5 分钟无更新自动过期，不写入记忆或 SQLite。

天气能力使用同一份 `AMAP_WEB_SERVICE_KEY` 注册为 `weather__query`。浏览器只上报 HTML5 Geolocation 的 WGS-84 坐标和 `accuracy_m`；坐标留在服务端内存中，经高德逆地理解析为行政区 `adcode` 后查询实况或预报。模型和对话记录不会收到原始经纬度、门牌或 POI。天气结果是行政区级数据，回复会明确查询区域；定位精度较差时只显示城市或区县范围。

```text
导航到上海虹桥站
从我现在的位置步行导航到人民广场
```

路线工具支持 `driving` 和 `walking`，返回距离、预计时间及高德地图链接。浏览器定位要求使用 `localhost` 或 HTTPS 页面。

**导航失败只返回稳定错误码。** 工具结果形如 `{status, output: {reason}, error, error_kind}`，其中 `reason` 是固定词表里的一员；高德的 HTTP 状态码、API 返回片段、异常类名只进日志，不进结果、不进聊天历史、不进气泡。`error` 填的也是这个码本身（它会被回填给模型，所以同样不能是原文）。

对话侧的措辞由本地模板按码给出，不让模型基于原始错误自由解释：

| reason | 用户看到的话 |
| --- | --- |
| `location_unavailable` | 我还没有拿到你的当前位置。打开位置共享后，我再给你规划。 |
| `location_stale` | 我这里的起点有一会儿没更新了。重新共享位置后，我再继续。 |
| `destination_required` | 你还没说要去哪儿。告诉我目的地，我马上给你规划。 |
| `destination_invalid` | 这个目的地我读不出来。换个更完整的写法，我再找。 |
| `destination_not_found` | 这个目的地我没定位准。换一个更完整的名称或地址，我再找。 |
| `walking_range_exceeded` | 这段路超出了步行规划范围。我可以改成驾车路线再看。 |
| `navigation_unavailable` | 这次路线服务没有响应。我保留了目的地，稍后可以接着查。 |

导航**成功**时结果里保留距离、时长、方式和地图链接，起点/终点的精确经纬度只活在工具内部（地图链接里已带），不进入结果 —— 否则模型会把它们复述进气泡。

## 权限矩阵与只读快路径

### 一次模型都不调的只读查询

请求命中确定性分岔后，结果由本地模板直接成句，**零模型调用、零过程气泡**：

| 用户说 | 出口 |
| --- | --- |
| 我在哪儿 / 我的位置 | `location__current` |
| 你在哪儿 / 你住在哪 | `system__local_info {kind: host}` |
| 现在几点 | `system__local_info {kind: time}` |
| 我的任务列表 / 有哪些任务 | `tasks__list` |
| 有哪些条件提醒 | `conditions__list` |
| 现在在放什么歌 | `netease__player_status` |

正式模块产出的 list 工具（`make_task_tools` / `make_condition_task_tools`）必须自带 `effect=read` **且** `deterministic_route_allowed=True`，否则会静默回落给模型。`tasks__get` 只标 `effect=read`、不开放确定性路由 —— 它需要具体的 `task_id`，只有模型能从 list 结果里拿到。

### 工具效果三值（`ToolSpec.effect`）

每个工具必须显式声明"这个动作会在世界上留下什么"，取值三选一（定义在 `agent_core/contracts.py`）：

| 值 | 含义 | 超时后怎么算 |
| --- | --- | --- |
| `read` | 什么都不改 | `failed`，重读一遍即可 |
| `transient` | 改了，但改错了再改回来就行（音量、播放/暂停/下一首） | `failed`，重读状态即可 |
| `persistent` | 留下了活过这次调用的东西（文件、任务、提醒、歌单、会话） | `outcome_unknown` —— 需要人工对账，不能重放 |

默认值是 **`persistent`（最严的一档）**：写代码时忘了标注，工具会落在"要复核"这一侧，而不是被当成只读放宽。注册时 `ToolRegistry` 强校验 —— `deterministic_route_allowed=True` 的工具必须 `effect=read`（或显式 `hard_routable=True`），因为那条路不经过模型也不经过用户。

确定性路由（不调模型直接执行）的准入条件就从这一个字段读：`agent_core/loop.py` 只放行 `effect == EFFECT_READ` 且 `deterministic_route_allowed=True` 的工具。

三值分开之后，"低风险、不需要确认、但会在世界上留下东西"这一格第一次成了**能被指出来**的集合。

**复核（`agent_core/consistency_review.py`）**在权限判定之后、真正执行之前，对 `persistent` + `risk="low"` 的动作做一次独立一致性复核：判据是一张声明式的表（`REQUIREMENTS`），说明某个工具的哪几个参数必须带上、哪几个必须是绝对路径；答不上来就回一个稳定码把这次调用拒掉，让模型回去问用户。同一条复核在**审批恢复**（`ActionGateway.resume`）上也装了 —— 恢复同样落到执行，现在它是空转（能走到"等审批"的动作都是 `risk="high"`），但不能因此不装。现在表里只登记了阶段 2 的 `code__agent`（要求带明确 `workspace_root`，以及说得出要改什么的 `objective`）。

**表里没登记的放行（fail-open）** —— 复核是新增的一层，把"没登记"当拒绝会把现存的持久工具当场堵死。代价是"忘了登记"和"决定不登记"在代码里长得一样，所以第二张表 `REVIEW_WAIVER` 显式记下后者：现存的 4 个低风险持久工具逐个写了豁免理由（`netease__login` / `media__submit_tts` / `subagent__dispatch` 参数形状已由 schema 与统一非空白检查覆盖；`work__submit` 的契约按任务类型而变，属 Work 领域、由工具级回调执行）。覆盖面由 `tests/test_consistency_review.py` 守着：现存低风险持久工具必须**逐个**在两张表里有名字，且表里登记了却不存在于注册表的名字只允许提前登记的契约那一个。

### 参数的两道结构校验（schema 之后）

JSON Schema 只管类型与"字段在不在"，有两件事它表达不了，于是 `ActionGateway._validate_structure`（第 ④ 步，在权限判定之前）补上：

1. **`required` 里的字符串不能全是空白。** `"   "` 满足 `type: string`，加 `minLength: 1` 也照样过 —— 它有长度，只是没有内容。判据是 `contracts.is_blank` 的**唯一一份实现**（网关、一致性复核的判据表、Work 的任务输入契约三处共用；各写各的 `not value.strip()`，`"  "` 就会在一处算没给、在另一处算给了）。空值判据只对字符串按"非空白"处理，**数字项不套它** —— `percent=0` 是"静音"而不是"没给"。
2. **跨字段关系。** 最典型的是 `work__submit`：`input` 该带哪些字段由 `task_type` 决定。这属于领域知识，所以不在网关里写一张"所有工具的跨字段规则"表，而是给 `ToolSpec` 加一个可选的 `argument_validator` 回调，由工具在注册时交出（网关不因此认识任何具体工具）。回调返回 `ToolArgumentViolation(error, error_kind, output)`，`output` 只放字段名与枚举取值，不放参数原文。回调**自己抛异常或返回错类型**一律按"工具坏了"处理（`argument_validator_failed`），不按"调用方给了坏参数" —— 两种问题的归属不同，混成一个码会让排障一直往参数上找。

`WORK_TASK_INPUT_REQUIREMENTS` 是"哪种任务要哪些输入"的唯一来源，住在 `agent_core/work_provider.py`（Work 领域）。`agent_core/routing.py` 只**读**它（`_required_input`），用来判断"这句话够不够格生成一条带现成参数的建议" —— 读到的是事实，不是执行权：建议永远只是建议，没有执行路径。

**这条检查不装在 `WorkHost.submit()` 上**，是查过之后才这么定的：进到 provider 的 `task_input` 取的是 `input or context`，在提交口只看得见一半输入；而且审批的判据只有一个（这个 task_type 要不要改本机状态），**与参数齐不齐无关** —— 参数没给全的提交照样要走到审批那一步让用户看见。`tests/test_work_tools.py` 里有一条用例专门钉住"提交口不许按参数完整性拒绝"，红了只有一种可能：有人把它加回来了。

### 变更类操作的确认矩阵

| 操作 | 权限 |
| --- | --- |
| 查询播放器、查询音量、查询任务/条件提醒 | 直接只读 |
| 播放、暂停、继续、下一首 | 直接执行 |
| 设置网易云音量 | 等待确认 |
| 调整系统音量 | 等待确认 |
| 创建歌单、添加歌曲 | 等待确认 |
| 取消语音任务 | 等待确认 |
| 整理文件、截屏、把音量写进计划步骤 | 等待确认 |

两条硬规则：

- **网易云音量 ≠ 系统音量。** 句子里点名播放器的音量不会被判成系统音量任务（`agent_core/routing.py` 的 `_APP_VOLUME_CONTEXT`）—— 否则用户会为了一个他没要的操作去点一次确认。
- **审批是 Host 的边界。** `WorkHost.submit()` 只认"这次提交是不是 Web 端逐项确认过的"，不看 `source` 字符串；`_requires_approval`、`approval_granted`、`approved_moves`、`approved`、`authorization`、`web_confirmed` 等字段出现在 `input` 或 `context` 里一律拒绝。计划（Control Plan）里的截屏/音量步骤由校验层**强制**打上确认标记，计划自己写 `requires_approval: false` 不算数。

### 任务终态回执

定时任务的完成/失败/取消在对话里说什么，只由 `agent_core/task_result_bridge.py` 一个出口决定：失败与取消是固定句（不含原因），完成句后面最多跟一句已确认的业务输出；内部诊断只进日志与 TaskStore。回执带的 `kind="task_result"` 与 `work_result`、`steward_progress` 一样**不进入长期记忆提炼**，但保留在当前对话上下文里。

事件分两层：`task.completed` / `task.failed` / `task.cancelled` 是**业务终态**，只供内部服务消费（回执桥、任务存储等），data 里带内部诊断，不跨浏览器边界；`task.result` 是回执桥**写库成功后**发出的**聊天刷新通知**，只带 `task_id` / `run_id` / `status` / `result_code` / `history_written` 五个字段。Web SSE 与前端只订阅 `task.result`，收到即重新拉一次历史 —— 一个终态在浏览器侧只产生一次刷新。

### 过程阶段通知（普通查询与补查共用一套规则）

长回合里聊天框会按**真实阶段**说一句短话：正在查、取得材料、正在读原文、取得正文、换来源。判断"该不该说、隔多久说、还能说几句"全部收在 `agent_core/steward_progress.py`，业务流程只报告发生过的动作：

- 阶段来自工具结果的**结构**（`results` / `items` 这类材料列表、`fetch_status`、`content`），不认自由文本：只有嵌套材料列表、没有顶层标题也算拿到材料；只有查询词、零条结果、失败状态都不算证据；结构不认识就什么都不说。
- 时间门槛只影响密度，不影响真假：首次查询等待满 1.5 秒才说，相邻两条至少隔 4 秒，间隔内的新进展覆盖待发旧进展；一次聊天回合最多一条首次通知 + 两条中段，等待确认之类的必要消息不受限。
- 措辞由**模型现场生成**：把"目标摘要、当前阶段、确实会执行的下一步、可公开的事实摘要、最近几句已发过程句"交给措辞生成器（`agent_core/steward.py` 的 `StagePhraseGenerator`），由它按人物语气写一句。工具名、文件路径、命令、异常、正文全文和模型推理都不会进入提示词；生成结果仍要过公开文字校验，并和最近三条已发句做重复检查。
- 生成在**后台任务**里跑（短超时），主任务只把阶段排进队列、不等措辞；生成失败、超时、不合格或与近期重复 → 这一条**直接不发**，不回退任何固定句（完成、失败、取消、等待确认这些必要通知仍走原有兜底模板）。气泡里不含查询词、标题、网址、路径或内部错误。
- 关闸顺序：交付最终回答/取消/失败之前先 `close()`：取消还没生成完的那句、不再接新通知，并等已经在落库路上的那条收口 —— 所以刷新前后顺序一致，最终回答之后不会再冒出旧进度。旧回合的待发消息不会进入下一轮。

代码长任务（执行进程里跑的那种）跟随**任务与执行尝试**的生命周期：查看项目、修改代码、验证改动、准备交付四类阶段各最多一次（验证必须有宿主真的开跑测试，任意执行命令不算），走同一套动态措辞与去重，但不套用聊天"最多两条中段"的上限。阶段只认结构化事实：ACP 工具操作类型（`read/search/fetch` 是看，`edit/delete/move` 是改）、宿主真的启动验证器前发出的 `verification_started`、补丁确实生成后的 `patch_ready`；工具的自由文本标题一律不解析。收口顺序固定为**先关进度、再写终态回执**（结果桥通过 `progress_hook` 调用过程桥的 `close_attempt`），所以不会出现"回执之后又冒出一条正在改代码"。实现见 `agent_core/work_progress_bridge.py`。

## 向量数据库配置

支持两种 Qdrant 运行模式：

### 本地模式（默认）

适合开发和单进程场景：

```python
from agent_core.vector_config import QdrantConfig

config = QdrantConfig(mode="local", path="./data/memory_vectors")
repos = MemoryRepositories(db_path, vector_config=config)
```

或通过环境变量：

```bash
QDRANT_MODE=local
QDRANT_PATH=./data/memory_vectors
```

**限制**：本地目录同时只能被一个进程打开。多进程或生产环境请使用 server 模式。

### Server 模式（生产推荐）

支持多进程、多模块共享：

```python
config = QdrantConfig(
    mode="server",
    url="http://localhost:6333",
    api_key="your-api-key",  # 可选
    timeout=30.0
)
repos = MemoryRepositories(db_path, vector_config=config)
```

或通过环境变量：

```bash
QDRANT_MODE=server
QDRANT_URL=http://localhost:6333
QDRANT_API_KEY=secret  # 可选
QDRANT_TIMEOUT=30.0
```

启动 Qdrant server（Docker）：

```bash
docker run -p 6333:6333 -v $(pwd)/qdrant_storage:/qdrant/storage qdrant/qdrant
```

## 已知限制

- **本地模式单进程锁**：`data/memory_vectors` 目录同时只能被一个进程打开。因此**不要同时启动两个 chat_cli**；此时再开监视器，监视器的向量健康检查会显示 `unavailable`（读 SQLite 的部分不受影响）。**生产环境请使用 Qdrant server 模式**。
- **向量维度必须一致**：Qdrant 集合维度需等于 `ZHIPUAI_EMBEDDING_DIMENSIONS`（默认 2048）。改维度需重建向量目录。

## 测试

```bash
python -m pytest -q
```

测试覆盖：记忆生命周期、混合检索、写入/嵌入重试、持久化运行、工具注册、监视器渲染等。

情景对话支持角色扮演与 Chatbot 双模式。新情景必须在首条设定前选择模式；首条设定提交后模式锁定。两种模式都使用独立的深度分析实例，思考内容不会保存或展示；角色扮演生成完整行为卡，Chatbot 同时生成当前情景、七项人设和三项互动偏好。分析卡最长 6000 字，超长会重试而不会静默截断。

## 文档索引

- [docs/deepseek-harness-acp-bridge.md](docs/deepseek-harness-acp-bridge.md) —— DeepSeek Harness ACP 桥接：配置、沙箱失败关闭、状态投影、取消与恢复
- [docs/RECENT_MODULE_WORKFLOWS.md](docs/RECENT_MODULE_WORKFLOWS.md) —— 近期情景、开场白、代码代理的状态与排查工作流
- [docs/memory-lifecycle.md](docs/memory-lifecycle.md) —— 记忆生命周期与后台维护
- [docs/memory-retrieval.md](docs/memory-retrieval.md) —— 三层混合检索与排序
- [docs/memory-embedding.md](docs/memory-embedding.md) —— BigModel 嵌入配置
- [docs/cloud-memory-usage.md](docs/cloud-memory-usage.md) —— 云端同步使用说明；新版语义记忆与内容维护的开关、版本读法与自查脚本
- [docs/2026-10-11-memory-b-acceptance.md](docs/2026-10-11-memory-b-acceptance.md) —— B 计划（语义记忆与内容维护）独立验收报告与证据
- [docs/superpowers/plans/2026-08-14-user-driven-memory-lifecycle.md](docs/superpowers/plans/2026-08-14-user-driven-memory-lifecycle.md) —— 记忆生命周期设计稿
