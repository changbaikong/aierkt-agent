# B 第四轮：修复第三轮 7 项缺口（2026-10-12）

依据：`scripts/verify_memory_b_third_review.py` 当前实测 2/9（CONTROL-1/2 通过，B3-R1..R7 全失败）。
目标：该脚本 9/9 通过，且既有记忆回归（417 项）不退化，原 B 边界反例 11/11、自动维护反例 6/6 保持通过。

## 统一原则（贯穿全部修复）

一个原则：**程序只认「和这条内容对得上的证据」**，不再认「本轮出现过某个词」。

- 采纳要绑到**同一件事**（B3-R1）
- 工具证据要绑到**同一次成功结果**（B3-R2）
- 新值要**逐项**有依据，不能一部分有依据就带另一部分进库（B3-R3）
- 等值数字要认成同一个值（B3-R4）
- 所有会改旧记录的入口共用同一套保护，不能只修一个分支（B3-R5）
- 回执要按**实际结果**写，不能按「配置了回调」写（B3-R6）
- 短期语义要**真的被生命周期处理**（B3-R7）

## 分片（互不重叠）

### 分片 A：维护服务证据层 — `agent_core/memory_maintenance.py`
- B3-R1：放行前把状态绑定到建议内容与目标事项；本轮无关同意不得给别的建议盖章。
- B3-R2：工具证据必须与目标变更对得上，且 `outcome` 不是失败。
- B3-R3：逐项核对变化内容，无依据的字段不得随有依据字段一起进新版本。
- B3-R6：`verify()` 的 sync/partial 按真实回调结果写（回调异常 → 不写 emitted）。
- 共用判定放在本文件内，`memory_turn.py` 的公开函数只读不写。

### 分片 B：数字与值比对 — `agent_core/memory_turn.py`
- B3-R4：`changed_value_tokens` / 值证据比对支持等值数字（中文数词 ↔ 阿拉伯数字）、单位与必要上下文。
- 提供分片 A 所需的「逐项依据」辅助函数（同一文件，由 B 负责写）。
- 保持既有公开行为：`mentions_item`、`expresses_adoption`、`adoption_evidenced` 语义只增强不放宽。

### 分片 C：写入入口统一 — `agent_core/memory_writes.py`
- B3-R5：`save_semantic_candidates` 的 `supersedes` 旁路纳入同一套来源/事项/状态规则；仅 proposed 的替代不得归档旧现行记录。
- B3-R7（持久化侧）：候选的 `horizon` 落进 metadata；短期记录写入到期时间。
- 不得改变 `correct`/`replace`/`merge`/`invalidate` 既有原子性与版本保护。

### 分片 D：生命周期与提炼入口 — `agent_core/memory_extract.py`
- B3-R7（生命周期侧）：短期（`horizon=short_term`）记录进入现有到期政策。
- B3-R1 的候选侧：`_apply_status_evidence` 只在事项绑定时认采纳（依赖分片 A/B 的共用判定）。
- 不改维护建议解析的字段名与校验强度。

### 分片 E：独立验收 + 报告 — `scripts/verify_memory_b_fourth_review.py`、`docs/2026-10-12-*.md|json`
- 不碰 A–D 的任何产品代码；只读当前代码、跑隔离验收、写自己的证据与报告。
- 必须复跑 `verify_memory_b_third_review.py`（9/9）、原 11 反例、6 维护反例、记忆回归。
- 报告里区分「脚本初筛」与「独立逐条核对」，并记录未验证范围。
