# 实施计划：白天/夜晚自适应场景切换

- 日期：2026-09-11
- 规格：`docs/superpowers/specs/2026-09-11-scene-daytime-adaptive.md`（已确认）
- 隔离方式：**不开 worktree**（工作区有 58 项未提交改动，隔离反而制造合并风险），直接在当前分支改
- 改动边界：新建 2 个文件，修改 3 个文件。**不触碰其他任何未提交改动**

## 改动文件清单

| 文件 | 操作 |
|---|---|
| `fronted/arcueid_daily_50/fronteddd/src/daytime.js` | 新建 |
| `fronted/arcueid_daily_50/fronteddd/src/daytime.test.js` | 新建 |
| `fronted/arcueid_daily_50/fronteddd/src/pages/ChatPage.jsx` | 修改 |
| `fronted/arcueid_daily_50/fronteddd/src/pages/SettingsPage.jsx` | 修改 |
| `agent_core/prompts.py` | 修改（仅加注释） |

---

## 阶段 0：前置勘察（已完成）

- [x] 确认 git 状态：主仓库 `codex/agent-monitor-console` 27 项改动；嵌套前端仓 `master` 31 项改动
- [x] 确认 `fronted/arcueid_daily_50` 是**嵌套独立仓库**（有自身 `.git`），非 submodule
- [x] 确认 `package.json` 有 `"test": "node --test \"src/**/*.test.js\""`
- [x] 确认 `SettingsPage.jsx` 是 `.env` 面板，区块结构、样式类名与 `handleFieldChange` 模式
- [x] 确认 `ChatPage.jsx` 的场景状态、按钮、`scene-day` 类位置

---

## 阶段 1：TDD RED —— 先写失败测试

**目标**：`src/daytime.test.js` 存在且全部失败（因为 `daytime.js` 还不存在）。

- [ ] 1.1 新建 `src/daytime.test.js`，导入尚不存在的 `./daytime.js`
- [ ] 1.2 写 13 个用例（详见规格 §五 的 1-13）
- [ ] 1.3 运行 `npm test`，确认**全红**（导入失败）

验证：`node --test "src/**/*.test.js"` 输出中 `daytime.test.js` 报错。

---

## 阶段 2：TDD GREEN —— 最小实现

- [ ] 2.1 新建 `src/daytime.js`，导出：
  - `DAY_START_HOUR = 6`、`DAY_END_HOUR = 18`
  - `isDaytime(date = new Date())`
  - `sceneIdFor(date = new Date())`
  - `msUntilNextFlip(date = new Date())`
  - `getSceneFollowTime()` / `setSceneFollowTime(value)`
- [ ] 2.2 运行 `npm test`，确认**全绿**
- [ ] 2.3 确认未破坏既有测试（`greetings.test.js`、`conversationState.test.js` 等）

验证：`npm test` 全通过，用例总数 ≥ 23（原有）+ 13（新增）。

---

## 阶段 3：接入 ChatPage

- [ ] 3.1 移除 `localStorage['currentSceneId']` 持久化、`switchScene`、场景按钮区块
- [ ] 3.2 `currentSceneId` 改由 `sceneIdFor()` 初始化
- [ ] 3.3 加 `setTimeout(msUntilNextFlip())` 精确定时 + 递归重排
- [ ] 3.4 加 `visibilitychange` 监听，回前台重判定
- [ ] 3.5 `scene-day` 类改由 `isDaytime` 决定（去掉 `scene.id === 6`）
- [ ] 3.6 开关关闭时不判定、固定 `scene_id = 1`、不挂 `scene-day`
- [ ] 3.7 确认 `getGreeting()` 的 `useMemo` 逻辑未被破坏（历史修复，勿回退）

验证：`npm run build` 通过（预期约 14-16 秒，52 modules 左右）。

---

## 阶段 4：设置页新增「场景」区块

- [ ] 4.1 在"联网搜索"与"系统能力诊断"之间插入 `settings-section`
- [ ] 4.2 checkbox 绑定 `getSceneFollowTime()` / `setSceneFollowTime()`
- [ ] 4.3 **即时生效**：不并入 `handleSave`，不写 `.env`
- [ ] 4.4 若需 checkbox 样式则补 CSS（复用暖橙主题色）
- [ ] 4.5 复查：差量中**不含**该字段提交给 `/api/settings`

验证：构建通过；`.env` 未被改动（`git diff` 确认无 `.env` 变化）。

---

## 阶段 5：prompts.py 加契约注释

- [ ] 5.1 在 `prompts.py:55` 上方加注释，指向 `daytime.js` 的 `DAY_START_HOUR`/`DAY_END_HOUR`
- [ ] 5.2 **不改动输出文本本身**（避免回归）
- [ ] 5.3 加固守门测试：断言 `build_relative_time_context()` 输出含 `06:00` 与 `18:00`

验证：该守门测试通过。

---

## 阶段 6：全量验证

- [ ] 6.1 前端 `npm test` 全绿
- [ ] 6.2 前端 `npm run build` 通过
- [ ] 6.3 后端相关测试通过（守门测试）
- [ ] 6.4 `git status` 复查：只动了约定的 5 个文件
- [ ] 6.5 手工验证清单（规格 §五 14-19）中可自动化的部分

---

## 回滚方案

只需 `<file>` 三个文件 + 删两个新文件：

```bash
git -C fronted/arcueid_daily_50 checkout -- fronteddd/src/pages/ChatPage.jsx fronteddd/src/pages/SettingsPage.jsx
rm fronted/arcueid_daily_50/fronteddd/src/daytime.js fronted/arcueid_daily_50/fronteddd/src/daytime.test.js
git checkout -- agent_core/prompts.py
```

注意：`ChatPage.jsx` / `SettingsPage.jsx` 回滚会**同时丢弃你自己之前的未提交改动**。若要精细回滚，应先备份这两个文件。

---

## 风险与注意事项

1. **`ChatPage.jsx` 与 `SettingsPage.jsx` 已有你的未提交改动** —— 我只能在其当前内容上追加/替换局部片段，不整体重写。改动前先读取最新内容。
2. **`storyData` 只有 2 条数据**，`sceneIdFor()` 返回 6 时依赖 id=6 存在；若被删则需兜底到 id=1。
3. **`getGreeting()` 时机修复不可回退** —— 它是 `useMemo` 在渲染期求值，别在改场景逻辑时误动。
4. **构建一次只跑一个** —— 历史教训：并发 build 同写 `dist/` 会互相干扰。
5. **Bash 需 `export PATH="/usr/bin:/bin:$PATH"`**，否则命令根本不执行（静默 exit 127）。
