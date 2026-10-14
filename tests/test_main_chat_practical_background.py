"""实施工作 A 的离线验证：关键背景要一路传到回答、核查、重写与收尾。

对应计划《主聊天三项实用能力：接续实施与交接说明》「新增离线验证限定三组」第 1 组：
早期保存的「暂不部署」阶段决定，穿插六十轮固定无关对话后，仍必须被主回答、
核查、重写与预算收尾实际收到；公开历史不变；普通回复不增加模型调用。

这些是**接线**证据：用假的、可数的模型检查它实际收到的输入，
不证明模型真的理解了语义。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agent_core.chat_background import (
    BACKGROUND_MARKER,
    BackgroundMemory,
    BackgroundQuote,
    PendingClarification,
    TurnBackground,
    trim_messages,
)
from agent_core.contracts import AgentRequest, ProviderResponse, Scope

# tests/ 不是包（没有 __init__.py），同级模块靠 sys.path 里的 tests 目录进来；
# 复用现有风格的假模型与 build_loop，不另建一套。
import test_search_mainloop as _mainloop  # noqa: E402

ScriptedProvider = _mainloop.ScriptedProvider
build_loop = _mainloop.build_loop
fact_gate = _mainloop.fact_gate
main_answer_request = _mainloop.main_answer_request
search_spec = _mainloop.search_spec
SCOPE = _mainloop.SCOPE
FACT_TEXT = _mainloop.FACT_TEXT

#: 「暂不部署」这条阶段决定的原话与本轮要传下去的限制。
DECISION_QUOTE = "部署方案暂时先不部署"
CURRENT_QUESTION = "查一下这个部署方案的准备事项，我现在应该怎么安排？"

#: 六十轮固定无关对话（不调模型、不调后台，只是历史填充物）。
FILLER_ROUNDS = 60
FILLER_USER = "今天天气怎么样？"
FILLER_ASSISTANT = "还行，出门带件外套。"


def full_background(user_text: str = CURRENT_QUESTION) -> dict[str, Any]:
    """一份完整的本轮关键背景：现行决定 + 原话 + 待澄清事项 + 缺口。"""
    return TurnBackground.build(
        user_text=user_text,
        selected=[BackgroundMemory(
            memory_id="mem-deploy-1",
            content=DECISION_QUOTE,
            kind="decision",
            content_status="accepted",
            version=2,
            layer="warm",
            semantic_judgment={"certainty": "certain"},
        )],
        quotes=[BackgroundQuote(role="user", text=DECISION_QUOTE, locator="turn-local:1")],
        pending=[PendingClarification(
            candidate_id="job-77::item[0]",
            job_id="job-77",
            item_path="item[0]",
            quote="这个先放着",
            reason="用户未指明对象",
            suggested_question="先放下的是正式部署还是本机演练？",
            target_id="mem-deploy-1",
            target_version=2,
        )],
        gaps=["较早的历史背景"],
    ).as_payload()


def payload_text(request: list[dict[str, Any]]) -> str:
    return json.dumps(request, ensure_ascii=False, default=str)


def long_filler_history(*, rounds: int = FILLER_ROUNDS) -> list[dict[str, Any]]:
    """六十轮固定无关对话，每轮都带一个编号标记，便于断言「早期约束丢了」。"""
    history: list[dict[str, Any]] = []
    for index in range(rounds):
        history.append({"role": "user", "content": f"{FILLER_USER}（第{index + 1}轮）"})
        history.append({"role": "assistant", "content": FILLER_ASSISTANT})
    return history


def requests_with_system(provider: ScriptedProvider, prefix: str) -> list[list[dict[str, Any]]]:
    return [
        request for request in provider.requests
        if any(
            str(message.get("content") or "").startswith(prefix)
            for message in request if message.get("role") == "system"
        )
    ]


def audit_text(request: list[dict[str, Any]]) -> str:
    """核查那次调用的正文：``AnswerAuditor`` 把要核查的内容放在最后一条。"""
    return json.dumps(json.loads(request[-1]["content"]), ensure_ascii=False)


# ── 第 1 组：背景实际传递 ──────────────────────────────────────────


def test_saved_phase_decision_survives_sixty_turns_and_reaches_answer_and_review(tmp_path):
    """早期「暂不部署」在六十轮无关对话之后，主回答与核查都必须实际收到。"""
    provider = ScriptedProvider(["按现行决定，部署先缓一缓；准备事项可以并行推进。"])
    loop = build_loop(tmp_path, provider, gate=fact_gate(provider), tools=(search_spec(),))
    request = AgentRequest(
        CURRENT_QUESTION,
        scope=SCOPE,
        system_prompt="角色提示：你是管家。",
        history=[
            {"role": "user", "content": DECISION_QUOTE},
            {"role": "assistant", "content": "收到，先不部署。"},
            *long_filler_history(),
        ],
        turn_background=full_background(),
    )

    outcome = asyncio.run(loop.run(request))

    assert outcome.status == "completed"
    answer = payload_text(main_answer_request(provider))
    assert BACKGROUND_MARKER in answer, "主回答必须收到关键背景"
    assert DECISION_QUOTE in answer, "较早保存的现行决定不能被后续闲聊顶掉"
    assert CURRENT_QUESTION in answer, "当前请求必须与背景同时在场"
    assert "job-77::item[0]" in answer, "待澄清事项要一起传下去"

    reviews = requests_with_system(provider, "你是最终核查器")
    assert reviews, "有外部证据就必须过核查"
    audit = audit_text(reviews[-1])
    assert DECISION_QUOTE in audit, "核查也要看得见同一条限制"
    assert "job-77::item[0]" in audit, "核查要看得见待澄清事项"
    assert "关键背景（本轮主回答同源）" in audit, "核查背景与主回答必须是同一份来源"
    background_block = audit.split("关键背景（本轮主回答同源）", 1)[1]
    assert BACKGROUND_MARKER not in background_block, (
        "背景已作为独立一段给出，不得再从 working 的 system 消息里重复切一份"
    )


def test_public_history_keeps_only_what_the_user_and_assistant_actually_said(tmp_path):
    """删减只作用于发给模型的副本：公开历史与用户消息一个都不多、一个都不少。"""
    provider = ScriptedProvider(["好的。"])
    loop = build_loop(tmp_path, provider, gate=fact_gate(provider))
    history = [
        {"role": "user", "content": DECISION_QUOTE},
        {"role": "assistant", "content": "收到，先不部署。"},
        *long_filler_history(),
    ]
    request = AgentRequest(
        CURRENT_QUESTION, scope=SCOPE, history=history, turn_background=full_background(),
    )

    asyncio.run(loop.run(request))

    assert len(loop.public_messages) == len(history) + 2, "公开历史 = 传入历史 + 本轮用户 + 本轮答复"
    # 传入历史逐条原样保留，末尾是本轮用户原话与一条答复。
    assert [item["content"] for item in loop.public_messages[:-2]] == [
        item["content"] for item in history
    ]
    assert loop.public_messages[-2]["content"] == CURRENT_QUESTION
    assert loop.public_messages[-1]["role"] == "assistant"
    assert loop.public_messages[-1]["content"]
    # 背景与内部删减说明都只进模型副本，绝不进公开历史。
    assert BACKGROUND_MARKER not in json.dumps(loop.public_messages, ensure_ascii=False)
    assert "[HISTORICAL" not in json.dumps(loop.public_messages, ensure_ascii=False)


def test_plain_reply_adds_no_extra_model_call(tmp_path):
    """普通回复（无外部证据）只花主回答这一次调用，澄清登记是零额外调用。"""
    provider = ScriptedProvider(["今天天气不错。"])
    loop = build_loop(tmp_path, provider, gate=fact_gate(provider))
    request = AgentRequest(
        "今天天气怎么样？", scope=SCOPE, history=long_filler_history(),
        turn_background=TurnBackground.build(
            user_text="今天天气怎么样？",
            selected=[BackgroundMemory("mem-deploy-1", DECISION_QUOTE, "decision", "accepted")],
            notes=["背景只搬运已收到的事实，不是新的决定来源。"],
        ).as_payload(),
    )

    outcome = asyncio.run(loop.run(request))

    assert outcome.status == "completed"
    assert len(provider.requests) == 1, "普通回复不增加复查、整理或澄清调用"
    assert not requests_with_system(provider, "你是最终核查器")
    assert not requests_with_system(provider, "你是检索材料整理器")


def test_budget_trims_history_but_never_the_background_or_current_request(tmp_path):
    """预算放不下时从最近的完整组往前删旧历史，背景与当前请求必须留住并如实报缺口。"""
    provider = ScriptedProvider(["按现行决定，先不部署。"])
    loop = build_loop(tmp_path, provider, gate=fact_gate(provider), tools=(search_spec(),))
    request = AgentRequest(
        CURRENT_QUESTION,
        scope=SCOPE,
        system_prompt="角色提示：你是管家。",
        history=[
            {"role": "user", "content": f"较早约束-{index}：{DECISION_QUOTE}"}
            if index == 0 else {"role": "user", "content": f"无关闲聊-{index}"}
            for index in range(40)
            for _ in (0, 1)
        ],
        turn_background=full_background(),
    )
    # 上面每轮两条：40 组「用户 + 助手」。
    request.history[:] = [
        message
        for index in range(40)
        for message in (
            {"role": "user", "content": (
                f"较早约束-{index}：{DECISION_QUOTE}" if index == 0 else f"无关闲聊-{index}"
            )},
            {"role": "assistant", "content": f"回复-{index}"},
        )
    ]
    original = list(request.history)

    asyncio.run(loop.run(request))

    assert request.history == original, "删减不得改动传入的历史列表"
    answer = payload_text(main_answer_request(provider))
    assert DECISION_QUOTE in answer
    assert CURRENT_QUESTION in answer


# ── 删减本身的确定性边界 ───────────────────────────────────────────


def test_trim_drops_whole_earlier_groups_without_mutating_input(tmp_path):
    """删减按完整组进行、不改输入，并如实交出口径内的缺口。

    .. warning::

       本测试只覆盖**已经成立**的行为。``protected`` 参数按 ``id()`` 认同一个
       对象（见 ``trim_messages`` 的实现说明），而 ``_message_groups`` 内部先对每条
       消息做了一次 ``dict(message)`` 拷贝，于是**传进来的原对象永远匹配不上**
       ``keep_ids``：即使传的是 ``messages`` 里同一个 dict，被保护的消息也会被删掉。

       实测（同对象传入）::

            messages = [system, u1, a1, u2, a2]
            protected = [messages[0], messages[-2], messages[-1]]
            trim_messages(..., protected=protected)[0]
            -> [u1, a1, u2, a2]        # system 被丢掉，而它是角色与权限

       这是生产代码的缺陷，不是本测试的期望行为。此处**不**断言保护项留存，
       以免用一个假期望把缺陷固定下来；缺陷已单独报告给负责人。
    """
    messages = [
        {"role": "system", "content": "角色与权限" * 40},
        {"role": "user", "content": "第一轮提问" * 40},
        {"role": "assistant", "content": "第一轮回复" * 40},
        {"role": "user", "content": "第二轮提问" * 40},
        {"role": "assistant", "content": "第二轮回复" * 40},
        {"role": "user", "content": "第三轮提问" * 40},
        {"role": "assistant", "content": "第三轮回复" * 40},
    ]
    before = [dict(message) for message in messages]

    # 七条全放得下需要 1400；给 1000，最早的完整组必须被整组丢掉。
    kept, gaps = trim_messages(messages, budget_tokens=1000, estimate=lambda rows: len(rows) * 200)

    assert messages == before, "trim_messages 必须只作用于返回的副本"
    contents = [message["content"] for message in kept]
    assert "第二轮提问" * 40 in contents and "第三轮回复" * 40 in contents
    assert "第一轮提问" * 40 not in contents, "放不下时从最早的完整组往前删"
    assert gaps == ["较早的 1 组对话"], "删了哪一组要如实写明，不能静默裁掉"


def test_trim_keeps_tool_call_paired_with_its_result(tmp_path):
    """工具调用与回执要么整组留、要么整组删，绝不制造孤立 tool 消息。"""
    messages = [
        {"role": "system", "content": "角色" * 100},
        {"role": "user", "content": "查一下", },
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "工具结果" * 50},
        {"role": "user", "content": "再问一句"},
        {"role": "assistant", "content": "好的"},
    ]

    kept, _ = trim_messages(messages, budget_tokens=400, estimate=lambda rows: len(rows) * 200,
                            protected=[messages[-1]])

    call_ids = {message.get("tool_call_id") for message in kept if message.get("role") == "tool"}
    assistant_calls = {
        call["id"] for message in kept for call in (message.get("tool_calls") or [])
    }
    assert call_ids == assistant_calls, "留下的工具回执必须都有对应调用"


# ── 背景对象自身的兼容与优先级 ─────────────────────────────────────


def test_background_payload_roundtrip_and_legacy_payloads_stay_readable():
    """背景可往返序列化；旧包、缺字段包、坏包一律按「没有背景」处理。"""
    payload = full_background()
    restored = TurnBackground.from_payload(payload)

    assert restored.user_text == CURRENT_QUESTION
    assert [item.content for item in restored.selected] == [DECISION_QUOTE]
    assert restored.selected[0].memory_id == "mem-deploy-1"
    assert restored.pending[0].suggested_question == "先放下的是正式部署还是本机演练？"
    assert restored.as_payload()["version"] == payload["version"]

    assert TurnBackground.from_payload(None).is_empty
    assert TurnBackground.from_payload("not json").is_empty
    assert TurnBackground.from_payload({"user_text": "在"}).selected == ()
    # 旧调用（不给 turn_background）与显式空背景行为一致。
    assert TurnBackground.from_payload({}).is_empty


def test_current_decisions_exclude_replaced_ones_but_keep_unknown_status():
    """现行决定排除已替代/已否决；缺判断字段的旧记录继续标明缺失，不被补成确定。"""
    background = TurnBackground.build(selected=[
        BackgroundMemory("old", "部署方案已决定立即部署", "decision", "superseded"),
        BackgroundMemory("now", DECISION_QUOTE, "decision", "accepted"),
        BackgroundMemory("legacy", "排期方案暂不调整", "decision", ""),
    ])

    current = {item.memory_id for item in background.current_decisions}
    assert current == {"now", "legacy"}
    rendered = background.render()
    assert "状态=superseded" in rendered, "状态要原样给出，不能省略"
    assert "未知状态" in rendered, "缺状态的旧记录标缺，不替它判断"


def test_render_keeps_critical_items_first_and_marks_the_truncation():
    """超限时只在够不关键的尾部截断，并如实写明；关键决定与当前请求已保留。"""
    background = TurnBackground.build(
        user_text=CURRENT_QUESTION,
        selected=[BackgroundMemory("m1", DECISION_QUOTE, "decision", "accepted")],
        quotes=[BackgroundQuote("assistant", "很长的助手原话" * 400)],
    )

    rendered = background.render(limit=200)

    assert rendered.startswith(f"当前用户原话：{CURRENT_QUESTION}")
    assert DECISION_QUOTE in rendered
    assert "已省略" in rendered and "关键决定与当前请求已保留" in rendered


def test_background_section_states_it_is_not_evidence_of_execution():
    """固定说明必须写明背景不是执行证据，避免据此声称动作已经做过。"""
    from agent_core.chat_background import background_section

    section = background_section(["一次性要求只留本轮。"])
    joined = "\n".join(section)

    assert "不得据此声称动作已经执行、正在执行或已经停止" in joined
    assert "一次性要求只留本轮。" in joined


def test_same_memory_is_not_injected_twice_and_dedupe_key_is_stable():
    """同一记忆不注入第二份：去重键按内容身份算，不看注入次数。"""
    background = TurnBackground.build(
        user_text=CURRENT_QUESTION,
        selected=[BackgroundMemory("mem-deploy-1", DECISION_QUOTE, "decision", "accepted")],
    )

    assert background.dedupe_key() == ("m:mem-deploy-1",)
    assert background.render_message().count(BACKGROUND_MARKER) == 1


@pytest.mark.parametrize("budget", [50, 500, 2000])
def test_trim_is_a_pure_function_over_the_given_copy(budget):
    """无论预算多少，删减都不改输入、不制造孤立工具消息。"""
    messages = [
        {"role": "system", "content": "角色" * 50},
        {"role": "user", "content": "问题一"},
        {"role": "assistant", "content": "答一"},
        {"role": "user", "content": "问题二"},
        {"role": "assistant", "content": "答二"},
    ]
    before = json.dumps(messages, ensure_ascii=False)

    kept, _ = trim_messages(messages, budget_tokens=budget, estimate=lambda rows: len(rows) * 100)

    assert json.dumps(messages, ensure_ascii=False) == before
    assert all(isinstance(message, dict) for message in kept)


def test_agent_request_keeps_turn_background_optional_for_legacy_callers():
    """旧位置参数调用含义不变；新字段缺省为空。"""
    legacy = AgentRequest("你好", SCOPE)
    assert legacy.turn_background is None
    assert TurnBackground.from_payload(legacy.turn_background).is_empty


def test_search_answer_still_publishes_when_background_is_absent(tmp_path):
    """关掉开关/不传背景时行为与改动前一致：照样按证据回答并给出处。"""
    provider = ScriptedProvider(["Nova 是一家研究机构。"])
    loop = build_loop(tmp_path, provider, gate=fact_gate(provider), tools=(search_spec(),))

    outcome = asyncio.run(loop.run(AgentRequest("Nova 是谁", scope=SCOPE)))

    assert outcome.reply == "Nova 是一家研究机构。"
    assert outcome.sources
    assert FACT_TEXT in payload_text(main_answer_request(provider))
    assert BACKGROUND_MARKER not in payload_text(main_answer_request(provider))
