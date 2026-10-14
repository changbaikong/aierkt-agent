"""可见范围授权规则测试（B 计划第三步）。

授权是纯函数：给一行记录和一个作用域，返回能不能读。所有边界都在这里钉死，
免得三个读取入口各判一套。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from agent_core.contracts import Scope
from agent_core.memory_scope import (
    DEFAULT_OWNER_CHANNELS,
    authorize,
    is_semantic,
    owner_channels,
    payload_scope_should,
    project_binding_from_env,
    record_scope_kind,
    sql_filter_new,
)


def _row(**overrides):
    base = dict(
        owner_id="local-user",
        channel="web",
        conversation_id="conv-1",
        module_id="companion",
        semantics_version=1,
        scope_kind="conversation",
        project_id=None,
        visibility="module",
        layer="warm",
        status="active",
    )
    base.update(overrides)
    return base


class ChannelRuleTests(unittest.TestCase):
    def test_default_channels_exclude_scenario_channels(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(owner_channels(), DEFAULT_OWNER_CHANNELS)
            self.assertNotIn("scenario", owner_channels())

    def test_channels_can_be_overridden_by_env(self) -> None:
        with mock.patch.dict(os.environ, {"MEMORY_OWNER_CHANNELS": "web, mobile"}, clear=True):
            self.assertEqual(owner_channels(), ("web", "mobile"))

    def test_project_binding_comes_from_env_only(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(project_binding_from_env(), "")
        with mock.patch.dict(os.environ, {"AGENT_MEMORY_PROJECT": "dm-agent"}, clear=True):
            self.assertEqual(project_binding_from_env(), "dm-agent")


class FormatSplitTests(unittest.TestCase):
    def test_legacy_rows_are_not_treated_as_new_scope(self) -> None:
        legacy = _row(semantics_version=0, scope_kind=None)
        self.assertFalse(is_semantic(legacy))
        self.assertEqual(record_scope_kind(legacy), "legacy")

    def test_new_rows_report_their_declared_scope(self) -> None:
        self.assertTrue(is_semantic(_row()))
        self.assertEqual(record_scope_kind(_row(scope_kind="project")), "project")
        # 新记录没写范围时按最严的会话范围算。
        self.assertEqual(record_scope_kind(_row(scope_kind="")), "conversation")

    def test_legacy_rows_never_go_through_the_new_authorizer(self) -> None:
        scope = Scope("local-user", "web", "conv-1")
        legacy = _row(semantics_version=0, scope_kind=None)
        self.assertFalse(authorize(legacy, scope))


class ConversationScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = Scope("local-user", "web", "conv-1")

    def test_same_conversation_is_allowed(self) -> None:
        self.assertTrue(authorize(_row(), self.scope))

    def test_other_conversation_channel_or_module_is_denied(self) -> None:
        self.assertFalse(authorize(_row(conversation_id="conv-2"), self.scope))
        self.assertFalse(authorize(_row(channel="cli"), self.scope))
        self.assertFalse(authorize(_row(module_id="work"), self.scope))

    def test_other_account_is_denied(self) -> None:
        self.assertFalse(authorize(_row(owner_id="someone-else"), self.scope))


class ProjectScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = Scope("local-user", "web", "conv-1")

    def test_same_project_reads_across_conversations(self) -> None:
        row = _row(scope_kind="project", project_id="dm-agent", conversation_id="conv-9")
        self.assertTrue(authorize(row, self.scope, project_id="dm-agent"))

    def test_without_a_trusted_binding_a_same_named_project_is_not_authorized(self) -> None:
        """项目名相同不算授权：绑定只能来自运行配置或用户明确动作。"""
        row = _row(scope_kind="project", project_id="dm-agent")
        self.assertFalse(authorize(row, self.scope, project_id=""))
        self.assertFalse(authorize(row, self.scope, project_id="another-project"))

    def test_project_records_ignore_conversation_and_channel(self) -> None:
        row = _row(scope_kind="project", project_id="dm-agent", channel="cli", module_id="work")
        self.assertTrue(authorize(row, self.scope, project_id="dm-agent"))


class OwnerScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = Scope("local-user", "web", "conv-1")

    def test_owner_preference_is_readable_in_main_chat_channels(self) -> None:
        self.assertTrue(authorize(_row(scope_kind="owner"), self.scope))
        self.assertTrue(authorize(_row(scope_kind="owner", channel="cli"), self.scope))

    def test_owner_records_do_not_open_up_to_scenario_channels(self) -> None:
        scenario = Scope("local-user", "scenario", "scene-1")
        self.assertFalse(authorize(_row(scope_kind="owner"), scenario))
        # 记录写在场景渠道里时，主聊天也不读它。
        self.assertFalse(authorize(_row(scope_kind="owner", channel="scenario"), self.scope))

    def test_other_account_is_denied_even_with_the_right_channel(self) -> None:
        self.assertFalse(authorize(_row(scope_kind="owner", owner_id="someone-else"), self.scope))


class CandidateFilterTests(unittest.TestCase):
    def test_sql_filter_covers_all_three_scopes(self) -> None:
        scope = Scope("local-user", "web", "conv-1")
        fragment, args = sql_filter_new(scope, project_id="dm-agent")
        self.assertIn("scope_kind='conversation'", fragment)
        self.assertIn("scope_kind='project'", fragment)
        self.assertIn("scope_kind='owner'", fragment)
        self.assertIn("conv-1", args)
        self.assertIn("dm-agent", args)

    def test_sql_filter_drops_project_branch_without_binding(self) -> None:
        fragment, _ = sql_filter_new(Scope("local-user", "web", "conv-1"), project_id="")
        self.assertNotIn("scope_kind='project'", fragment)
        # 没有绑定时连 owner 分支也在（owner 与项目无关），但绝不放宽会话分支。
        self.assertIn("scope_kind='conversation'", fragment)

    def test_payload_should_carries_owner_and_channel_constraints(self) -> None:
        scope = Scope("local-user", "web", "conv-1")
        should = payload_scope_should(
            scope,
            project_id="dm-agent",
            match=lambda key, value: ("match", key, value),
            match_any=lambda key, values: ("match_any", key, tuple(values)),
            must=lambda items: ("must", tuple(items)),
        )
        kinds = [clause[1] if clause[0] == "match" else clause[0] for clause in should]
        self.assertIn("must", kinds)
        # 旧格式账号级事实那一条：必须同时带 visibility=shared 和渠道约束。
        shared_clause = [
            c for c in should
            if c[0] == "must" and ("match", "visibility", "shared") in c[1]
        ]
        self.assertEqual(len(shared_clause), 1)
        self.assertIn(("match", "channel", "web"), shared_clause[0][1])
        owner_clause = [c for c in should if c[0] == "must" and c[1][0][0] == "match" and c[1][0][1] == "scope_kind" and c[1][0][2] == "owner"]
        self.assertEqual(len(owner_clause), 1)
        self.assertIn(("match_any", "channel", ("web", "cli", "desktop")), owner_clause[0][1])


if __name__ == "__main__":
    unittest.main()
