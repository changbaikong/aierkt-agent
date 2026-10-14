"""候选侧采纳证据必须绑到「这条候选事项」（B3-R1 候选侧）。

背景：``adoption_evidenced`` 对**本轮用户原话**不做事项绑定（只有历史消息才
逐句绑定），于是本轮里任意一句「同意」都能被模型拿来给另一件事盖章：
「同意A；B你再分析一下」会把 B 也盖成 accepted。

这里锁定两块行为，缺一不可：

1. **不能张冠李戴**：采纳表达说的是别的事时，候选必须退回 proposed。
2. **不能误杀真采纳**：指代式采纳（「就按这个做」）和显式决定
   （「改用／一律／暂不部署」）必须仍然被认成 accepted。

第 2 条同样重要 —— 把绑定做严一点很容易顺手把「不把用户确认当入库门槛」
的既有用例打回 proposed，所以正反两向都在这里断言。
"""
from __future__ import annotations

import gc
import asyncio
import json
import os
import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_core.contracts import Scope
from agent_core.memory_extract import SemanticMemoryExtractor
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_turn import turn_from_messages
from agent_core.memory_writes import MemoryWriter
from agent_core.memory_processing import MemoryReviewProcessor
from agent_core.memory_semantic import original_sources
from support.memory_review import FixedReviewProvider, supported_row


class _Embeddings:
    """固定向量：这些用例只关心状态与生命周期，不关心语义相似度。"""

    def embed(self, _text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class _ExtractorCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="memory-horizon-lifecycle-"))
        self._saved_env = {
            name: os.environ.get(name) for name in ("QDRANT_MODE", "MEMORY_SEMANTIC_ENABLED")
        }
        os.environ["QDRANT_MODE"] = "local"
        os.environ["MEMORY_SEMANTIC_ENABLED"] = "true"
        self.scope = Scope("review-user", "web", "horizon-lifecycle", "companion")
        self.extractor = SemanticMemoryExtractor(None)

    def tearDown(self) -> None:
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        gc.collect()
        shutil.rmtree(self.root, ignore_errors=True)

    def _packet(self, text: str):
        return turn_from_messages(
            turn_id="turn-1",
            occurred_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
            scope=self.scope,
            user_text=text,
            published_reply="收到",
        )

    def _candidate_status(self, user_text: str, content: str, status: str = "accepted", *, expected='accepted') -> str:
        packet = self._packet(user_text)
        raw = json.dumps(
            {
                "memories": [
                    {
                        "content": content,
                        "kind": "decision",
                        "content_status": status,
                        "source_types": ["user_statement"],
                        "source_refs": [packet.turn_id],
                    }
                ]
            },
            ensure_ascii=False,
        )
        candidates, _suggestions, _rejected = self.extractor.parse_maintenance(raw, packet=packet)
        self.assertEqual(len(candidates), 1, "这条候选应该被解析出来（内容本身不丢）")
        case_root = Path(tempfile.mkdtemp(dir=self.root))
        repos = initialize_memory_repositories(case_root / 'records.db', case_root / 'vectors', vector_size=3)
        try:
            writer = MemoryWriter(repos, _Embeddings())
            quotes = [(s['message_id'], s['quote']) for s in original_sources(packet)]
            if user_text in ('早上好。今天有点累，先歇会儿。另外项目日志一律先落盘再回传。',
                             '如果累了就先歇会儿。另外项目日志一律先落盘再回传。'):
                quotes = [(packet.turn_id, '另外项目日志一律先落盘再回传')]
            row = supported_row(content, quotes, subject='本例明确事项', status=status)
            if expected == 'proposed':
                row = {'index': 0, 'verdict': 'unsupported', 'reason': '本例原话没有作出候选所称的无条件决定'}
            provider = FixedReviewProvider([row])
            durable = []
            reviewed, _, result = asyncio.run(MemoryReviewProcessor(writer, provider).prepare(
                packet, candidates, [], persist=durable.append))
            if expected != 'proposed':
                self.assertEqual(provider.calls, 1)
            else:
                self.assertIn(provider.calls, (0, 1))  # Fabricated numbers can fail the hard evidence gate.
            saved = writer.save_semantic_candidates(self.scope, reviewed, packet=packet, turn_id=packet.turn_id)
            if expected == 'proposed':
                self.assertEqual(saved, [], result.as_dict())
                with repos.connect() as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM memory_records WHERE content_status='accepted'").fetchone()[0], 0)
                return 'proposed'  # No accepted decision exists in the actual database.
            self.assertEqual(len(saved), 1, result.as_dict())
            fresh = repos.records_by_ids([saved[0]['id']])[saved[0]['id']]
            self.assertEqual(fresh['content'], content)
            return fresh['content_status']
        finally:
            repos.close()


class AdoptionSubjectBindingTests(_ExtractorCase):
    """B3-R1 候选侧：采纳表达必须与候选事项对得上。"""

    def test_reviewed_conditional_content_keeps_the_condition_in_database(self):
        for source in ('如果预算够就改用方案B', '如果不累的话，就一律先落盘'):
            with self.subTest(source=source):
                self.assertEqual(self._candidate_status(source, source, status='proposed'), 'proposed')

    def test_unrelated_agreement_in_the_same_turn_cannot_stamp_another_item(self) -> None:
        """本轮「同意A」不能给 B 盖章 —— 这是 B3-R1 的核心用例。"""
        status = self._candidate_status(
            "同意以后回答先说结论；云端部署的建议你再分析一下",
            "用户已采纳云端部署方案",
            expected='proposed',
        )
        self.assertEqual(status, "proposed")

    def test_agreement_on_a_named_other_subject_cannot_stamp_this_item(self) -> None:
        """「同意」指向的是另一个具名事项时，也不能给本候选盖章。"""
        status = self._candidate_status("项目的排期同意下周开始", "用户已采纳云端部署方案", expected='proposed')
        self.assertEqual(status, "proposed")

    def test_subject_named_with_adoption_in_one_clause_is_kept(self) -> None:
        """同一句里既提到事项又有采纳表达时，必须保留 accepted。"""
        status = self._candidate_status("云端部署方案就按这个来吧", "采用云端部署方案")
        self.assertEqual(status, "accepted")

    def test_plain_adoption_of_the_same_subject_is_kept(self) -> None:
        status = self._candidate_status("同意采用本地部署方案", "采用本地部署方案")
        self.assertEqual(status, "accepted")

    def test_deictic_adoption_still_counts(self) -> None:
        """「就按这个做」指的是当前讨论的事，不能因为没重复事项名就被打回。"""
        status = self._candidate_status("统一记忆规则就按这个做，先别部署", "统一记忆规则")
        self.assertEqual(status, "accepted")

    def test_subject_then_deictic_confirmation_keeps_accepted(self) -> None:
        """先说了事项、紧接着用指代式确认（「那就按这个来吧」）要保留 accepted。

        这是 lead 点名要防的误伤：事项在上一句交代过，确认句本身只写「这个」，
        不能因为确认句没重复事项名就当没绑定。
        """
        status = self._candidate_status("先把云端部署方案说清楚；那就按这个来吧", "采用云端部署方案")
        self.assertEqual(status, "accepted")

    def test_unrelated_agreement_then_target_item_stays_proposed(self) -> None:
        """(a) 正反两条的另一半：「同意A；B你再分析一下」→ B 必须保持 proposed。"""
        status = self._candidate_status(
            "同意以后回答先说结论；云端部署的建议你再分析一下", "用户已采纳云端部署方案"
            , expected='proposed'
        )
        self.assertEqual(status, "proposed")

    def test_negated_deictic_adoption_is_not_adoption(self) -> None:
        """「先不要按这个来」不是采纳。"""
        status = self._candidate_status("先不要按这个来", "统一记忆规则", expected='proposed')
        self.assertEqual(status, "proposed")

    def test_unsettled_turn_still_blocks_adoption(self) -> None:
        """用户明说「还没定」时，任何采纳表达都不作数（既有规则不能被削弱）。"""
        status = self._candidate_status("先不要决定是否采用，也不要部署。", "采用统一记忆规则", expected='proposed')
        self.assertEqual(status, "proposed")

    def test_explicit_decision_without_the_word_agree_is_recognized(self) -> None:
        """显式决定（不带「同意/确认」字样）仍要认 —— 不能靠字面词兜底。"""
        for user_text, content in (
            ("这个项目就按云端部署来", "项目按云端部署执行"),
            ("方案定了，用云端", "云端部署方案已定"),
        ):
            with self.subTest(user_text=user_text):
                status = self._candidate_status(user_text, content)
                self.assertEqual(status, "accepted", f"{user_text!r} 应当认成已采纳")

    def test_rejected_status_is_not_rescued_by_subject_binding(self) -> None:
        """否决状态走既有证据链，不会被这次的绑定改动影响。"""
        status = self._candidate_status("云端部署不用了，取消", "云端部署方案", status="rejected")
        self.assertEqual(status, "rejected")

    def test_switch_to_wording_counts_as_accepting_the_new_value(self) -> None:
        """「改用/换成/改为 X」= 用户采纳了新值，不能降级成 proposed。

        写入侧的替代保护本来就认这类措辞；提炼侧若不认，正当替代会被降成
        proposed，写入侧再因为 proposed 拒绝归档，模型链路永远走不通。
        """
        for user_text, content in (
            ("构建产物保留策略改用只保留最近两次，不再按五次", "构建产物保留策略只保留最近两次"),
            ("预算换成三千", "预算为三千元"),
            ("排期改为下周二", "排期为下周二"),
        ):
            with self.subTest(user_text=user_text):
                self.assertEqual(self._candidate_status(user_text, content), "accepted")

    def test_switch_wording_for_another_subject_does_not_stamp_this_item(self) -> None:
        """换值措辞同样要绑事项：「排期改用周二」不能证明「预算改为三千」。"""
        status = self._candidate_status("排期改用周二", "预算改为三千", expected='proposed')
        self.assertEqual(status, "proposed")

    def test_plain_rejection_wording_is_not_acceptance(self) -> None:
        """「不要用」说旧的不作数，不等于采纳新值 —— 不能当 adopted 放行。"""
        status = self._candidate_status("不要用云端的方案", "采用云端部署方案", expected='proposed')
        self.assertEqual(status, "proposed")

    def test_direct_decision_wording_is_recognized(self) -> None:
        """用户直接为事项定下结论时，不需要再补一句「同意」。

        真实模型批次里 Q1「这个项目暂时先不部署」与 Q11「项目日志一律先落盘
        再回传」都被程序降成了 proposed —— DECISION_MARKERS 词表当时写好了却
        没有接进绑定路径，等于死代码。这里锁住「词表 + 绑定」两件事都在。
        """
        for user_text, content in (
            ("这个项目暂时先不部署，继续做本机验收。", "该项目暂时不部署，继续在本机做验收"),
            ("早上好。今天有点累，先歇会儿。另外项目日志一律先落盘再回传。", "项目日志的处理规则：一律先落盘保存，再回传。"),
            ("项目日志一律先落盘再回传，不得直接回传", "项目日志一律先落盘再回传，不得直接回传"),
            ("数据归档统一走冷库", "数据归档改为统一走冷库"),
        ):
            with self.subTest(user_text=user_text):
                self.assertEqual(
                    self._candidate_status(user_text, content),
                    "accepted",
                    f"{user_text!r} 是明确决定，不该退回 proposed",
                )

    def test_direct_decision_wording_for_another_subject_is_not_bound(self) -> None:
        """明确决定同样要绑事项：「排期暂不调整」证明不了「部署暂不实施」。"""
        status = self._candidate_status("排期暂不调整", "部署暂不实施", expected='proposed')
        self.assertEqual(status, "proposed")

    def test_a_decision_in_a_continuation_clause_is_bound(self) -> None:
        """「不是 A，是决定 B」：结论落在续写分句上时，仍算对这件事的决定。

        独立复核发现：模型把「改用 B」写成「决定使用 B」、而用户把新结论放在
        续写分句里（该分句不重复事项名）时，记录会被降级成 proposed ——
        用户明确改的决定看起来只是一条建议。续写分句跟在提到事项的那一句
        后面，指的是同一件事，应当认。
        """
        status = self._candidate_status(
            "回滚方案不是选择使用方案A，是决定使用方案B。", "回滚方案决定使用方案B"
        )
        self.assertEqual(status, "accepted")

    def test_a_continuation_clause_needs_the_subject_in_the_same_sentence(self) -> None:
        """续写要有前提：同一句里没有提到这件事时，别的句子不能给它盖章。"""
        status = self._candidate_status(
            "排期方案暂不调整；是决定使用方案B。", "回滚方案决定使用方案B"
            , expected='proposed'
        )
        self.assertEqual(status, "proposed")

    def test_asking_whether_to_decide_is_not_a_decision(self) -> None:
        """提问不是下结论 —— 分句会切掉句末问号，所以句首疑问词也要拦。"""
        for user_text, content in (
            ("是不是要一律先落盘？", "用户已决定日志一律先落盘"),
            ("要不要先不部署？", "项目暂不部署"),
        ):
            with self.subTest(user_text=user_text):
                self.assertEqual(
                    self._candidate_status(user_text, content, expected='proposed'),
                    "proposed",
                )

    def test_withholding_a_decision_is_not_a_decision(self) -> None:
        """「先不要决定是否采用」是**没有**决定，不能被「先不」误判成决定。

        这是既有守卫用例（用户明说还没定时任何采纳表达都不作数），加了直接
        决定词表之后必须仍然拦住。
        """
        status = self._candidate_status("先不要决定是否采用，也不要部署。", "采用统一记忆规则", expected='proposed')
        self.assertEqual(status, "proposed")


    def test_a_conditional_decision_is_not_a_decision(self) -> None:
        """带条件的设想不是决定：「如果不累的话，就一律先落盘」还没定下来。

        条件的作用域是**整句**：分句会按逗号切开，后半句自己不带条件词，
        按分句判就会漏过去，所以这里连逗号版本一起锁。
        """
        for user_text, content in (
            ("如果不累的话就一律先落盘", "日志一律先落盘"),
            ("如果不累的话，就一律先落盘", "日志一律先落盘"),
            ("如果预算够就改用方案B", "改用方案B"),
            ("如果预算够，就改用方案B", "改用方案B"),
            ("一旦定了就一律先落盘", "日志一律先落盘"),
            ("看情况吧，也许一律先落盘", "日志一律先落盘"),
            ("要是可以的话暂不部署", "项目暂不部署"),
        ):
            with self.subTest(user_text=user_text):
                self.assertEqual(
                    self._candidate_status(user_text, content, expected='proposed'),
                    "proposed",
                    f"{user_text!r} 只是设想，不该记成已决定",
                )

    def test_a_condition_does_not_affect_a_later_unrelated_decision(self) -> None:
        """条件只作用在自己那一句：后一句无关的明确决定仍要认。

        不能因为前面出现过「如果」，就把后面「另外日志一律先落盘」也否掉。
        """
        status = self._candidate_status(
            "如果累了就先歇会儿。另外项目日志一律先落盘再回传。", "项目日志先落盘再回传"
        )
        self.assertEqual(status, "accepted")


    def test_one_shared_ordinary_word_does_not_bind_two_subjects(self) -> None:
        """只共用一个普通词（如「方案」）不算同一件事。

        实测漏点：用户说「排期方案暂不调整」，模型输出「部署方案立即执行」，
        两件事只共用「方案」，却被记成已决定。要求至少两处共同实义片段。
        """
        status = self._candidate_status("排期方案暂不调整", "部署方案立即执行", expected='proposed')
        self.assertEqual(status, "proposed")

    def test_more_conditional_connectives_are_recognized(self) -> None:
        """条件连接词不止「如果」：若 / 只要 同样是设想，不是决定。"""
        for user_text, content in (
            ("若预算够就一律先落盘", "日志一律先落盘"),
            ("只要不忙就一律先落盘", "日志一律先落盘"),
            ("假使预算够就改用方案B", "改用方案B"),
        ):
            with self.subTest(user_text=user_text):
                self.assertEqual(self._candidate_status(user_text, content, expected='proposed'), "proposed")

    def test_the_suffix_dehua_in_a_plain_phrase_is_not_conditional(self) -> None:
        """「说的话」不是条件句，不能把明确决定也否掉。

        「的话」既可以是条件（「不累的话就…」），也可以只是「说的话」里的普通
        词。只有后面紧跟「就／便／才／再」才算条件。
        """
        status = self._candidate_status("就照我刚才说的话一律先落盘", "日志一律先落盘")
        self.assertEqual(status, "accepted")


class ShortTermLifecycleTests(_ExtractorCase):
    """B3-R7 生命周期侧：短期语义要真的进入现有到期政策。"""

    def _parse_one(self, content: str, *, user_text: str, **extra):
        packet = self._packet(user_text)
        item = {
            "content": content,
            "kind": "fact",
            "content_status": "reported",
            "source_types": ["user_statement"],
            "source_refs": [packet.turn_id],
        }
        item.update(extra)
        raw = json.dumps({"memories": [item]}, ensure_ascii=False)
        candidates, _suggestions, rejected = self.extractor.parse_maintenance(raw, packet=packet)
        return packet, candidates, rejected

    def test_declared_horizon_survives_without_any_keyword_in_the_text(self) -> None:
        """声明的 short_term 必须自己生效，不能靠正文里恰好带「今天」这类词。

        这条是对「只按关键词推断」的反证：正文刻意不含任何短期关键词。
        """
        text = "刚拿到快递所以心情轻松了一点"
        _packet, candidates, rejected = self._parse_one(
            text, user_text=text, horizon="short_term"
        )
        self.assertEqual(len(candidates), 1, f"不该被拒：{rejected}")
        self.assertEqual(candidates[0].as_metadata().get("horizon"), "short_term")

    def test_long_term_declaration_is_not_overridden_by_keywords(self) -> None:
        """长期偏好即使正文里有「今天」，也要按声明的 long_term 走，不得被判短期。"""
        text = "我的习惯是今天的事情今天做完，这是长期原则"
        _packet, candidates, rejected = self._parse_one(
            "长期原则：今天的事情今天做完", user_text=text, kind="preference",
            content_status="explicit", horizon="long_term",
        )
        self.assertEqual(len(candidates), 1, f"不该被拒：{rejected}")
        self.assertEqual(candidates[0].as_metadata().get("horizon"), "long_term")

    def test_unknown_horizon_is_refused_instead_of_silently_long_term(self) -> None:
        """非法 horizon 要拒收，不能静默当成长期留存。"""
        _packet, candidates, rejected = self._parse_one(
            "随便一句", user_text="随便一句", horizon="forever"
        )
        self.assertEqual(candidates, [])
        self.assertTrue(any("horizon" in item["reason"] for item in rejected), rejected)

    def test_short_term_item_is_expired_by_maintenance_after_a_year(self) -> None:
        repos = initialize_memory_repositories(
            self.root / "records.db", self.root / "vectors", vector_size=3
        )
        try:
            writer = MemoryWriter(repos, _Embeddings())
            packet = self._packet("今天因为等待验收有些烦躁")
            raw = json.dumps(
                {
                    "memories": [
                        {
                            "content": "今天因为等待验收有些烦躁",
                            "kind": "fact",
                            "content_status": "reported",
                            "source_types": ["user_statement"],
                            "source_refs": [packet.turn_id],
                            "horizon": "short_term",
                        }
                    ]
                },
                ensure_ascii=False,
            )
            candidates, _suggestions, rejected = self.extractor.parse_maintenance(
                raw, packet=packet
            )
            self.assertEqual(len(candidates), 1, f"短期情绪候选不该被拒：{rejected}")
            provider = FixedReviewProvider([supported_row(
                '今天因为等待验收有些烦躁', [('turn-1', '今天因为等待验收有些烦躁')],
                subject='当天情绪', mode='short_term', days=1, status='reported')])
            durable = []
            candidates, _, result = asyncio.run(MemoryReviewProcessor(writer, provider).prepare(
                packet, candidates, [], persist=durable.append))
            self.assertEqual(provider.calls, 1)
            self.assertTrue(candidates, result.as_dict())
            saved = writer.save_semantic_candidates(self.scope, candidates, packet=packet, turn_id=packet.turn_id)
            self.assertEqual(len(saved), 1)
            self.assertEqual(datetime.fromisoformat(saved[0]['expires_at']) - datetime.fromisoformat(saved[0]['created_at']), timedelta(days=1))
            record_id = saved[0]["id"]
            writer.run_maintenance(now=datetime.now(UTC) + timedelta(days=365))
            fresh = repos.records_by_ids([record_id]).get(record_id)
            self.assertTrue(
                fresh is None or fresh["status"] != "active",
                "短期（horizon=short_term）记录一年后不得仍是 active",
            )
        finally:
            repos.close()
            gc.collect()


if __name__ == "__main__":
    unittest.main()
