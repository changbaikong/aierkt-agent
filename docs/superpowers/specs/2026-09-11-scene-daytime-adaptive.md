# 设计规格：白天/夜晚自适应场景切换（纯前端）

- 日期：2026-09-11
- 状态：**待用户确认**（未确认前不得进入实现）
- 前身：本文件曾为"后端统一判定"方案（见 §附录A 变更记录），因实测环境缺陷改为纯前端
- 相关代码：`fronted/arcueid_daily_50/fronteddd/src/{storyData.js,pages/ChatPage.jsx,styles.css}`、`agent_core/prompts.py`

---

## 一、问题陈述

当前"白天/夜晚"的判定只存在于 prompt 的一句自然语言里（`agent_core/prompts.py:55`）：

> 白天按 06:00-18:00，晚上按 18:00-06:00 判断。

它是写给模型看的指令，代码里没有对应实现。前端场景则由 `ChatPage.jsx:445` 的硬编码 `scene.id === 6 ? ' scene-day' : ''` 与 `localStorage['currentSceneId']` 手动按钮控制，与时钟**零联动**。

目标：让前端场景根据当前时段**自动**在"白天"与"夜晚"之间切换。

---

## 二、已确认的设计决策

| # | 决策项 | 结论 |
|---|---|---|
| 1 | 判定依据 | **本机时钟 06:00 / 18:00**，与 prompt 口径一致；不引入地理位置依赖 |
| 2 | 判定位置 | **纯前端**（浏览器本地计算），不新增后端接口 |
| 3 | 手动按钮 | **移除**（场景按钮），改为设置页里的**开关** |
| 4 | 场景映射 | 白天 → `id=6` 办公书房；夜晚 → `id=1` 夜晚房间 |
| 5 | 同步方式 | 页面加载即判定 + **精确定时**；跨过 06:00 / 18:00 自动切换 |
| 6 | 设置落点 | 设置页新增「场景」区块，仅一个开关；偏好存 **localStorage**，不写 `.env` |
| 7 | 边界可调 | **不开放**。06:00/18:00 写死在 `daytime.js` 作为唯一真相源 |

### 为什么是纯前端（实测依据）

实现前的环境探测得到两个阻塞性缺陷，都在后端一侧：

1. **`ZoneInfo('Asia/Shanghai')` 直接失败** —— `TZPATH=()` 且 `tzdata` 未安装，`ZoneInfoNotFoundError`。
2. **`local_time_info()` 拿不到 IANA key** —— 在 Windows 上 `datetime.now().astimezone().tzinfo` 返回 `datetime.timezone(timedelta(seconds=28800), '中国标准时间')`，`.key` 为 `None`，兜底 `str(value)` 得到的是本地化中文名 **`"中国标准时间"`**，`ZoneInfo()` 无法解析。
   > 即 `agent_tools/system_info.py` 返回的 `timezone` 字段在本机**不是合法 IANA 名称**。
3. **`agent_tools` 包不可导入** —— `agent_tools/__init__.py` → `agent_core/__init__.py` → `memory_agent` → `memory_retrieval` → `memory_repositories` → `ModuleNotFoundError: No module named 'qdrant_client'`。

后两者属于既有环境债务，与场景切换无关。浏览器自带完整 IANA 时区库（`Intl.DateTimeFormat().resolvedOptions().timeZone` 稳定返回 `Asia/Shanghai`），且本地时钟天然就是"用户所处时区的当前时间"——这正是需求所要的口径。

### 时段边界语义

- `06:00 <= 时:分 < 18:00` → 白天
- 其余 → 夜晚
- **左闭右开**：06:00 整属于白天，18:00 整属于夜晚

---

## 三、实现方案

### 3.1 新增 `src/daytime.js` —— 单一真相源

```js
export const DAY_START_HOUR = 6
export const DAY_END_HOUR = 18

/** 按本地时钟判断是否白天。左闭右开：[06:00, 18:00)。 */
export function isDaytime(date = new Date()) { ... }

/** 白天返回 6（办公书房），夜晚返回 1（夜晚房间）。 */
export function sceneIdFor(date = new Date()) { ... }

/** 距离下一次时段切换还有多少毫秒（用于精确定时）。 */
export function msUntilNextFlip(date = new Date()) { ... }
```

要点：
- **纯函数**，接受 `date` 参数以便单测注入任意时刻，不读全局状态
- 边界常量集中导出，成为全项目唯一出处。**不对外开放自定义**（见决策 7）
- `msUntilNextFlip()` 返回距下一个 06:00 或 18:00 的毫秒数，用于 `setTimeout` 精确切换（避免轮询粒度带来的延迟）
- 另导出场景开关的读写（见 §3.4），与 `localStorage` 交互集中在此，便于测试替身注入

### 3.2 前端 `ChatPage.jsx` 改动

1. 移除 `localStorage['currentSceneId']` 持久化、`switchScene` 函数、手动场景按钮区块
2. `currentSceneId` 由 `sceneIdFor()` 初始化（同步，**无首屏闪烁**）
3. 时段同步：`setTimeout` 到 `msUntilNextFlip()` 时刻精确切换，切换后递归安排下一次（比 60s 轮询更准、更省）
4. 同时监听 `visibilitychange`：页面从后台切回时重新判定一次（应对休眠/长时间挂起导致定时器漂移）
5. `scene-day` 类不再依赖硬编码 `scene.id === 6`，改由 `isDaytime` 直接决定：
   ```jsx
   className={`app-shell free-chat-scene${isDaytime ? ' scene-day' : ''}`}
   ```
   解除"场景"与"光照"的耦合——以后加新白天场景无需再动这行。
6. 当开关为**关**时：不做时段判定与定时器，固定 `scene_id = 1`（夜晚房间），且不挂 `scene-day` 类

配套：保留 `.scene-day` 全部 CSS 规则不动（现有视觉已满足）。

### 3.3 `prompts.py` 同步约定

`prompts.py:55` 那句文本保留（模型需要该规则理解相对时间），但在其上方加注释标明：

> 时段边界以 `fronted/.../src/daytime.js` 的 `DAY_START_HOUR` / `DAY_END_HOUR` 为准，修改边界需同步此处。

跨语言无法物理共享常量，故用**明文契约 + 一条守门测试**约束：测试断言 `build_relative_time_context()` 的输出文本包含 `06:00` 与 `18:00`，使边界一旦漂移即报警。

注意：**本开关只影响场景视觉，不改变模型的时间认知**。关闭后模型仍按 06:00/18:00 理解"白天/晚上"——这是有意为之，避免"界面固定夜晚"被模型理解为"现在真的永远是夜里"。

### 3.4 设置页新增「场景」区块

`SettingsPage.jsx` 在"联网搜索"与"系统能力诊断"之间插入一个 `settings-section`：

```
场景
  [✓] 场景跟随时间切换
      开启后白天显示办公书房，夜晚显示夜晚房间（06:00 切换）。
      关闭后固定显示夜晚房间。
```

- 复用现有 `.settings-section` / `.settings-field` 样式与 `handleFieldChange` 模式（若需 checkbox 样式则补 CSS）
- 值为 `localStorage['sceneFollowTime']`，字符串 `'true'` / `'false'`，**默认 `'true'`**
- **即时生效**：切换后不依赖页面级"保存配置"按钮，立刻写入 localStorage 并触发一次重判定
  > 理由：该开关属于前端显示偏好，与 `.env` 配置无关。若混入 `handleSave`，会出现"改开关却写脏 `.env`"或"不点保存不生效"的困惑。
- **绝不加入** `web_api.py` 的 `config_keys` 白名单，不触碰 `.env`

读取封装在 `daytime.js`（如 `getSceneFollowTime()` / `setSceneFollowTime(v)`），使 `SettingsPage` 与 `ChatPage` 共用同一读写口径，避免两处写错 key。

---

## 四、不做什么（明确划界）

- 不新增后端接口 / 不新增 `agent_core/daytime.py`
- **不修** `tzdata` 缺失与 `agent_tools` 导入链（记为独立遗留问题，见 §六）
- 不引入地理位置 / 日出日落计算
- **不开放时段边界自定义**（决策 7）——只有两个场景，可调边界是伪需求；也不做"白天用哪张图"的场景选择器
- 不新增或修改任何场景素材图片
- **不把场景设置写进 `.env`**——纯前端偏好，与后端配置无关
- 不改动 `condition_tasks.py` 的天气任务窗口
- 不重构 `greetings.js` 的时间跨度分档逻辑
- 不做移动端/其他端适配

---

## 五、验证方式（TDD 计划）

先写失败测试再实现。测试文件 `src/daytime.test.js`（沿用项目 `node --test "src/**/*.test.js"` 约定）：

1. 05:59 → 夜晚
2. **06:00 整 → 白天**（边界左闭）
3. 12:00 → 白天
4. 17:59 → 白天
5. **18:00 整 → 夜晚**（边界右开）
6. 23:59 → 夜晚
7. 00:00 → 夜晚
8. `sceneIdFor()` 与 `isDaytime()` 结论一致：白天=6，夜晚=1
9. `msUntilNextFlip()`：
   - 05:00 → 3600000（1 小时，指向 06:00）
   - 20:00 → 指向次日 06:00（跨天正确）
   - 返回值恒为正且 ≤ 12 小时
10. 传入带时区的 `Date` 时按本地时钟口径判定（不误用 UTC 小时）

**开关读写测试**（`daytime.test.js` 内，沿用 `greetings.test.js` 的 localStorage 替身模式）：
11. 未写入时 `getSceneFollowTime()` 默认返回 `true`
12. `setSceneFollowTime(false)` 后读回 `false`
13. localStorage 中为非法值（如 `'abc'`）时回退默认 `true`

**守门测试**（Python 侧）：断言 `prompts` 输出含 `06:00` / `18:00`。

**手工验证**：
14. 白天时段加载 → 办公书房，立绘无夜色滤镜
15. 系统时钟拨到 17:59 停留 → 到 18:00 时自动切换为夜晚房间（验证精确定时）
16. 页面切后台再切回 → 场景与当前时段一致（验证 `visibilitychange`）
17. 快速连续刷新 → 场景稳定，不抖动
18. 设置页关闭开关 → 立即固定为夜晚房间（不点"保存配置"）；刷新后仍保持关闭
19. 打开开关 → 立即按当前时段显示正确场景

---

## 六、风险与遗留

| 风险 | 缓解 / 状态 |
|---|---|
| 前端成为"06:00/18:00"的第二份规则，可能与 prompt 漂移 | §3.3 明文契约 + 守门测试 |
| 跨时区访问（浏览器时区 ≠ 服务器时区） | **有意选择**：前端口径即"用户当前所在时区的本地时间"，比服务器时区更贴合"屏幕上现在是白天吗"的直觉 |
| 浏览器时区被用户手动改错 | 属用户侧配置，不做补偿 |
| 开关关闭后，界面固定夜晚但模型仍按真实时段理解 | §3.3 已说明：**有意为之**，避免模型把"固定夜晚"理解成"现在真的是夜里" |
| 设置页开关混入 `handleSave` 导致误写 `.env` | §3.4 明确：即时生效、不入 `config_keys`；实现时需确认差量里不含该字段 |
| 移除场景按钮后不便快速预览两种视觉 | 用设置页开关即可（开=按时段，关=夜晚）；若要白天，需拨系统时钟或在控制台调 `daytime.js` |
| **遗留缺陷（本次不修）**：`system_info.local_time_info()` 在 Windows 返回非 IANA 的本地化时区名（如 `"中国标准时间"`），且 `tzdata` 缺失导致 `ZoneInfo()` 不可用 | 影响面超出本任务，建议单独立项。注意：**任何未来消费 `local_time_info()['timezone']` 的代码都会踩此坑** |
| **遗留缺陷（本次不修）**：`agent_tools` 包因缺 `qdrant_client` 无法导入 | 同上，环境依赖问题 |

---

## 七、待确认

请确认以上规格。确认后我将：
1. 创建 git worktree 隔离分支
2. 输出可执行的分步实施计划
3. 以红绿 TDD 实现 `src/daytime.js` + 测试（含开关读写）
4. 接入 `ChatPage.jsx`（自动切换 + 移除场景按钮）
5. 设置页新增「场景」区块（开关）
6. 补 prompt 守门测试
7. 代码评审 → 收尾

---

## 附录A：变更记录

- **2026-09-11 初版**：判定位置为"后端统一判定 + 新增 `GET /api/scene-state`"，同步方式为 60s 轮询。
- **2026-09-11 修订一**：改为**纯前端**。原因见 §二"为什么是纯前端"。原方案中"后端统一判定保证与 prompt 一致"的优势，被两处实测缺陷（时区名非 IANA、`tzdata` 缺失、包导入失败）的成本抵消；改用明文契约 + 守门测试达成同等的一致性保障。同步方式从"60s 轮询"升级为"精确定时 + 可见性回补"。
- **2026-09-11 修订二**：按"保存在设置中"新增决策 6、7 与 §3.4。设置落在**设置页 UI + localStorage**（非 `.env`）；开关即时生效；**明确不开放时段边界自定义**——只有两个场景，可调边界是伪需求，边界保持写死在 `daytime.js`。
