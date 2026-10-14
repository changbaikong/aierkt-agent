# B 记忆协议第一阶段只读审查

审查范围：`memory-agent-repair-workflow.md`、`memory_processing.py`、`memory_semantic.py`、`memory_horizon.py`、`tests/test_memory_model_led.py`，以及 2026-10-13 的模型实测输出。没有修改实现，也没有重新发起付费调用。

## 第一阶段应固定的重放夹具与原话预期

| 夹具 | 输入原话与历史 | 独立预期（以用户原话为准） | 必验数据库/新会话结果 |
| --- | --- | --- | --- |
| Q1 | 用户：“这个项目暂时先不部署，继续做本机验收。”；助手仅确认 | 至少拆成部署暂缓与本机验收两项；不能补出部署地点或实施；这是阶段约束，不是默认一天 | 两项均可保存；部署地点不存在；后续会话只读到这两个决定 |
| Q2 | 三项助手建议后用户只说：“嗯” | 无法确定采纳哪一项；不得把“嗯”绑定到全部建议 | 不产生任何采纳记忆，旧记录不被替代 |
| Q3 | 助手：“以后回答先说结论可以吗？”；用户：“嗯，就这样” | 唯一提议得到明确认同；可保存“先说结论”长期偏好；不得增加“再做说明”等助手未提内容 | 新偏好可保存并在下一会话召回；认同绑定在条目级 `adoption`，不是 quote 子项 |
| Q17 | 用户：“排期方案暂不调整；部署方案暂时先不部署。”；旧记忆“部署方案为立即执行”；助手回复补出“先放本机” | 排期和部署分开；部署旧决定退出，新决定不含“本机”；“暂时”是阶段寿命，不能强制 short_term/一天 | 新排期与部署记录可原子提交；旧部署 archived；新会话召回“暂不部署”，不召回“本机” |

Q17 的三次真实重放保存在 `2026-10-13-memory-b-c-gate-model-review-replay-final.json`；Q1 三次、Q2 三次和 Q3 两次模型输出在 `2026-10-13-memory-model-led-live.json`。这些文件的 `semantic_acceptance` 仍是 `unknown`，不能用模型自评替代独立判断。测试应保留原始响应不变：原始无效 schema 重放必须明确失败原因；修正后响应用独立夹具验证可保存和召回，不能给原始 Q17 short_term 缺 days 默补 phase 并称模型理解通过。

## 已证实的实际缺陷

1. Q1/Q17 的模型把“暂时/暂不”当成 short_term，却不给 `days`；当前 `_validate_review` 直接拒绝，导致正常决定漏存。`memory_horizon.resolve_lifetime` 又把项目阶段约束作为长期保留，提示词、运行校验和寿命模块互相矛盾。
2. Q1-1 的父项有 `verdict=supported`，两个 `parts` 均缺 `verdict`，而校验逐个子项要求 `supported`；连合法 phase 的部署项也被拒绝。这是独立子项 schema 不完整，不能只修 lifetime。Q1-2/3 另有未拆分事项。
3. Q3 模型理解了唯一提议和认同，却把 `adoption=true` 放入 quote；程序只接受条目顶层字段；Q3-1 还给出 `same_subject=false`。存档请求的 sources 实际包含“嗯”和“就这样”两个合法原始分句，因此不能把这两个 quote 判为伪造或错误引用；完整原句可以作为新 span 协议的额外约束。
4. Q17 模型正确删掉助手补出的“本机”，但因期限字段无效而整体待复核；无有效变更凭据时旧“立即执行”仍 active，后续会话会召回过期决定。
5. 条目级无效引用、缺字段和无效期限都汇总成 `review_not_supported_or_invalid_quotes`；空响应走 `invalid_review_response`，首轮调用超时直接向外抛出。诊断和恢复不能混为一个不确定语义状态，应分别保存原因和重试预算。

## 统一 schema 的兼容约束与风险

- 当前 `original_sources()` 只有 `message_id/quote/source_type`，没有稳定 `source_id`、字符起止 `span` 或来源版本；统一 schema 若直接改字段，会使旧回执和缓存无法验证。应保留旧字段读取兼容，并为每个来源生成稳定 ID 与 span，引用按 ID+span 精确绑定。
- 当前 `MemoryCandidate.source_refs` 只存消息 ID，不能表达同一消息的两个事项或完整分句；迁移时必须允许旧 message ID 回退解析，同时新写入强制 source locator，禁止用 quote 相似度补绑。span 应统一零起点、结束不包含的字符区间，并明确按 Unicode 字符计数，防止中文与表情在 Python/浏览器之间错位；重复 message ID 有冲突内容时必须拒绝。
- `adoption` 是语义绑定事实，应与 `index/verdict` 同级；`source_type=assistant_proposal` 只有在同一条目显式 adoption 且有用户认同来源时才可支持。多提议后的单独“嗯”必须保持 uncertain/unsupported。
- `content_status`（事实/决定的进度）与 `verdict`/`uncertainty`（本次语义判断是否可靠）必须分开存取；`confidence` 只能表示理解把握，不能把 uncertain 变成 reported/accepted，也不能把 accepted 变成 verified/completed。
- `memory_horizon.Lifetime` 目前只有 `horizon`，会丢失模型返回的 `phase`；统一 schema 应保留 `mode=phase|short_term|long_term`、`basis`。`days` 必须有明确依据，区分用户明示天数与当天情绪/一次请求的一天政策；当前提示词“short_term 必须原话明确天数”与既有 `test_memory_subject_lifetime.py` 和工作流的一天政策冲突。阶段决定不因“暂时”自动过期。
- subject lifetime 必须按每个 part 计算；混合句不能把一个“三天提醒”期限借给长期日志或部署决定。
- 回放缓存 key 绑定候选、packet、目标版本和规则版本；schema 升级必须递增规则/模式版本，旧 cache 只能重放或显式失效，不能静默当作新授权。

## 自主检索与恢复的第一阶段边界

阶段一应验证代理能从当前回合、相关历史和现行记录主动检索，再形成计划；空 `relevant_history` 不能被当作“没有历史”，应记录搜索未执行、无结果或权限拒绝三种状态。提交前必须重新读取目标版本并检查 scope/owner/project 绑定。

恢复夹具至少覆盖：无模型配置、伪造/缺 span 引用、空响应、一次格式修正失败、60 秒超时、目标版本变化、持久化判断后崩溃、提交后崩溃、重启重复领取。已持久化的判断重试提交时不得再次调用模型；版本变化只重读受影响事项。旧记录只有在同事项且 `change_authorized=true` 的有效绑定存在时才可归档。

本报告结论：第一阶段先冻结上述四组原话夹具和独立 oracle，再做 schema 迁移；在 Q1/Q3/Q17 的字段、寿命和旧记录召回问题修复前，不能宣称 B 完成或放行 C。

## 2026-10-13 本轮修复后的补充只读复核

范围仅为 `agent_core/memory_protocol.py`、`agent_core/memory_processing.py`、`tests/test_memory_protocol_replay.py`。未改实现、未重复执行主代理的测试、未调用付费模型；以下证据来自纯 Python 解码与修正合并探针，不代表真实模型理解或数据库提交验收。

1. **P1：重复 index 清除原始拒绝，修正响应仍能将其放行。** 首响应包含两个 `index=0`，第一个分别设置 `same_subject=false` 或引用 `source_id=fake`，第二个为完整合法项；解码重复 index 时删除此前保留的同编号项。两种探针都得到 `initial.items=[]`、`needs_repair=true`，修正响应提供单个合法 `index=0` 后，合并结果含 `same_subject=true` 和合法引用。原始拒绝已从保护合并的输入消失。应保留不可覆盖的编号级拒绝记录；重复编号可以待复核，但不能让格式修正获得重新判断语义或替换坏引用的权限。现有混合错误测试未覆盖重复编号。
2. **P1：显式 adoption=false 未纳入否定标记保护。** 使用来源 `s0=user:yes`、`s1=assistant:proposal`，首响应引用两者且 `adoption=false`，其余语义标记为真、`change_authorized=true`，期限为缺 `days` 的 short_term。解码仅记录缺 days；修正补 days 并改为 `adoption=true`，合并结果保留新的真值和自动生成的认同绑定。原始不认同被当成可替换格式项。应锁定原始 adoption 否定，测试认同否定与缺期限共存时仍不能授权助手建议。
3. **P1：adoption_binding 中的坏来源未在初次解码时拒绝。** 与上一探针相同，但首响应为 `adoption=true`，proposal 绑定引用 `source_id=fake`，普通 quotes 全合法，short_term 仍缺 days。初次解码只得到 `invalid_field`，未得到 `unknown_source_id`；修正补 days 并替换 proposal 绑定后，合并结果已变为合法绑定。普通 quotes 的新保护未覆盖认同绑定引用。应对 proposal/acknowledgement 同样解析来源和范围，并把引用拒绝独立于格式错误保留。

代码检查还确认：缓存指纹现已同时绑定正常复核提示和格式修正提示；完整合法的兄弟项仍由原始项覆盖修正项。本轮未重跑对应测试，因此只确认实现覆盖，测试通过结论由主代理给出。

## 2026-10-13 第二轮保护修复后的只读复核

范围仅为最新 `memory_protocol.py` 与 `memory_processing.py`；未修改实现、未重复主代理测试、未付费调用。代码检查确认：整数重复编号现以 blocked_indices 保留并跨修正合并；显式 adoption=false 纳入保护；认同绑定的两个引用在首次解码时还原并核验；首次与修正的 format_errors 合并保存。

**仍有 P1：无效编号类型能删除原始拒绝，却不进入封锁列表。** Python 中 `False == 0`、`0.0 == 0`。构造首响应 `[原始 index=0 拒绝项, index=False 或 0.0 的无效项]`，原始拒绝分别为 `same_subject=false` 与 `unknown_source_id`。四个纯解码/合并探针均输出 `initial.items=[]`、`blocked_indices=[]`、`needs_repair=true`；格式修正返回合法 index=0 后，合并结果重新放行。无效编号分支使用普通相等比较删除此前槽，但 blocked_indices 只登记精确整数重复号，两个条件不一致。应让无效类型完全不触碰既有槽；只对经过精确整数检查的编号执行重复删除与封锁，并覆盖 false/true、浮点数与对应整数编号组合。该遗漏同时影响合法兄弟项不可改写保证，因为其保护输入同样可能被无效编号清除。

阶段门结论：阶段 1 的修正不可改变原始拒绝这一硬边界尚未闭合，本轮不建议进入阶段 2。此处只判断协议门，不代表已完成全部阶段 1 的语义、持久化或真实模型验收；修复上述编号别名漏洞并由主代理确认相关测试通过后，可以重新判断阶段推进。
