# 记忆 B 计划四处修复 · 独立离线复核（反例证伪）

**一句话结论：不通过。** 验收脚本本身自报的数字全部对得上（退出码 0、新增 18/18、既有 11 个脚本全过、7 个文件无哈希漂移），但独立构造的反例证伪出 **3 处**问题，其中 2 处是硬伤：虚构的审批结论「已获上级同意 / 甲方认可 / 已获同意」能整条写进库并归档旧记录；阶段约束只要句子里带一个当天情绪词就会被降级成当天状态，两天后被清掉。

## 一、实测数字与命令

| 项目 | 命令 | 实测 | 与自报名单是否一致 |
| --- | --- | --- | --- |
| 验收入口 | `& .\venv\Scripts\python.exe scripts/verify_memory_b_final_acceptance.py` | 退出码 0，`ok=true`，`ALL_REQUIRED_CHECKS_PASS`；`new_checks` 逐条数 18 条、`new_passed=18`、`new_total=18`；`existing_scripts` 11 条全 `ok`；`failed_required=[]`、`failed_scripts=[]` | 一致 |
| 既有脚本抽样 1 | `& .\venv\Scripts\python.exe scripts/verify_memory_b_acceptance_edges.py` | `ok=true, passed=11, total=11` | 与聚合报告 11/11 一致 |
| 既有脚本抽样 2 | `& .\venv\Scripts\python.exe scripts/verify_memory_maintenance_acceptance_edges.py` | `ok=true, passed=6, total=6` | 与聚合报告 6/6 一致 |
| 既有脚本抽样 3 | `& .\venv\Scripts\python.exe scripts/verify_memory_maintenance_loop.py` | `ok=true, passed=9, total=9` | 与聚合报告 9/9 一致 |
| 冻结哈希 | 7 个文件 SHA256 前 16 位对比 `.pytmp\frozen_hashes.json` | `memory_evidence 2d93a7fba99ed683`、`memory_horizon f428486dba2c67b3`、`memory_turn 1514c1864fae95f9`、`memory_writes a932b25d07eca63c`、`memory_maintenance 7e2978d52a827f21`、`memory_queue f2cf89deac9a7d90`、`memory_extract 55772730a9fe65a1` | **7/7 全对，漂移 0** |
| 寿命字段真实落库 | `.pytmp\rev_probe_f_bypass_lifetime.py`（隔离临时库） | 阶段约束 `expires_at=None`、`horizon='long_term'`、`lifetime_basis='项目阶段约束，保留到被明确替代或取消'` | 与要求一致 |

自写反例脚本（全部在 `.pytmp\`，只读产品代码，隔离临时库，不碰生产数据）：
`rev_probe_a_evidence.py`、`rev_probe_b_minimal.py`、`rev_probe_c_status.py`、`rev_probe_d_write_path.py`、`rev_probe_e_clean.py`、`rev_probe_f_bypass_lifetime.py`、`rev_probe_g_final_repro.py`、`rev_probe_h_phase_mood.py`。

各块实测：

- **a 换方案夹带无出处结论**：原话否认评审/验收/上级表态时，`验收通过`、`评审通过`、`已获上级批准`、`试运行成功`、`演练结束`、`领导已签字` 全部被拦（`applied=False`，旧记录仍 `active`）；**但** `已获上级同意`、`甲方认可`、`已获同意` 三条 `applied=True`，旧记录被 `archived`，回执 `passed=True`、逐项 `items=()`。
- **b 正常同义改写**：「选择→决定/采用」「改成→改为」「用→使用」「敲定」「不再用→停止使用」「预算八千（带/不带量词、任一侧带量词）」全部 `gaps=[]`，写入路径 `applied=True`，**未误拒**。
- **c 寿命**：两条阶段约束 `long_term` + `expires_at` 为空；两条当天情绪 `short_term` + 1 天并按期到期；「只限今天」→1 天；「保留五天」→5 天（实测落库剩余 5.000 天）；明说期限与阶段措辞并存时按明说期限（5 天）。**例外见发现 2。**
- **d 负例**：跨事项号码（同句/分句/顺序颠倒三种写法）全部被拒（原话里另一个号码是 66219，内容里却写成这条记录的号码）；原话没有的数值「四百」被拒。

## 二、发现

**发现 1（硬伤，必须修）：不带「评审/审批/验收」名词的同意类结论根本没有检查项，「已获上级同意」整条落库并归档旧记录。**

- 哪里有问题：统一依据检查认「同意」当结论，只认「名词+结果词」（评审同意、审批通过）或固定字面（已获批准、已通过）。`已获同意`、`已获上级同意`、`甲方认可` 里的「同意/认可」既不是固定字面，前面也没有审批名词，于是这一句被判「没有结论类信息」；再往下 `uncertain` 那一层又被「依据里存在同事项的状态」放行，两层都不管。
- 为什么严重：这是四件事里第 1 件的核心承诺（「内容里每项信息都要找得到出处」）。用户明确说「还没验收，上级也没表态」，程序却把「已获上级同意」写进库、把旧记录归档，等于凭空造了一条用户从没说过的事实。
- 怎么复现：`& .\venv\Scripts\python.exe .pytmp/rev_probe_g_final_repro.py`。输出 `R1: applied = True 旧记录状态 = archived / 回执 passed = True 逐项 = ()`；同一位置换成「验收通过」则 `applied = False`（对照成立）。仅看检查层：`& .\venv\Scripts\python.exe .pytmp/rev_probe_c_status.py` 第 1 节，`已获上级同意` 的结论项识别为 `[]`。

**发现 2（硬伤，必须修）：阶段约束只要句子里带一个当天状态词，整条被降级成当天状态，推进两天后被清掉。**

- 哪里有问题：寿命规则里「阶段约束长期保留」这一步有个附加条件「而且不是当天状态」；只要正文含「今天/今晚/这会儿/心情/有点累」等任一当天状态词，这一步就被跳过，直接落到当天状态按 1 天处理。规则文档写的优先顺序是「明说期限 > 阶段约束 > 当天状态」，实现把它变成了「当天状态压过阶段约束」。
- 为什么严重：这正是第 2 件修复要防的事故。用户说「项目先不部署，继续联调，今天有点累」时，「先不部署」是阶段约束、是主句，「有点累」只是同一句里的状态；现在两天后维护会把这条约束标成 `expired`。
- 怎么复现：`& .\venv\Scripts\python.exe .pytmp/rev_probe_h_phase_mood.py`。输出：`H2 内容含「今天」… 落库 horizon='short_term' basis='当天状态，按现有短期政策处理'`，推进两天后 `status=expired`；同库对照 `H3 纯阶段约束 … status=active`。只判寿命：`项目暂时先不部署，今天我有点累` → `short_term / 1 day`，而 `phase=True sameday=True`。
- 边界说明：我没有主张「今天先这样」这种明说只限今天的句子必须长期保留。要修的是判定顺序——当天状态词不该无条件压过同一句里的阶段约束。

**发现 3（次要，建议修）：同一类别里已有一项结论时，同类另一项无出处的结论会被跳过。**

- 哪里有问题：判断「新内容多出来的结论」时，只要旧内容里出现过同一类别的标记，这一类别整体跳过，不区分是不是同一项，也不看旧内容里那个标记是不是被否定的。
- 怎么复现：`& .\venv\Scripts\python.exe .pytmp/rev_probe_e_clean.py`，第二节 F1 用例——旧内容 `部署方案为方案A，验收通过`，新内容加「评审通过」（原话只说验收），`applied=True` 旧记录被归档。
- 诚实标注：原验收报告里提到的「同类别有依据就能顶替另一项」在本轮**没能稳定复现**——因为模型把两条结论都写进了原话，证据里就都有了。上面这条依赖「旧内容里已经含同类标记」这个前提，影响面比发现 1 小，但机制是真的。

**已核实没问题的部分**：两条遗留项都站得住。普通词收窄没有扩大化——`value_tokens("项目日志一律先落盘再回传")` 为空，`changed_value_tokens` 里虽仍留「一律」这类字，但下游按「数值+量词」比较时被 `value_entries` 挡掉；反方向 `value_entries("预算八千元")==((8000,'元'),)` 仍生效。离散探针另测了「统一/一模一样/唯一/第一期/一点/一共」，`entries` 表现与预期一致；跨事项号码、原话没有的数值、扩大适用范围、虚构条件、新建候选借 `supersedes` 旁路这 5 类负例全部被正确拒绝或降级（旁路实测 `saved=[]`、旧记录 `active`）。

## 三、未覆盖范围

1. 全部为离线反例，`live_model_calls=0`。只证明程序会/不会接受给定的输出，不代表真实模型产生这类错误的频率。
2. 寿命结论用模拟时钟推进两天验证，不是真实长跑观察；跨进程、双端同步、生产库现状未验。
3. 发现 1、2 只验证了「写入路径确实会落库」与「寿命判定结果」，没有统计线上已受影响的数据量，也没有核对历史记录里是否已经存在这类被写进去的无依据结论。
4. 未验证并发、重试、队列重放路径下这三处是否表现一致；未覆盖非中文措辞与工具回执作为唯一依据的场景。
5. `agent_core` 下除这 7 个文件以外的模块、以及前端与其他服务未审。

## 复验轮（当前冻结代码）

**一句话结论：3 处里 2 处真的修好了、1 处没修好；另外发现 2 处新问题。**「取得式同意」整条落库已修好（`已获上级同意`／`甲方认可`／`已获同意` 全部被拦，旧记录保持 `active`）；「阶段约束被当天状态降级」已修好（推进两天后仍是 `active`）；**「同类另一项无出处的结论被跳过」没修好**（旧「验收通过」+ 新「评审通过」仍然 `applied=True`、旧记录被 `archived`）。新发现：带替代措辞的原话算依据这条新改动，把**同一句里另一件事的续写分句**也拉进了依据范围；同时「同意」类真结论在换方案场景里被误当成没有出处。

### 一、复跑上一轮三条复现脚本

| 脚本 | 上一轮实测 | 本轮实测 | 结论 |
| --- | --- | --- | --- |
| `rev_probe_g_final_repro.py` | R1 `applied=True`、旧 `archived` | R1／R2 均 `applied=False`、旧 `active`，原因逐项点名「同意」；对照「验收通过」同样被拦 | 发现 1 已修好 |
| `rev_probe_g_final_repro.py` 第 2 节 | 阶段约束+情绪 → `short_term`，两天后 `expired` | `expires_at=None`、`horizon='long_term'`，两天后 `active`（与纯阶段约束一致） | 发现 2 已修好 |
| `rev_probe_h_phase_mood.py` | H2 含「今天」→ `short_term`，两天后 `expired` | H1／H2／H3 全部 `long_term` + `expires_at=None`，两天后全部 `active` | 发现 2 已修好 |
| `rev_probe_e_clean.py` 第一节 | E1／E2／E3 三条 `applied=True` | E1–E7 全部 `applied=False`、旧 `active` | 发现 1 已修好 |
| `rev_probe_e_clean.py` 第二节 F1 | `applied=True`、旧 `archived` | **仍然 `applied=True`、旧 `archived`**；逐项识别其实已正确算出「评审通过」是相对旧内容的新结论项 | 发现 3 **未修好** |
| `rev_probe_e_clean.py` 第三节 | 跨事项号码全部被拒 | G1–G3、G5 仍被拒，G4 对照放行 | 未回退 |
| `rev_probe_e_clean.py` 第四节 | 预算八千三种写法放行 | H1–H3 均 `applied=True` | 未回退 |
| `rev_probe_g_final_repro.py` 正例 S1–S3 | 三条同义改写放行 | S1 选择→决定（correct）／S2 改成→改为／S3 用→使用 全部 `applied=True`、旧 `archived` | 未回退 |

发现 3 的机制（只读代码 + 探针输出）：识别层已经改对——「评审通过」被判为「相对旧内容的新结论项」并标 `unsupported`；但放行判据比的是「两句话有没有共同实义片段」，而「验收通过」与「评审通过」共用「通过」，于是被当成同一件事有出处。**根因是比对粒度停在字片段、没有落到「哪一项结论」上。**

### 二、本轮新写反例（措辞与上一轮不同）

脚本：`rev2_probe_new_counterexamples.py`（N1／N2／N3）、`rev2_probe_n3c_diag.py`、`rev2_probe_n3c_generalize.py`、`rev2_probe_n3c_smuggle.py`、`rev2_probe_n1e_minimal.py`、`rev2_probe_consent_minimal.py`，全部在 `.pytmp\`，隔离临时库、只读产品代码。

- **N1 换方案夹带原话没有的同意类结论（自创说法）**：原话「机房搬迁方案换成方案丙，方案乙作废；开会只是把材料过了一遍，没人拍板。」＋内容「…已征得客户同意」→ 拦住；同结构换成「主管部门认可」「已取得同意」「得到了领导认可」（原话改为「这事还没报上去，谁也没同意」）→ 全部拦住，旧记录 `active`。**这 4 条符合预期，但对照条 N1e 不符合**：原话真的写了「已经征得客户同意」时，同样结构仍然被拦住（见新发现 2）。
- **N2 阶段约束与当天状态混在同一句（自创说法）**：「灰度发布这轮先不推，往后放，我有点累」「数据迁移当前阶段不做，中午有点困」「架构改造尚未动工，今天没精神」「这会儿有点烦，接口评审先不安排」四条全部 `long_term` + `expires_at=None`，推进两天后全部 `active`；对照「接口评审就今天过一遍，只限今天」仍按 1 天并在两天后 `expired`。**5 条全部符合预期，发现 2 确实修好、也没把「明说只限今天」误判成长期。**
- **N3「不是 A，是决定 B」式续写**：正例「机房搬迁方案不是方案甲，是决定方案丙」→ 模型标 `correct`（`applied=True`）与标 `replace`（「值班轮换不是两班倒，是决定改成三班倒」）都放行，**没被误拒**；负例「机房搬迁方案不是方案甲，方案丙也还行。」（换成另一句、无结论引导词）与「机房搬迁方案先用着，预算不是八千，是决定六千」（纠正的是别的事）都被拒。**但第三条负例不符合预期**：「机房搬迁方案不是方案甲，**是决定排期往后挪一周**」→ 内容「机房搬迁方案决定采用方案丙」被放行、旧记录 `archived`（见新发现 1）。

### 三、新发现

**新发现 1（必须修）：续写分句只要自带一个值词，就被当成目标事项的依据，同一句里另一件事的结论因此能给「换方案」盖章。**

- 哪里有问题：为了让「不是 A，是决定 B」能认，续写规则把紧跟其后的分句并进「讲这件事的那几句」；并进来的门槛只看这一句是否含数值词或结论状态词，不看它说的是不是同一件事。
- 为什么严重：用户说的是「方案甲不用了，往后拖一周」，程序却接受了「方案已经决定采用方案丙」并把旧记录归档——丙从没被用户选过。这正好把上一轮修好的第 1 件事（结论必须有出处）从侧面打开一个口子。
- 怎么复现：`& .\venv\Scripts\python.exe .pytmp\rev2_probe_n3c_generalize.py`。C1（续写「排期往后挪一周」）与 C3（续写「预算六千元」）`applied=True`、旧 `archived`；对照 C2（续写「甲方同意」，不含值词）`applied=False`，C5／C6 正当替代仍放行。
- 边界（已实测，不夸大）：把同一手法再叠一层、往新正文里塞原话完全没有的短结论（「已完结」「已经收尾」）**会被拦住**，所以这条目前只能替换**已在原话里出现过的措辞**（如「采用」「决定」），不能凭空造词。见 `rev2_probe_n3c_smuggle.py` X1–X3 全部 `applied=False`。

**新发现 2（应修）：原话真给了「同意」类结论时，换方案场景里会被误判成没有出处。**

- 哪里有问题：续写规则不把「同意／认可」当成结论信号（只认「已完成／尚未」这类状态词和数值词），所以「已经征得客户同意」这一类分句被排除在依据之外；判定「有没有结论」的那一层却认它。
- 为什么严重：这是误拒（用户明说的事被当成没依据），方向与发现 1 相反。同一结构换成「主管部门已经核准」「验收通过了」都放行，只有「同意／认可」被拦，说明是词表没对齐，不是设计取向。
- 怎么复现：`& .\venv\Scripts\python.exe .pytmp\rev2_probe_n1e_minimal.py`。P1–P4（原话「…；已经征得客户同意。」）全部 `applied=False`、旧 `active`，依据句里根本没有那一句；P5（「主管部门已经核准」）与 P6（「验收通过了」）同样结构 `applied=True`、旧 `archived`。
- 边界：`& .\venv\Scripts\python.exe .pytmp\rev2_probe_consent_minimal.py` 显示，单独把内容与这句原话比对时是**能找到出处**的（`已经征得客户同意`／`已征得客户同意`／`征得客户同意`／`已取得客户同意` 均放行），被拦的是端到端绑定这一层；换成「客户已经同意」「客户已认可」「甲方已经同意方案丙」则连单句比对也不放行。

**发现 3（上一轮遗留，仍未修好）**：见上文第一节末行，复现命令仍是 `& .\venv\Scripts\python.exe .pytmp\rev_probe_e_clean.py`（F1）。

### 四、哈希漂移（SHA256 前 16 位）

7 个指定文件与 `.pytmp\frozen_hashes.json` **逐个相符，漂移 0**：`memory_evidence 5126c89a3ed28ba0`、`memory_horizon e871d4f051ebd638`、`memory_turn 1514c1864fae95f9`、`memory_writes a932b25d07eca63c`、`memory_maintenance 213c200d2beb8da7`、`memory_queue f2cf89deac9a7d90`、`memory_extract 81b0089ce060640a`。顺带核对冻结文件里另 3 条验收脚本哈希（`verify_memory_b_final_acceptance 1e37a0cb1a086dbb`、`verify_memory_quality_live 414608d4b43024a7`、`verify_memory_final_falsification a721ded33903efc2`）也全部相符。本轮复验期间没有改动任何产品代码、配置或生产数据。

### 五、验收脚本复跑

`& .\venv\Scripts\python.exe scripts/verify_memory_b_final_acceptance.py` → 退出码 **0**，`ok=true`、`verdict=ALL_REQUIRED_CHECKS_PASS`，`new_passed=18`／`new_total=18`，`existing_scripts` 11 个全 `ok`（11/11、6/6、9/9、24/24、13/13、9/9、16/16、12/12、26/26、3/3、3/3），`failed_required=[]`、`failed_scripts=[]`，`live_model_calls=0`。数字与明细一致，脚本本身没被做手脚。

### 六、复验轮未覆盖范围

1. 全部仍是离线反例，`live_model_calls=0`；本轮只证明程序会／不会接受给定输出，不代表真实模型产生这类错误的频率。新发现 1、2 都依赖模型先给出那样的建议文本。
2. 新发现 1 只在「机房搬迁方案／方案甲·乙·丙」这一族措辞上做了泛化（C1–C3、C5–C6），其他事项名词、其他语言、多轮连续替代未测。
3. 新发现 2 只测了「换方案」这一条路径（replace／correct + 引用原话）；新建候选、工具回执单独作依据、跨进程与队列重放路径下是否同样误拒未验。
4. 寿命结论依旧用模拟时钟推进两天，不是真实长跑；生产库里是否已存在被这条规则写歪的记录未核对。
5. 本轮没有重新审 `agent_core` 其余模块、前端、云端同步与并发路径；发现 3 的根因虽已定位到比对粒度，但没有穷举还有哪些同形结论对（如「审核通过 + 批准」）会走同一条路。

## 终轮（冻结代码）

**一句话结论：不通过 —— 验收脚本自报的数字全部复现，但独立反例证伪出 3 条（用户对别的事说的「同意／批准」仍能当依据；同种不同事的「验收通过」仍能互为依据），旧记录被归档、回执写着 `passed=true`。** 第 2 件（阶段约束长期保留、情绪同句不降级）、第 3 件（换说法豁免、单字取值位）复验**未发现**新问题。

### 一、实测数字与命令

| 项目 | 命令 | 实测 |
| --- | --- | --- |
| 验收入口 | `& .\venv\Scripts\python.exe scripts/verify_memory_b_final_acceptance.py` | 退出码 **0**，`ok=true`，`verdict=ALL_REQUIRED_CHECKS_PASS`，`new_passed=18`／`new_total=18`，`failed_required=[]`，`failed_scripts=[]`，`live_model_calls=0` |
| 既有 11 个脚本 | 同上（`existing_scripts` 逐条） | 11 条全 `ok`，逐条 passed/total：11/11、6/6、9/9、24/24、13/13、9/9、16/16、12/12、26/26、3/3、3/3 |
| 冻结哈希 | 7 个文件 SHA256 前 16 位 vs `.pytmp\frozen_hashes.json` | **7/7 相符，漂移 0**：`memory_evidence 439b67698d5a9bcd`、`memory_horizon e871d4f051ebd638`、`memory_turn 1514c1864fae95f9`、`memory_writes a932b25d07eca63c`、`memory_maintenance d700ade64f3bb80e`、`memory_queue f2cf89deac9a7d90`、`memory_extract 81b0089ce060640a`（复验结束再算一次，仍全对） |
| 第 5 步写入路径 | `& .\venv\Scripts\python.exe .pytmp\rev3_step5_write_path.py` | 阶段约束＋情绪同句：`expires_at=None`、`metadata.horizon='long_term'`、`lifetime_basis='项目阶段约束，保留到被明确替代或取消'`、`metadata.evidence_receipt` 存在且 `passed=true`、`items=[]`、`rule_version='evidence-1'`、`retention='long_term：项目阶段约束…'` —— **四项要求全部满足** |
| 验收门是否灵敏 | `.pytmp\rev3_mutant_run.py`（把 `.pytmp` 下的验收脚本副本 + 运行期 monkeypatch 把结论识别打瞎） | 基线副本 `ok=true 18/18`；打瞎后 `verdict=NOT_PASSED`、`main()` 返回 **1**、`new_passed=17/18`、`failed_required=['candidate-bypass-claim']` —— 门是「失败即不通过」的，但 18 项里只有 1 项对这类回退敏感 |

自写探针（全部在 `.pytmp\`，只读产品代码、隔离临时库、每用例独立 scope）：`rev3_probe_frozen_counterexamples.py`（4 组反例 + 3 组正例）、`rev3_dbg_mechanism.py`（机制定位）、`rev3_step5_write_path.py`、`rev3_mutant_run.py`＋`rev3_mutant_acceptance.py`。

四组反例与三组正例的实测（`预期/实测`）：

- **R1 取得式同意／固定字面审批结论无出处**：`已获公司同意`、`经负责人同意` 均拦住（`applied=False`，旧记录 `active`）✔；对照「原话只有孤立的『同意』」也拦住 ✔；对照「原话真给了甲方认可」放行 ✔。
- **R2 阶段约束＋情绪同句**：`数据迁移排期暂时不动，今天心里烦`、`日志回传流程往后再说，这会儿情绪有点低落` 均 `long_term`＋`expires_at=None`，推进两天后仍 `active` ✔；对照「只限今天」仍 1 天并在两天后 `expired` ✔。
- **R3 单字取值位**：原话没提「丙」写 `改为方案丙`、原话没提「C」写 `改为方案C` 均拦住 ✔；取值位层 `_unknown_runs` 分别报出 `['丙']`、`['C']` ✔；对照「原话明说方案丙」放行 ✔。
- **R4 两件事共用「通过」**：不同种（原话只说过验收通过，内容写 `评审通过`）拦住 ✔；**同种不同事（原话只说过备份策略验收通过，内容写 `机房搬迁验收通过`）放行 ✘**。
- **P1–P3 反方向正例**：`改用方案丙`→`决定采用方案丙`、`不再用甲`→`停止使用甲` 均放行 ✔；「保留五天」落成 `short_term`＋5 天 ✔；`验收流程暂时先不启动` 落成 `long_term`＋`expires_at=None` ✔ —— **正常表达没有被误拒**。

### 二、发现

**发现 1（必须修，本轮明文承诺「用户对别的事说同意不算」未做到）：审批结论只按「种类」核，没有绑到「哪一件事」，别的事的同意／批准仍能当依据，旧记录被归档。**

- 哪里有问题：绑定层把原话里**另一件事的结论句**当成这条记录的强续写拉进证据；核对层随后按「两句有没有共同实义片段」放行，不检查那句话说的是不是这件事。
- 证据（实测）：原话 `机房搬迁方案换成方案丙。客户提的那个报表格式我同意，照他说的改。` ＋ 建议 `机房搬迁方案为方案丙，已获客户同意` → `applied=True`、旧记录 `archived`，绑成依据的句子是 `['机房搬迁方案换成方案丙', '客户提的那个报表格式我同意']`；只给「搬迁」那一句当依据时 `gaps=['同意']`，加上报表格式那句后 `gaps=[]`，两句共用实义片段 `['同意','客户']`。把「同意」换成「已获批准／报销单」同样放行（共用片段 `['批准','获批']`）。同类对照 `同意，以后回答先说结论`（不带主体名词）确实被拦住——说明漏的是「带主体名词或换成批准」这一片。
- 为什么严重：用户从没说过客户批准搬迁，程序却把这条写进库、把旧记录归档，核对回执还写 `passed=true`；这正是四件事里第 1 件要防的凭空造事实。
- 怎么复现：`& .\venv\Scripts\python.exe .pytmp\rev3_probe_frozen_counterexamples.py`（R1c、R1d 两行 `预期 False 实测 True 旧记录=archived`）；机制与逐层对照：`& .\venv\Scripts\python.exe .pytmp\rev3_dbg_mechanism.py`（D1、D2，含同一句用逗号分隔的 D4a 同样放行）。

**发现 2（必须修，与发现 1 同一根因）：同种不同事的「验收通过」互为依据，A 的验收通过给 B 的验收通过盖章。**

- 哪里有问题：种类校验只能区分「评审 vs 验收」这种不同名词；两边都是「验收」时直接进入片段比对，而「备份策略验收通过」与「机房搬迁验收通过」共用 `验收／收通／通过`，于是被判成同一件事有出处。
- 证据（实测）：原话 `机房搬迁方案换成方案丙。备份策略验收通过了。` ＋ 建议 `机房搬迁方案为方案丙，机房搬迁验收通过` → `applied=True`、旧记录 `archived`、新记录回执 `passed=True items=[]`；只给「搬迁」句 `gaps=['验收通过']`，加上「备份策略」句 `gaps=[]`；两边种类都是 `验收`。对照把另一件事换成「评审通过」则拦住（这部分本轮确实修好了）。同一句（逗号）内版本 D4b 同样放行。
- 为什么严重：用户原话里验收通过的是备份策略，搬迁那部分根本没提；程序把「搬迁验收通过」写进库并归档旧记录，回执还说每一项都有依据。
- 怎么复现：`& .\venv\Scripts\python.exe .pytmp\rev3_probe_frozen_counterexamples.py`（R4b `预期 False 实测 True 旧记录=archived`）；`& .\venv\Scripts\python.exe .pytmp\rev3_dbg_mechanism.py`（D3、D4b）；落库与回执：`& .\venv\Scripts\python.exe .pytmp\rev3_step5_write_path.py`（W2 节）。
- 边界（不夸大）：我主张的是「同一件事」的核对应落到事项上；跨事项是否一律不许互为依据、以及这两条在新建候选／correct／工具回执路径下的表现，本文没有断言。

**发现 3（次要，针对验收脚本自身的灵敏度，不是产品缺陷）：18 项新增检查对「结论识别整体失效」这种回退只有 1 项会失败。**

- 哪里有问题：把结论识别打瞎后，`claim-approval`、`claim-execution`、`claim-window` 三项仍然通过，靠的是第二层 `uncertain` 兜底；只有 `candidate-bypass-claim` 抓住。
- 为什么要说：`new_passed=18/18` 是真的，但不要把它当成「这几层都被覆盖到了」的证明；真正的证据层回退可能只让 1 项变红。
- 怎么复现：`& .\venv\Scripts\python.exe .pytmp\rev3_mutant_run.py`（基线 `ok=true 18/18`）与 `& .\venv\Scripts\python.exe .pytmp\rev3_mutant_run.py --mutate`（`verdict=NOT_PASSED`、`new_passed=17/18`、`failed_required=['candidate-bypass-claim']`），报告写到系统临时目录，不覆盖仓库里的验收证据文件。

方法说明：首版探针在同一库同一 scope 下连跑多例，语义去重让后一例复用了前一例的记录、结论失真（R3a 曾误报放行）；改为每个用例独立 scope 后复跑，本文所有数字都来自独立 scope 版本。

### 三、未覆盖范围

1. 全部仍是离线反例，`live_model_calls=0`：只证明程序会／不会接受给定输出，不代表真实模型产生这类错误的频率。
2. 发现 1、2 只在「换方案（replace）＋机房搬迁／方案甲丙」这一族措辞上验证；新建候选、`correct`／`merge`、成功工具回执单独作依据、队列重放、跨进程与双端同步路径未验。
3. 寿命结论仍用模拟时钟推进两天，不是真实长跑观察；生产库里是否已有被这两条规则写歪的记录、影响面多大，未核对。
4. 未覆盖非中文措辞、多轮连续替代、同一轮里三件以上事情的相互借证。
5. 验收脚本的既有 11 个脚本是**同进程**调用、以其自报 `ok` 与 stdout 里的 JSON 为准，本轮只抽样核对了聚合数字可复现，没有逐个独立复算。
6. `agent_core` 下这 7 个文件以外的模块、前端、云端同步未审。
7. 本轮没有改动任何产品代码、配置或生产数据；唯一的写入是运行官方验收命令时它按自身设计重写了 `.pytmp\evidence\2026-10-12-memory-b-final-acceptance.json`，以及 `.pytmp\` 下的探针、`docs\` 下本报告。
