"""阶段措辞的测试替身：一个只会回"当前阶段那句话"的假模型。

动态措辞改造之后，过程气泡必须由模型现场生成；测试里不能真的调模型，所以统一用这里的
:class:`StagePhraseProvider`。它做三件事：

1. 认出阶段措辞生成器的提示词（系统提示里带"最近已经说过的话"这一条）；
2. 按提示词里的"当前阶段"回一句对应的测试话术（**只存在于测试里**，生产路径没有句库）；
3. 把每次收到的提示词记下来，方便断言"只喂了安全信息"（没有工具名、路径、正文）。

需要"重复句 / 不合格句 / 超时 / 报错"这些反例时，用 ``replies`` 指定前几次的返回值，
或者开 ``fail`` / ``delay``。
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent_core.contracts import ProviderResponse
from agent_core.provider import LLMProvider
from agent_core.steward import _PHASE_BRIEF

#: 每个阶段一句测试话术。它们是**测试替身**，不是产品里的固定短句库。
STAGE_TEST_LINES: dict[str, str] = {
    "searching": "我在查这件事，先看看有什么线索。",
    "materials_found": "材料已经回来了，我对着看一看。",
    "reading_source": "我在读这份原文，核对里面的说法。",
    "source_read": "这份已经取回来了，我再翻下一份。",
    "redirect": "这条线索不够，我换个来源再看。",
    "conflict": "两边说法对不上，我再核一遍。",
    "work_inspect": "我先看项目里的文件，弄清问题在哪。",
    "work_edit": "我开始改文件了，改完会检查。",
    "work_verify": "我在跑验证，看看改动过不过。",
    "work_deliver": "改动整理好了，我把验证一起交给你。",
    "work_repair": "上一版没过，我改一版再验。",
}

_BRIEF_TO_STAGE = {brief: stage for stage, brief in _PHASE_BRIEF.items()}

#: 阶段措辞生成器的系统提示里必有这一条；用它把"措辞调用"和别的模型调用分开。
STAGE_PROMPT_MARKER = "最近已经说过的话"


class StagePhraseProvider(LLMProvider):
    """假模型：阶段措辞调用回一句测试话术，其余调用回一句普通回复。"""

    def __init__(
        self,
        *,
        lines: dict[str, str] | None = None,
        replies: list[str] | None = None,
        delay: float = 0.0,
        fail: bool = False,
        fallback: str = "好的，我知道了。",
    ) -> None:
        self.lines = dict(STAGE_TEST_LINES if lines is None else lines)
        self.replies = list(replies or [])
        self.delay = float(delay)
        self.fail = fail
        self.fallback = fallback
        self.calls: list[list[dict[str, Any]]] = []

    # ── 观测 ────────────────────────────────────────────────────────
    @staticmethod
    def is_stage_prompt(messages: list[dict[str, Any]]) -> bool:
        return any(
            STAGE_PROMPT_MARKER in str(message.get("content") or "") for message in messages
        )

    @property
    def stage_calls(self) -> list[list[dict[str, Any]]]:
        return [call for call in self.calls if self.is_stage_prompt(call)]

    @staticmethod
    def stage_of(messages: list[dict[str, Any]]) -> str:
        for message in messages:
            for line in str(message.get("content") or "").splitlines():
                if line.startswith("- 当前阶段："):
                    return _BRIEF_TO_STAGE.get(line.split("：", 1)[1].strip(), "")
        return ""

    def stage_prompts_seen(self) -> list[str]:
        return [self.stage_of(call) for call in self.stage_calls]

    # ── 模型 ────────────────────────────────────────────────────────
    async def complete(self, messages, *, tools=None, max_tokens=None, temperature=None):
        self.calls.append(list(messages))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("provider down")
        if not self.is_stage_prompt(messages):
            return ProviderResponse(
                message={"role": "assistant", "content": self.fallback}, usage={},
            )
        if self.replies:
            content = self.replies.pop(0)
        else:
            content = self.lines.get(self.stage_of(messages), self.fallback)
        return ProviderResponse(message={"role": "assistant", "content": content}, usage={})


def stage_line(stage: str) -> str:
    """取某个阶段的测试话术（断言里用，避免再抄一遍字面量）。"""
    return STAGE_TEST_LINES[stage]
