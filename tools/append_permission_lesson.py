"""把「账号级完全访问」这一课追加到 LESSONS_LEARNED.md。

用 Python 直接写文件而不是过 shell —— 正文里含反引号，`python -c "..."` 会让
bash 先做命令替换、把内容静默吃掉（记忆里有前例，踩过）。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "LESSONS_LEARNED.md"

ENTRY = """
## 2026-09-20 — 账号级完全访问：两条独立的闸门必须一起放

- 问题或决策：需求是"给本机账号开一个完全访问档，开了之后高风险操作不再逐项确认"。
  最直觉的做法是加一条 `AllowAllRiskPolicy` 替掉 `DefaultRiskPolicy`，改完看着就完事了。
  实际不是 —— 这个系统里有**两条互不相干的审批层**，只放一条等于只修了一半，
  而且剩下那一半的症状是"点了没反应"，很难联想到权限。
- 根因或理由：两条闸门的触发方式和落库位置完全不同：
  - **聊天工具层**：`ToolSpec.risk == "high"` → `DefaultRiskPolicy.authorize` 返回
    `confirm` → `loop.py` 落 `ConfirmationRequest` 并暂停本轮（前端弹确认框）。
  - **后台 Host 层**：`control_plan.MANDATORY_APPROVAL_TASK_TYPES`（截屏、系统音量）
    由校验层强制标 `_requires_approval` → `LocalWorkerProvider.execute` 见
    "要求审批但没拿到 `approval_granted`" 就返回 `waiting_approval`。
  关键坑：**审批记录是 provider 的返回状态创建的，不是 Host 主动建的**。所以一个
  永远返回 `completed` 的测试 provider 根本造不出待审批记录，测试会以 `assert 0 == 1`
  这种"看起来是功能没做"的形式失败 —— 排查时先怀疑功能，方向就错了。
- 解决方案：新增 `agent_core/permission_mode.py` 作为**共享**组件
  （`AccountPermissionStore` / `AccountPermissionController` / `AccountAwareRiskPolicy`
  / `FullAccessCleanup`），两条链路各自接上，而不是各写一套：
  - 聊天层：Web / CLI / 子代理创建 `AgentLoop` 时都注入同一个 `risk_policy`。
  - Host 层：`WorkHost` 在**每次实际执行前**按 `WorkRequest.scope.owner_id` 查一次
    模式（不做进程内缓存），全开时走**已有的 checkpoint 通道**写
    `{"approval_granted": True}`，于是 `LocalWorkerProvider` 里那条
    `_requires_approval and not approval_granted` 仍然是活的防线，没有被绕过。
  - `ToolSpec.risk` **一个都没改**，也没全局换成 AllowAll —— 策略是"按 owner 查表后
    决定要不要回落"，风险标记保持原样，切回标准模式立刻恢复确认流。
- 预防/复用：
  - **`FULL_ACCESS_EXCLUSIONS = {"work__approve", "work__plan_submit"}` 是流程短路，
    不是危险评级。** 前者的职责就是"批准一次待审批操作"，放开它等于让工具批准自己；
    后者会派生新的待审批步骤。这类"会自我放大的权限"必须单独列出来，不能跟着模式一起放。
  - **`configured_mode` 和 `effective_mode` 必须分开返回。** 服务不是监听在回环地址时
    配置能存但不会生效，只回一个字段就会让用户以为"我已经关掉确认了"，而实际没有 ——
    错的档位显示比不显示更危险。前端常驻标记读 `effective`（含义是"现在真的不用确认了"），
    设置页把两个都显示出来。
  - **旧确认清理必须按 `owner_id` 圈定，不能按会话或模块。** 账号级开关影响的是所有会话，
    按 `conversation_id` 清理会留下别处的待确认；清理只拒绝不执行，并且**顺序不能反**：
    先终止停在 `waiting_approval` 的计划步骤、再拒绝审批记录。反过来的话
    `decide_approval` 会先把步骤推到终态，计数就丢了。
  - **清理失败要报进结果的 `failed` 字段，不能让整个切换失败。** 模式已经切了，此时抛异常
    会留下"配置是新的、旧确认还在"的中间态，用户更没法判断。
  - 这轮暴露的两个真 bug 值得记：① `FullAccessCleanup` 里同步调用了一个 async 的
    `reject_pending_for_owner`，协程被静默丢弃（普通环境不报错，只是不生效）；修法是无
    事件循环时 `asyncio.run`、有循环时丢线程池。② 前端判"未知 reason 码"用
    `REASON_TEXT[code] || fallback`，而合法的空字符串是 falsy，导致已知的空码走进兜底、
    把后端英文原文漏给了用户；改用 `Object.prototype.hasOwnProperty.call` 先判存在。
"""


def main() -> int:
    text = TARGET.read_text(encoding="utf-8")
    if "## 2026-09-20 — 账号级完全访问" in text:
        print("ENTRY ALREADY PRESENT, skipping")
        return 0
    if not text.endswith("\n"):
        text += "\n"
    text += ENTRY
    with io.open(TARGET, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    print("appended; new size", len(text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
