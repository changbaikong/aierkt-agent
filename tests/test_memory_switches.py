from __future__ import annotations

import os
import unittest
from unittest import mock

from agent_core import memory_switches


class MemorySwitchTests(unittest.TestCase):
    """开关的默认值必须与改造前一致：不配环境变量时什么都不变。"""

    def test_defaults_keep_the_pre_migration_behaviour(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(memory_switches.extraction_enabled())
            self.assertTrue(memory_switches.maintenance_enabled())
            self.assertFalse(memory_switches.cloud_sync_enabled())
            # 新版语义记忆是**新增**链路，默认必须关着：关着就是改造前的行为。
            self.assertFalse(memory_switches.semantic_memory_enabled())

    def test_semantic_memory_is_opt_in_only(self) -> None:
        with mock.patch.dict(os.environ, {"MEMORY_SEMANTIC_ENABLED": "true"}, clear=True):
            self.assertTrue(memory_switches.semantic_memory_enabled())
        with mock.patch.dict(os.environ, {"MEMORY_SEMANTIC_ENABLED": "off"}, clear=True):
            self.assertFalse(memory_switches.semantic_memory_enabled())

    def test_extraction_and_recall_follow_the_master_switch(self) -> None:
        """总开关关着时，提炼和召回都关 —— 不留「写了却读不到」的中间态。"""
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(memory_switches.semantic_extraction_enabled())
            self.assertFalse(memory_switches.semantic_recall_enabled())
        with mock.patch.dict(os.environ, {"MEMORY_SEMANTIC_ENABLED": "true"}, clear=True):
            self.assertTrue(memory_switches.semantic_extraction_enabled())
            self.assertTrue(memory_switches.semantic_recall_enabled())

    def test_recall_can_be_disabled_while_extraction_stays_on(self) -> None:
        env = {"MEMORY_SEMANTIC_ENABLED": "true", "MEMORY_SEMANTIC_RECALL_ENABLED": "false"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertTrue(memory_switches.semantic_extraction_enabled())
            self.assertFalse(memory_switches.semantic_recall_enabled())

    def test_false_spellings_all_disable(self) -> None:
        for raw in ("0", "false", "FALSE", "no", "off"):
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {"MEMORY_EXTRACT_ENABLED": raw}, clear=True):
                self.assertFalse(memory_switches.extraction_enabled())

    def test_cloud_mode_switches_are_read_independently(self) -> None:
        env = {
            "MEMORY_EXTRACT_ENABLED": "false",
            "MEMORY_MAINTENANCE_ENABLED": "false",
            "MEMORY_CLOUD_SYNC_ENABLED": "true",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(memory_switches.extraction_enabled())
            self.assertFalse(memory_switches.maintenance_enabled())
            self.assertTrue(memory_switches.cloud_sync_enabled())

    def test_reading_is_not_cached_so_tests_and_reconfigures_take_effect(self) -> None:
        with mock.patch.dict(os.environ, {"MEMORY_EXTRACT_ENABLED": "true"}, clear=True):
            self.assertTrue(memory_switches.extraction_enabled())
            os.environ["MEMORY_EXTRACT_ENABLED"] = "false"
            self.assertFalse(memory_switches.extraction_enabled())

    def test_the_documented_enablement_configuration_resolves_as_intended(self) -> None:
        """按使用说明里那套「云端处理」配置启用时，开关必须解析成预期的样子。

        钉三件事：处理端只有一个；内容维护跟着开；本机虽然不处理，但**要能读**
        新版记录 —— 少最后一条，就会出现「云端写了、本机看不到」。
        """
        cloud_env = {
            "MEMORY_SEMANTIC_ENABLED": "true",
            "MEMORY_PROCESSING_ENDPOINT": "cloud",
            "MEMORY_CONTENT_MAINTENANCE_ENABLED": "true",
        }
        with mock.patch.dict(os.environ, cloud_env, clear=True):
            self.assertTrue(memory_switches.semantic_memory_enabled())
            self.assertTrue(memory_switches.cloud_processing_enabled())
            self.assertFalse(memory_switches.local_processing_enabled())
            self.assertTrue(memory_switches.content_maintenance_enabled())
            # 不显式设 recall 时跟随总开关 —— 本机读得到新版记录。
            self.assertTrue(memory_switches.semantic_recall_enabled())

        # 本机侧（同一个处理端，只负责读）
        local_env = dict(cloud_env, MEMORY_SEMANTIC_RECALL_ENABLED="true")
        with mock.patch.dict(os.environ, local_env, clear=True):
            self.assertTrue(memory_switches.semantic_recall_enabled())
            self.assertFalse(memory_switches.local_processing_enabled())
            self.assertTrue(memory_switches.cloud_processing_enabled())

    def test_a_bad_processing_endpoint_fails_loudly_instead_of_guessing(self) -> None:
        """处理端写错必须抛错：静默退回默认值是当初「双端同时处理」那个 bug 的形态。"""
        with mock.patch.dict(os.environ, {"MEMORY_PROCESSING_ENDPOINT": "cloudd"}, clear=True):
            with self.assertRaises(ValueError):
                memory_switches.processing_endpoint()


if __name__ == "__main__":
    unittest.main()
