"""memory_extract 提炼器的纯逻辑单元测试（fake provider，离线，无需 API key）。

覆盖：JSON 解析兜底链、字段归一化、prompt 内容、异常隔离、env 构建降级。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_core.contracts import ProviderResponse
from agent_core.memory_extract import (
    DEFAULT_BASE_URL,
    DEFAULT_DURATION,
    DEFAULT_MAX_TOKEN,
    DEFAULT_TIMEOUT,
    ExtractedMemory,
    MAX_CONTENT_LENGTH,
    MAX_MEMORIES,
    MemoryExtractor,
    MemoryExtractionError,
    SEMANTIC_MAX_TOKEN,
    SEMANTIC_SYSTEM_PROMPT,
    SemanticMemoryExtractor,
    build_extractor_from_env,
    build_semantic_extractor_from_env,
    render_turn_packet,
)
from agent_core.memory_turn import MemoryRef, MemoryTurnPacket


class _FakeProvider:
    """手写 fake 提炼后端：可配置返回文本 / 抛异常，并记录每次调用。"""

    def __init__(self, text: str = "", error: Exception | None = None):
        self._text = text
        self._error = error
        self.calls: list[list[dict]] = []
        self.temperatures: list[float | None] = []
        self.max_tokens_seen: list[int | None] = []

    async def complete(self, message, *, temperature=None, max_tokens=None):
        self.calls.append(message)
        self.temperatures.append(temperature)
        self.max_tokens_seen.append(max_tokens)
        if self._error:
            raise self._error
        return ProviderResponse(message={"content": self._text}, usage={})


class _ExhaustionProvider:
    """模拟「推理占满预算」：前 exhaust_calls 次返回空正文 + 输出顶满 max_tokens，
    之后返回正常 JSON。用来测动态 token 分配——预算被吃满时应自动加码重试。"""

    def __init__(self, good_text: str, exhaust_calls: int = 1):
        self.good_text = good_text
        self.exhaust_calls = exhaust_calls
        self.calls = 0
        self.max_tokens_seen: list[int] = []

    async def complete(self, message, *, temperature=None, max_tokens=None):
        self.calls += 1
        budget = max_tokens or 512
        self.max_tokens_seen.append(budget)
        if self.calls <= self.exhaust_calls:
            return ProviderResponse(
                message={"role": "assistant", "content": ""},
                usage={"completion_tokens": budget, "total_tokens": budget + 100},
            )
        return ProviderResponse(
            message={"role": "assistant", "content": self.good_text},
            usage={"completion_tokens": 200, "total_tokens": 300},
        )


def _make_extractor(text: str = "") -> tuple[MemoryExtractor, _FakeProvider]:
    provider = _FakeProvider(text)
    return MemoryExtractor(provider), provider


class ParseTests(unittest.TestCase):
    """_parse 纯逻辑：兜底链每环都要可测。"""

    def test_normal_json_parse(self):
        text = (
            '{"memories": [{"content": "用户喜欢猫", "conflict_key": "user.preference.likes", '
            '"confidence": 0.9, "importance": 0.8, "duration": "long_term"}]}'
        )
        extractor, _ = _make_extractor()
        memories = extractor._parse(text)
        self.assertEqual(len(memories), 1)
        m = memories[0]
        self.assertIsInstance(m, ExtractedMemory)
        self.assertEqual(m.content, "用户喜欢猫")
        self.assertEqual(m.conflict_key, "user.preference.likes")
        self.assertEqual(m.confidence, 0.9)
        self.assertEqual(m.importance, 0.8)
        self.assertEqual(m.duration, "long_term")
        self.assertEqual(m.source_type, "llm_extracted")

    def test_code_fence_wrapped(self):
        text = (
            '```json\n'
            '{"memories": [{"content": "用户熬夜", "conflict_key": "user.habit.schedule", '
            '"confidence": 0.6, "importance": 0.7, "duration": "habit"}]}\n'
            '```'
        )
        extractor, _ = _make_extractor()
        memories = extractor._parse(text)
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].conflict_key, "user.habit.schedule")

    def test_invalid_json_returns_empty(self):
        extractor, _ = _make_extractor()
        for bad in ("", "this is not json", "{no quotes}", "[]", '{"memories": 42}', '"just a string"'):
            with self.subTest(bad=bad):
                self.assertEqual(extractor._parse(bad), [])

    def test_empty_memories_returns_empty(self):
        extractor, _ = _make_extractor()
        self.assertEqual(extractor._parse('{"memories": []}'), [])

    def test_filters_invalid_slot(self):
        text = (
            '{"memories": ['
            '{"content": "合法槽位", "conflict_key": "user.state.mood", "confidence": 0.5, "importance": 0.5},'
            '{"content": "非法槽位", "conflict_key": "user.secret", "confidence": 0.9, "importance": 0.9}'
            ']}'
        )
        extractor, _ = _make_extractor()
        memories = extractor._parse(text)
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].conflict_key, "user.state.mood")

    def test_normalizes_fields(self):
        text = (
            '{"memories": [{"content": "越界值", "conflict_key": "pet.relation.bond", '
            '"confidence": 1.5, "importance": -0.5, "duration": "forever"}]}'
        )
        extractor, _ = _make_extractor()
        m = extractor._parse(text)[0]
        self.assertEqual(m.confidence, 1.0)  # 截断到 0~1
        self.assertEqual(m.importance, 0.0)
        self.assertEqual(m.duration, DEFAULT_DURATION)  # 未知档位兜底

    def test_missing_or_bad_values_default(self):
        text = (
            '{"memories": [{"content": "缺字段", "conflict_key": "user.task.current", '
            '"confidence": "abc", "importance": null}]}'
        )
        extractor, _ = _make_extractor()
        m = extractor._parse(text)[0]
        self.assertEqual(m.confidence, 0.5)
        self.assertEqual(m.importance, 0.5)

    def test_content_truncated(self):
        long_content = "x" * (MAX_CONTENT_LENGTH + 100)
        text = (
            '{"memories": [{"content": "%s", "conflict_key": "user.preference.likes", '
            '"confidence": 0.8, "importance": 0.8}]}' % long_content
        )
        extractor, _ = _make_extractor()
        m = extractor._parse(text)[0]
        self.assertEqual(len(m.content), MAX_CONTENT_LENGTH)

    def test_max_memories_cap(self):
        items = ",".join(
            '{"content": "m%d", "conflict_key": "user.state.mood", "confidence": 0.5, "importance": 0.5}' % i
            for i in range(10)
        )
        extractor, _ = _make_extractor()
        memories = extractor._parse('{"memories": [%s]}' % items)
        self.assertEqual(len(memories), MAX_MEMORIES)


class ExtractTests(unittest.IsolatedAsyncioTestCase):
    """async extract()：走完整链路 + 隔离保证。"""

    async def test_extract_happy_path(self):
        text = (
            '{"memories": [{"content": "用户在赶开题报告", "conflict_key": "user.task.current", '
            '"confidence": 0.9, "importance": 0.9, "duration": "session"}]}'
        )
        extractor, provider = _make_extractor(text)
        memories = await extractor.extract("今晚在赶开题报告")
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].conflict_key, "user.task.current")
        self.assertEqual(memories[0].duration, "session")

    async def test_prompt_includes_history_and_reply(self):
        extractor, provider = _make_extractor('{"memories": []}')
        history = [
            {"role": "user", "content": "昨晚我熬夜了"},
            {"role": "assistant", "content": "早点休息呀"},
        ]
        await extractor.extract(
            "今晚还要赶开题报告",
            assistant_reply="好的，我记下了~",
            history=history,
        )
        self.assertEqual(len(provider.calls), 1)
        messages = provider.calls[0]
        self.assertEqual(messages[0]["role"], "system")
        user_message = messages[1]["content"]
        self.assertIn("user: 昨晚我熬夜了", user_message)
        self.assertIn("assistant: 早点休息呀", user_message)
        self.assertIn("user: 今晚还要赶开题报告", user_message)
        self.assertIn("assistant: 好的，我记下了~", user_message)
        self.assertIn("user.state.mood", user_message)  # 槽位表在场

    async def test_temperature_passed_through(self):
        extractor, provider = _make_extractor('{"memories": []}')
        await extractor.extract("hi")
        self.assertEqual(provider.temperatures, [0.2])

    async def test_provider_error_returns_empty(self):
        provider = _FakeProvider(error=RuntimeError("network down"))
        extractor = MemoryExtractor(provider)
        memories = await extractor.extract("hi")
        self.assertEqual(memories, [])

    async def test_provider_empty_content_returns_empty(self):
        extractor, _ = _make_extractor(text="")
        self.assertEqual(await extractor.extract("hi"), [])
        extractor2, _ = _make_extractor(text="   ")
        self.assertEqual(await extractor2.extract("hi"), [])


class LoggingTests(unittest.IsolatedAsyncioTestCase):
    """提炼日志：成功/失败/空响应/零结果都要能看到中文工作日志。"""

    async def test_success_logs_start_and_complete(self):
        text = (
            '{"memories": [{"content": "用户在赶开题报告", "conflict_key": "user.task.current", '
            '"confidence": 0.9, "importance": 0.9, "duration": "session"}]}'
        )
        extractor, _ = _make_extractor(text)
        with self.assertLogs("agent_core.memory.extract", level="INFO") as captured:
            memories = await extractor.extract("今晚在赶开题报告", assistant_reply="好的~")
        output = "\n".join(captured.output)
        self.assertIn("提炼开始", output)
        self.assertIn("提炼完成", output)
        self.assertIn("共 1 条记忆", output)
        self.assertIn("user.task.current×1", output)  # 槽位分布
        self.assertEqual(len(memories), 1)

    async def test_empty_result_logs_zero_complete(self):
        extractor, _ = _make_extractor('{"memories": []}')
        with self.assertLogs("agent_core.memory.extract", level="INFO") as captured:
            memories = await extractor.extract("hi")
        output = "\n".join(captured.output)
        self.assertIn("提炼完成：共 0 条记忆 无", output)
        self.assertEqual(memories, [])

    async def test_provider_error_logs_warning(self):
        provider = _FakeProvider(error=RuntimeError("network down"))
        extractor = MemoryExtractor(provider)
        with self.assertLogs("agent_core.memory.extract", level="WARNING") as captured:
            memories = await extractor.extract("hi")
        output = "\n".join(captured.output)
        self.assertIn("提炼调用失败", output)
        self.assertIn("network down", output)
        self.assertEqual(memories, [])

    async def test_empty_response_logs_warning(self):
        extractor, _ = _make_extractor(text="")
        with self.assertLogs("agent_core.memory.extract", level="WARNING") as captured:
            memories = await extractor.extract("hi")
        output = "\n".join(captured.output)
        self.assertIn("提炼响应为空", output)
        self.assertEqual(memories, [])


class DynamicBudgetTests(unittest.IsolatedAsyncioTestCase):
    """动态 token 分配：推理占满预算（正文为空 + 输出顶满）时自动加码重试。"""

    async def test_escalates_then_succeeds(self):
        text = (
            '{"memories": [{"content": "用户最近经常熬夜", "conflict_key": "user.habit.schedule", '
            '"confidence": 0.9, "importance": 0.7, "duration": "habit"}]}'
        )
        provider = _ExhaustionProvider(text, exhaust_calls=1)
        extractor = MemoryExtractor(provider)
        with self.assertLogs("agent_core.memory.extract", level="WARNING") as captured:
            memories = await extractor.extract("熬夜赶工")
        self.assertEqual(provider.max_tokens_seen, [512, 1024])  # 512 被吃满 → ×2
        self.assertIn("动态加大", "\n".join(captured.output))
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].conflict_key, "user.habit.schedule")

    async def test_stops_at_ceiling(self):
        provider = _ExhaustionProvider('{"memories": []}', exhaust_calls=99)
        extractor = MemoryExtractor(provider)
        with self.assertLogs("agent_core.memory.extract", level="WARNING") as captured:
            memories = await extractor.extract("熬夜")
        self.assertEqual(provider.max_tokens_seen, [512, 1024, 2048])  # 到上限即止，不再加码
        self.assertEqual(memories, [])
        output = "\n".join(captured.output)
        self.assertEqual(output.count("动态加大"), 2)
        self.assertIn("提炼响应为空", output)

    async def test_custom_base_uses_env_budget(self):
        provider = _ExhaustionProvider('{"memories": []}', exhaust_calls=99)
        extractor = MemoryExtractor(provider, max_token=1024)
        await extractor.extract("熬夜")
        self.assertEqual(provider.max_tokens_seen, [1024, 2048])  # 从 1024 起步，到 2048 封顶

    async def test_genuinely_empty_does_not_escalate(self):
        extractor, provider = _make_extractor(text="")  # 真·空响应：输出 token 没顶满
        with self.assertLogs("agent_core.memory.extract", level="WARNING") as captured:
            memories = await extractor.extract("hi")
        self.assertEqual(provider.max_tokens_seen, [512])  # 只调一次，不加码
        self.assertIn("提炼响应为空", "\n".join(captured.output))
        self.assertEqual(memories, [])


class EnvTests(unittest.TestCase):
    """build_extractor_from_env：优雅降级 / 配置注入。"""

    def test_without_key_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            env_path = Path(d) / ".env"
            env_path.write_text("EXTRACT_BASE_URL=https://example.com\n", encoding="utf-8")
            # 显式清空 key：override=False 不会覆盖已存在的环境变量
            with mock.patch.dict(os.environ, {"EXTRACT_API_KEY": ""}):
                self.assertIsNone(build_extractor_from_env(env_path))

    def test_with_key_constructs_provider(self):
        with tempfile.TemporaryDirectory() as d:
            env_path = Path(d) / ".env"
            env_path.write_text(
                "EXTRACT_API_KEY=test-key\nEXTRACT_MODEL=test-model\n", encoding="utf-8"
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch(
                    "agent_core_adapters.openai_compatible.OpenAICompatibleProvider"
                ) as FakeAdapter:
                    extractor = build_extractor_from_env(env_path)
        self.assertIsNotNone(extractor)
        FakeAdapter.assert_called_once_with(
            base_url=DEFAULT_BASE_URL,
            api_key="test-key",
            model="test-model",
            max_token=DEFAULT_MAX_TOKEN,
            timeout=DEFAULT_TIMEOUT,
            # 提炼必须开思考：强制思考的模型会把预算先花在推理上，
            # 不开的话正文被截断成空串，表现成"什么都没提炼出来"。
            thinking_enabled=True,
        )
        self.assertIs(extractor.provider, FakeAdapter.return_value)


if __name__ == "__main__":
    unittest.main()


class SemanticExtractorTests(unittest.TestCase):
    """统一候选提炼器：七类结构、来源校验、背景缺口标记。"""

    def _packet(self, **overrides) -> MemoryTurnPacket:
        base = dict(
            turn_id="turn-1",
            occurred_at="2026-10-10T12:00:00+00:00",
            scope_owner="user",
            scope_channel="desktop",
            scope_conversation="conversation",
            user_text="就按这个做，先别部署",
            published_reply="好，先按方案实施，暂不部署",
        )
        base.update(overrides)
        return MemoryTurnPacket(**base)

    def test_parse_accepts_all_seven_kinds(self) -> None:
        items = []
        for kind, status in (
            ("fact", "reported"),
            ("decision", "proposed"),
            ("plan", "in_progress"),
            ("preference", "tentative"),
            ("experience", "observed"),
            ("open_question", "open"),
            ("hypothesis", "untested"),
        ):
            items.append(
                {
                    "content": f"{kind} 类内容",
                    "kind": kind,
                    "content_status": status,
                    "source_types": ["user_statement"],
                    "reason": "因为不想分散",
                }
            )
        extractor = SemanticMemoryExtractor(_FakeProvider(""))
        candidates, rejected = extractor.parse(
            json.dumps({"memories": items}, ensure_ascii=False), packet=self._packet()
        )
        self.assertEqual([c.kind for c in candidates], [i["kind"] for i in items])
        self.assertEqual(rejected, [])

    def test_parse_drops_model_claiming_verified_or_completed(self) -> None:
        extractor = SemanticMemoryExtractor(_FakeProvider(""))
        text = json.dumps(
            {
                "memories": [
                    {
                        "content": "方案已经落地完成",
                        "kind": "plan",
                        "content_status": "completed",
                        "source_types": ["model_inference"],
                    },
                    {
                        "content": "用户在上海",
                        "kind": "fact",
                        "content_status": "verified",
                        "source_types": ["tool_result"],
                    },
                ]
            },
            ensure_ascii=False,
        )
        candidates, rejected = extractor.parse(text, packet=self._packet())
        self.assertEqual(candidates, [])
        self.assertEqual(len(rejected), 2)

    def test_parse_drops_relations_to_ids_outside_the_input(self) -> None:
        extractor = SemanticMemoryExtractor(_FakeProvider(""))
        text = json.dumps(
            {
                "memories": [
                    {
                        "content": "在早前决定上继续",
                        "kind": "decision",
                        "content_status": "proposed",
                        "source_types": ["assistant_suggestion"],
                        "related_ids": ["mem-does-not-exist"],
                    }
                ]
            },
            ensure_ascii=False,
        )
        candidates, rejected = extractor.parse(
            text, packet=self._packet(recalled_memory_refs=[MemoryRef("mem-1", "decision", "accepted")])
        )
        self.assertEqual(candidates, [])
        self.assertIn("mem-does-not-exist", rejected[0]["reason"])

    def test_parse_keeps_relations_that_exist_in_the_input(self) -> None:
        extractor = SemanticMemoryExtractor(_FakeProvider(""))
        text = json.dumps(
            {
                "memories": [
                    {
                        "content": "在早前决定上继续",
                        "kind": "decision",
                        "content_status": "proposed",
                        "source_types": ["assistant_suggestion"],
                        "related_ids": ["mem-1"],
                    }
                ]
            },
            ensure_ascii=False,
        )
        candidates, _ = extractor.parse(
            text, packet=self._packet(recalled_memory_refs=[MemoryRef("mem-1", "decision", "accepted")])
        )
        self.assertEqual(candidates[0].related_ids, ("mem-1",))
        self.assertIn("turn-1", candidates[0].source_refs)

    def test_parse_rejects_a_source_reference_that_does_not_exist(self) -> None:
        """模型随手编一个出处当来源，程序不能采信。"""
        extractor = SemanticMemoryExtractor(_FakeProvider(""))
        text = json.dumps(
            {
                "memories": [
                    {
                        "content": "用户已经采纳统一记忆方案",
                        "kind": "decision",
                        "content_status": "accepted",
                        "source_types": ["user_statement"],
                        "source_refs": ["nonexistent-message"],
                    }
                ]
            },
            ensure_ascii=False,
        )
        candidates, rejected = extractor.parse(
            text, packet=self._packet(user_text="先不要决定是否采用，也不要部署。")
        )
        self.assertEqual(candidates, [])
        self.assertIn("nonexistent-message", rejected[0]["reason"])

    def test_review_rejects_adoption_without_user_evidence(self) -> None:
        """用户明明说「先不要决定」，不能记成「用户已采纳」。"""
        extractor = SemanticMemoryExtractor(_FakeProvider(""))
        text = json.dumps(
            {
                "memories": [
                    {
                        "content": "用户已经采纳统一记忆方案",
                        "kind": "decision",
                        "content_status": "accepted",
                        "source_types": ["user_statement"],
                    }
                ]
            },
            ensure_ascii=False,
        )
        packet = self._packet(user_text="先不要决定是否采用，也不要部署。")
        candidates, _ = extractor.parse(text, packet=packet)
        self.assertEqual(len(candidates), 1)
        self._assert_reviewed_adoption(packet, candidates, supported=False)

    def _assert_reviewed_adoption(self, packet, candidates, *, supported):
        from pathlib import Path
        import tempfile
        from agent_core.memory_processing import MemoryReviewProcessor
        from agent_core.memory_repositories import initialize_memory_repositories
        from agent_core.memory_writes import MemoryWriter
        from agent_core.contracts import Scope
        from support.memory_review import FixedReviewProvider, supported_row
        class Embeddings:
            def embed(self, text):
                return [.1, .2, .3]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repos = initialize_memory_repositories(root/'records.db', root/'vectors', vector_size=3)
            try:
                writer = MemoryWriter(repos, Embeddings())
                row = (supported_row('统一记忆规则', [(packet.turn_id, '统一记忆规则就按这个做')],
                                     subject='统一记忆规则', status='accepted') if supported
                       else {'index': 0, 'verdict': 'unsupported', 'reason': '用户明确要求先不要决定，尚未采纳'})
                provider = FixedReviewProvider([row])
                durable = []
                reviewed, _, result = asyncio.run(MemoryReviewProcessor(writer, provider).prepare(
                    packet, candidates, [], persist=durable.append))
                self.assertEqual(provider.calls, 1)
                scope = Scope(packet.scope_owner, packet.scope_channel, packet.scope_conversation, 'companion')
                saved = writer.save_semantic_candidates(scope, reviewed, packet=packet, turn_id=packet.turn_id)
                from contextlib import closing
                with closing(repos.connect()) as db:
                    rows = [dict(r) for r in db.execute('SELECT * FROM memory_records')]
                self.assertEqual(len(rows), int(supported), result.as_dict())
                if supported:
                    self.assertEqual(rows[0]['content'], '统一记忆规则')
                    self.assertEqual(rows[0]['content_status'], 'accepted')
                    self.assertEqual(rows[0]['status'], 'active')
                else:
                    self.assertEqual(saved, [])
            finally:
                repos.close()
                import gc
                gc.collect()

    def test_parse_keeps_adoption_that_the_user_actually_expressed(self) -> None:
        extractor = SemanticMemoryExtractor(_FakeProvider(""))
        text = json.dumps(
            {
                "memories": [
                    {
                        "content": "统一记忆规则",
                        "kind": "decision",
                        "content_status": "accepted",
                        "source_types": ["user_statement"],
                    }
                ]
            },
            ensure_ascii=False,
        )
        packet = self._packet(user_text="统一记忆规则就按这个做，先别部署")
        candidates, _ = extractor.parse(text, packet=packet)
        self._assert_reviewed_adoption(packet, candidates, supported=True)

    def test_prompt_marks_an_unpublished_reply_as_unusable(self) -> None:
        packet = self._packet(published_reply="", outcome_status="cancelled")
        rendered = render_turn_packet(packet)
        self.assertIn("未发布", rendered)

    def test_prompt_lists_recalled_ids_and_background_gaps(self) -> None:
        packet = self._packet(
            recalled_memory_refs=[MemoryRef("mem-1", "decision", "accepted", excerpt="早前决定")],
            completeness="partial",
            omitted_sections=("relevant_history",),
        )
        rendered = render_turn_packet(packet)
        self.assertIn("mem-1", rendered)
        self.assertIn("relevant_history", rendered)
        self.assertIn("不要因此推断", rendered)

    def test_system_prompt_forbids_inventing_missing_definitions(self) -> None:
        self.assertIn("open_question", SEMANTIC_SYSTEM_PROMPT)
        self.assertIn("不要编造用户动机", SEMANTIC_SYSTEM_PROMPT)

    def test_extract_reports_provider_failure_instead_of_returning_empty(self) -> None:
        """调用失败必须**报错**，不能返回空列表。

        旧行为（返回 []）让队列把失败记成「这轮没内容」并结束作业，一次断网
        就等于这一轮永远没被记住。这条用例钉的是新行为：失败要能被上层看见。
        """
        extractor = SemanticMemoryExtractor(_FakeProvider(error=RuntimeError("boom")))
        with self.assertRaises(MemoryExtractionError):
            asyncio.run(extractor.extract(self._packet()))

    def test_extract_reports_invalid_structure_instead_of_returning_empty(self) -> None:
        extractor = SemanticMemoryExtractor(_FakeProvider("这不是 JSON"))
        with self.assertRaises(MemoryExtractionError):
            asyncio.run(extractor.extract(self._packet()))

    def test_extract_returns_empty_only_when_the_model_says_there_is_nothing(self) -> None:
        """合法空候选（模型明确说没有值得记的）才是「确实没有内容」。"""
        extractor = SemanticMemoryExtractor(_FakeProvider('{"memories": []}'))
        self.assertEqual(asyncio.run(extractor.extract(self._packet())), [])

    def test_extract_returns_candidates_from_provider_text(self) -> None:
        text = json.dumps(
            {
                "memories": [
                    {
                        "content": "决定让主模型参与候选提炼",
                        "kind": "decision",
                        "content_status": "accepted",
                        "source_types": ["user_statement"],
                        "reason": "不想让陪伴记忆和项目记忆分裂",
                    }
                ]
            },
            ensure_ascii=False,
        )
        extractor = SemanticMemoryExtractor(_FakeProvider(text))
        candidates = asyncio.run(extractor.extract(self._packet()))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].content_status, "accepted")
        self.assertEqual(candidates[0].reason, "不想让陪伴记忆和项目记忆分裂")
        self.assertEqual(candidates[0].origin, "model_candidate")

    def test_build_from_env_uses_the_main_answer_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text(
                "GEMINI_API_KEY=answer-key\nGEMINI_MODEL=answer-model\n"
                "GOOGLE_GEMINI_BASE_URL=https://example.invalid\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch(
                    "agent_core_adapters.openai_compatible.OpenAICompatibleProvider"
                ) as FakeAdapter:
                    extractor = build_semantic_extractor_from_env(env)
        self.assertIsNotNone(extractor)
        FakeAdapter.assert_called_once_with(
            base_url="https://example.invalid",
            api_key="answer-key",
            model="answer-model",
            max_token=SEMANTIC_MAX_TOKEN,
            timeout=DEFAULT_TIMEOUT,
            thinking_enabled=True,
        )

    def test_main_answer_model_timeout_matches_b_single_call_budget(self) -> None:
        self.assertEqual(DEFAULT_TIMEOUT, 60.0)

    def test_build_from_env_returns_none_without_the_answer_model(self) -> None:
        """缺主回答模型配置就返回 None —— 不静默退回旧的陪伴提炼模型。"""
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("EXTRACT_API_KEY=extract-key\nEXTRACT_MODEL=other\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertIsNone(build_semantic_extractor_from_env(env))
