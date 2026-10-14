# 云端记忆同步：使用说明

本地负责记录和读取，云端负责提炼、轮转和维护，默认每 10 分钟同步一次。

## 职责划分

本地（这台电脑）：写对话、读记忆、向量检索。**不再自己提炼、不再自己轮转。**
云端（轻量服务器）：收对话、调模型提炼、入库、过期归档、算向量。

一句话：本地是「嘴和眼睛」，云端是「记性和整理」。

## 云端跑什么

服务器 `<你的服务器地址>` 上常驻三个服务，都是开机自启、崩了自动重启：

| 服务 | 干什么 | 听哪个端口 |
|---|---|---|
| `agent-memory-api` | 收上传、给变更、给快照、报状态 | `127.0.0.1:8790` |
| `agent-memory-worker` | 每 30 秒取一轮对话，提炼入库 | 无（出站） |
| `qdrant` | 向量库 | `6333` / `6334` |

**8790 没有放通公网**，只能从 SSH 隧道进来。端口不暴露，就不存在
「谁拿到口令谁就能往记忆库里灌东西」那一层风险。

## 本地配置

`.env` 里这几项：

```
MEMORY_CLOUD_SYNC_ENABLED=true      # 总开关
MEMORY_EXTRACT_ENABLED=false        # 本地不再提炼
MEMORY_MAINTENANCE_ENABLED=false    # 本地不再轮转
CLOUD_MEMORY_URL=http://127.0.0.1:8790
CLOUD_MEMORY_TOKEN=<服务器上绑定 local-user 的口令>
MEMORY_SYNC_INTERVAL_SECONDS=600    # 10 分钟
```

改完要重启本地 agent 才生效（开关是启动时读的）。

## 日常怎么用

**隧道已经在开机自启里了，正常开电脑它自己就在跑。** 不用管它。

隧道没起来时（刚装完还没重启、或者手动关过），双击这个就能立刻拉起来：

```
scripts\cloud_memory_tunnel.cmd
```

它会一直重连：网络换了、电脑睡醒、服务器重启，都自己恢复，不用手动干预。

### 开机自启是怎么装的

```
scripts\install_cloud_tunnel_autostart.cmd      # 装
scripts\uninstall_cloud_tunnel_autostart.cmd    # 卸（只删启动项，不动仓库里的脚本）
```

装的时候往「启动」文件夹放两个文件（就是上面那个 `.cmd` 和它的隐藏启动器
`.vbs`），登录时自动跑、没有黑窗口。不用计划任务是因为这个项目在带中文的
用户目录下，`schtasks` 传路径时的编码会和 `cmd.exe` 对不上，装出来的任务
路径是坏的 —— 放文件到启动目录没这个问题。

**装完要重新登录一次才生效**（或者手动双击那个 `.cmd` 先顶上）。

看同步状态：

```
ssh -i ~/.ssh/<你的密钥名> root@<你的服务器地址> "tail -n 20 /var/log/agent-memory-worker.log"
```

隧道通着的话，本地直接看更方便：

```
curl http://127.0.0.1:8790/api/memory/sync/status
```

agent 自己也在 `/api/health` 的 `checks.cloud_sync` 里报同步状态
（`status` / `last_sync_at` / `upload_pending`）—— 隧道断了这里会显示积压往上走。

## 隧道不开 / 云端挂了会怎样

**不会丢东西，也不会卡聊天。**

- 本地照常聊天、照常读记忆。
- 每一轮对话进本地队列排队，网络恢复后自动补传。
- 上传失败的**不出队**，下次同步重试同一条，不会跳数据。
- 云端提炼失败会重试（30 秒 → 2 分钟 → 10 分钟，最多 5 次），
  5 次都失败的任务保留在库里并告警，不会悄悄消失。
- 退出 agent 时最多等 5 秒补传；传不完的留在队列里，下次启动接着传。

## 回退到纯本地

把这三行改回去，重启 agent：

```
MEMORY_CLOUD_SYNC_ENABLED=false
MEMORY_EXTRACT_ENABLED=true
MEMORY_MAINTENANCE_ENABLED=true
```

本地立刻恢复成改造前的样子，云端那边不停不影响。

## 首次接入怎么做的

分两步，都跑在服务器上（要同时看到两个库文件）：

```
# 本机：导出一份干净快照（agent 要先停掉）
scp data/memory_records.db 到服务器 /tmp/local-memory.db

# 服务器：先只核对
python scripts/cloud_memory_baseline.py --local-db /tmp/local-memory.db --owner local-user
# 确认没问题再对齐
python scripts/cloud_memory_baseline.py --local-db /tmp/local-memory.db --owner local-user --apply
```

两边都认识、内容也一致的记录**一条不动**；只差时间戳的也跳过（硬推只会把
云端的时间往回拨）。云端独有的记录原样保留并列在核对结果里，不删不覆盖。

本地客户端不需要额外操作：它的**第一次**同步会自动改用完整快照对齐
（因为变更流水里只有「装好这套系统之后」的改动，之前就存在的记录
一条都不会出现在流水里 —— 只看流水，它们永远到不了本地，而且不报错），
之后就一直走增量。

## 新版语义记忆与内容维护（2026-10-11）

这一层管「记住什么、改哪条旧的」：七类内容（事实 / 决策 / 计划 / 偏好 / 经历 / 待解问题 / 假设）、
每类自己的进展状态，以及**记录自己的版本号**。它和上面的冷热同步是两层，开关也分开。

### 三个开关

| 开关 | 默认 | 管什么 |
|---|---|---|
| `MEMORY_SEMANTIC_ENABLED` | `false` | 新版语义记忆总开关。关着时这一层完全不跑，行为跟改造前一样 |
| `MEMORY_PROCESSING_ENDPOINT` | `local` | 同一个回合只由**一个**端处理；取值只能是 `local` 或 `cloud`，写别的直接报错，不静默降级 |
| `MEMORY_CONTENT_MAINTENANCE_ENABLED` | `false` | 内容维护：对**已有**记录提更新建议并应用。总开关开着而它关着时，新建候选照常，只是不改旧记录 |

两个附带开关默认跟随总开关，要单独调时再写：`MEMORY_SEMANTIC_EXTRACT_ENABLED`（只提炼）、
`MEMORY_SEMANTIC_RECALL_ENABLED`（只召回）。**两个都关时新记录对召回不可见** ——
不会留下「写进去了但读不到」的中间态。

### 一个回合会发生什么

1. 正文发布后，把这一轮、本轮用到的旧记忆和相关背景做成处理包，交给**指定处理端**的持久队列；
2. 提炼一次，同时给出「新建候选」和「对已有记录的更新建议」；
3. 候选按七类落成记录；更新建议进维护队列，由维护工人应用（纠正 / 替代 / 合并 / 标记冲突 / 失效等）；
4. 写入在**一个事务里**核对目标还是不是现行版本，旧版本归档保留并指向新版本；
5. 变更同步到另一端，下一次回答与核查看到的是现行版本。

关掉内容维护时：候选照常落库、更新建议不应用、也不新开维护任务；已排队未执行的作业原样留着，
重新打开后照常处理，既有新版本不会倒退。

### 版本怎么读

- 记录自己的修订号是 `record_version`；
- `status='active'` 且 `effective_record_id` 为空的那条才是**现行版本**；
- 被纠正或替代掉的旧版本是 `status='archived'`、`content_status='superseded'`，并用 `effective_record_id` 指向新版本；
- `review_state` / `review_due_at` 是复核状态与下次复核时间。证据不足的失效**只排复核、不改内容状态**，缺哪条证据、下次从哪取证记在记录的 `metadata.review_gap` 里。

### 失败和拒绝要分开看

| 情况 | 回执 | 会不会重试 |
|---|---|---|
| 证据不足、权限不够、目标版本已变 | 拒绝，带原因 | 不重试（重试结果一样） |
| 数据库写不进去、向量或同步出问题 | 执行失败，带错误 | 按次数重试：本地 30 秒 / 120 秒、最多 3 次；云端 30 秒 → 2 分钟 → 10 分钟、最多 5 次 |

重试复用**已落库**的提炼结果，不会重新调模型 —— 重跑一次模型会得到另一版措辞，
同一动作的幂等键就跟着变了。

看积压：本地队列 `kind_stats()` 把「候选」和「维护」分开计数；云端队列多一个 `maintained` 状态，
表示「这一轮没有新候选，但改了已有记忆」，跟「什么都没有」不是一回事。

### 云端接口的一个硬要求

上传必须把**处理包**（`packet`）和包版本（`schema_version`）一起带上。少了它，云端只看到一问一答：
更新建议没有可指向的旧记录，纠正与替代在云端整条失效，而且不报错（接口会静默丢掉模型里没有的字段）。
接口现在会原样收下并落库，用例 `tests/test_cloud_memory.py::test_the_processing_packet_survives_the_upload_hop` 钉住这一点。

### 怎么自查

六份脚本、预期条数和证据文件（都在临时库/临时目录里跑，不碰生产记忆）：

```
python scripts/verify_memory_b_acceptance_edges.py             # 11/11 原边界反例
python scripts/verify_memory_maintenance_acceptance_edges.py   # 6/6  维护反例（故障、旧版本、错误建议）
python scripts/verify_memory_maintenance_loop.py               # 9/9  离线维护闭环
python scripts/verify_memory_search_fusion.py                  # 24/24 搜索与记忆融合
python scripts/verify_memory_dual_end_semantics.py             # 13/13 同一包两端语义对照
python scripts/verify_memory_dual_end_http.py                  # 8/8  走真实 HTTP 的上传—处理—回传—纠正
```

（真实模型的两条：`verify_memory_maintenance_live.py` 7 个样例各 3 次、`verify_memory_answer_chain_live.py`
四轮真实对话加调用回执。它们会真的调模型、真的搜外网，按需跑。）

证据与逐条判定见 [B 计划独立验收](2026-10-11-memory-b-acceptance.md) 与
[复跑证据](2026-10-11-memory-maintenance-repair-evidence.json)。

### 正式启用与回滚（待确认后执行）

代码已经部署到云端并在服务器上跑通验收，但**开关还没开**（两端 `.env` 都不设这三个变量 = 默认关闭，生产仍走旧的陪伴提炼路径）。要启用：

**启用前先确认三件事**

```
python scripts/verify_memory_acceptance_all.py     # 六份脚本 + 记忆测试，退出码 0
python scripts/check_memory_compatibility.py       # 存量数据与新版契约的只读兼容审计
ssh ... "cd /opt/agent/app && sha256sum cloud_memory/api.py cloud_memory/worker.py"   # 与本地一致
ssh ... "ls -lh /opt/agent/code.bak-*.tgz"         # 有代码备份
```

兼容审计查的是：老库里有没有契约外的类型/状态组合、同一事项两条现行版本、悬空的现行版本引用、异常的版本号或复核状态。它是**只读**的，随时可以跑；有问题会打出来并返回退出码 1。2026-10-11 实测：本机库（1376 条）与云端生产库（1337 条）都是 0 问题。

**启用（两端各改 `.env`，然后重启）**

```
# 服务器 /opt/agent/app/.env
MEMORY_SEMANTIC_ENABLED=true
MEMORY_PROCESSING_ENDPOINT=cloud
MEMORY_CONTENT_MAINTENANCE_ENABLED=true

# 本机 .env（只读那一端）
MEMORY_SEMANTIC_ENABLED=true
MEMORY_PROCESSING_ENDPOINT=cloud
MEMORY_SEMANTIC_RECALL_ENABLED=true

systemctl restart agent-memory-api agent-memory-worker    # 服务器；本机重启 agent
```

**启用后头半小时看什么**

| 看哪 | 看什么 | 正常的样子 |
|---|---|---|
| 服务器服务 | `systemctl is-active`、`NRestarts` | 四个 active，重启数不涨 |
| 服务器队列 | `memory_ingest_queue` 各状态条数 | 新回合从 `pending` 走到 `stored` / `maintained`，`exhausted` 为 0 |
| 服务器记录 | `memory_records` 里 `semantics_version>=1` 的条数 | 开始出现并增长 |
| 变更流水 | `memory_changes` 的 `max_seq` | 随每次更新前进 |
| 本机 | 说一句话后上传队列清空；再问相关问题时召回到新记录 | 上传清空、能读到新版内容 |
| 两端 | 同一 `semantics_key` 的现行版本 | 只有一条（`active` 且 `effective_record_id` 为空） |

**回滚（出问题就退回来）**

把两端那几行改回注释或 `false`，重启对应服务：新链路立刻停写，旧陪伴提炼路径恢复；已经写成的新版记录留在库里、不会倒退，也不会被删。

**两个注意**：处理端只能是一个（两端都写 `cloud`），写错会直接抛错而不是静默降级；本机如果将来要自己处理（`local`），先确认向量库是服务端模式 —— Qdrant 本地模式不允许两个进程同时打开同一个目录。


## 验收结论（2026-10-09）

`scripts/cloud_memory_acceptance.py` 在**独立测试库**上跑，19 项全过：

- 错口令被拒 / 跨账号上传被拒（账号隔离真的在拦，不是只有一层全局口令）
- 上传、重复上传只入队一次、同秒的两轮都传上去
- 提炼出变更并合并进本地；归属沿用原账号和原会话，没被塞进默认会话
- 记录时间贴着原对话时间（不是云端收到的时间）
- 向量随变更回传；没有向量的记录留在 pending，本地补算
- 同一批变更重放不产生重复记忆（处理中重启的等价场景）
- 快照校验能拦住被篡改的内容；断网内容留在队列
- 到期判断不依赖状态字段（本地不再跑轮转，读取路径自己看时间）

实测数字：本地 1370 → 1373 条（云端 504 条 local-user 全部对齐），
第二轮同步改走增量，拉 0 条。
