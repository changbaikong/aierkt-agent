"""过滤精排层 - 用 EMA 时间衰减 + 质量/长度/去重过滤"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
import numpy as np

LOGGER = logging.getLogger("agent_core.memory.filtering")


class FilteringRanker:
    """
    过滤精排层（RAG 第三层）
    任务：把召回+重排的候选结果过滤成最终要用的高质量记忆

    流程：
    1. 时间衰减（EMA）- 越旧的记忆权重越低
    2. 质量过滤 - 扔掉 RRF 分数太低的
    3. 长度过滤 - 扔掉太短/太长的
    4. 去重过滤 - 扔掉 embedding 相似度很高的重复记忆
    5. 最终精排 - 按衰减后的分数排序
    """

    def __init__(
        self,
        half_life_days: int = 180,
        min_score_threshold: float = 0.01,
        min_length: int = 2,
        max_length: int = 800,
        duplicate_threshold: float = 0.85,
    ):
        """
        参数说明：
        - half_life_days: 时间衰减半衰期（天数）- 默认 180 天（半年后权重降到 50%）
        - min_score_threshold: RRF 分数阈值（低于此值直接扔掉）。
          注意 RRF 分数本身很小：k=60 时单路命中约 0.011~0.016、双路约 0.033，
          所以默认 0.01 只杀「几乎无命中」的凑数匹配，不误伤正常召回。
        - min_length: 最短字符数（中文/英文统一按去空格后的字符数计；太短的是杂质碎片）
        - max_length: 最长字符数（太长的用户看不完）
        - duplicate_threshold: 去重阈值（余弦相似度 > 此值认为是重复）
        """
        self.half_life_days = half_life_days
        self.decay_rate = 0.5 ** (1 / half_life_days)  # 每天的衰减率
        self.min_score_threshold = min_score_threshold
        self.min_length = min_length
        self.max_length = max_length
        self.duplicate_threshold = duplicate_threshold

    def filter_and_rerank(self, candidates: list[dict[str, Any]], layer: str = "") -> list[dict[str, Any]]:
        """
        完整的过滤精排流程

        输入：召回+重排后的候选记忆列表
        输出：过滤精排后的高质量记忆列表

        Args:
            candidates: 候选记忆列表
            layer: 层级标识（用于日志，如 "warm", "cold"）
        """
        if not candidates:
            layer_hint = f"[{layer}] " if layer else ""
            LOGGER.debug("📥 过滤精排：%s输入为空，跳过", layer_hint)
            return []

        original_count = len(candidates)
        LOGGER.info("📥 过滤精排开始：输入 %d 条候选", original_count)

        # 1. 时间衰减（给每个候选加上 adjusted_score）
        for item in candidates:
            item["adjusted_score"] = self._apply_time_decay(item)

        # 2. 质量过滤
        candidates = self._quality_filter(candidates)
        after_quality = len(candidates)
        LOGGER.info("  🔍 质量过滤：%d → %d（过滤掉 %d 条低分）", original_count, after_quality, original_count - after_quality)

        # 3. 长度过滤
        candidates = self._length_filter(candidates)
        after_length = len(candidates)
        LOGGER.info("  📏 长度过滤：%d → %d（过滤掉 %d 条长度异常）", after_quality, after_length, after_quality - after_length)

        # 4. 去重过滤
        candidates = self._duplicate_filter(candidates)
        after_dedup = len(candidates)
        LOGGER.info("  🧹 去重过滤：%d → %d（过滤掉 %d 条重复）", after_length, after_dedup, after_length - after_dedup)

        # 5. 最终精排（按 adjusted_score 降序）
        candidates = sorted(candidates, key=lambda x: x["adjusted_score"], reverse=True)
        LOGGER.info("  🎯 最终精排：按时间衰减分数排序完成")

        LOGGER.info(
            "✅ 过滤精排完成：%d → %d（总过滤率 %.1f%%）",
            original_count, after_dedup, (1 - after_dedup / original_count) * 100 if original_count > 0 else 0,
        )

        return candidates

    def _apply_time_decay(self, item: dict[str, Any]) -> float:
        """
        EMA 风格的时间衰减

        公式：adjusted_score = base_score * (decay_rate ^ age_days)
        """
        base_score = float(item.get("score", 0.0))

        # 获取记忆的创建时间
        created_at = item.get("created_at")
        if not created_at:
            return base_score  # 没有时间戳就不衰减

        try:
            # 解析时间戳（支持 ISO 格式）
            if isinstance(created_at, str):
                # 移除末尾的时区信息（如果有）
                if created_at.endswith('Z'):
                    created_at = created_at[:-1]
                elif '+' in created_at:
                    created_at = created_at.split('+')[0]
                doc_time = datetime.fromisoformat(created_at)
            else:
                doc_time = created_at

            # 计算年龄（天数）
            age_days = (datetime.now() - doc_time).days

            # 指数衰减
            time_weight = self.decay_rate ** age_days

            return base_score * time_weight

        except (ValueError, TypeError, AttributeError):
            # 时间戳解析失败，返回原始分数
            return base_score

    def _quality_filter(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        质量过滤：保留 adjusted_score > 阈值的候选

        动态阈值：如果过滤后太少（< 3 个），自动放宽阈值
        """
        filtered = [item for item in candidates if item["adjusted_score"] > self.min_score_threshold]

        # 动态阈值：如果高质量候选太少，放宽到 0.15
        if len(filtered) < 3 and self.min_score_threshold > 0.15:
            before_relax = len(filtered)
            filtered = [item for item in candidates if item["adjusted_score"] > 0.15]
            LOGGER.debug("    ⚠️  质量阈值放宽到 0.15（原阈值 %.2f 只剩 %d 个 → 放宽后 %d 个）",
                        self.min_score_threshold, before_relax, len(filtered))

        return filtered

    def _length_filter(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        长度过滤：避免太短的杂质碎片和太长的长文

        中文/英文统一按「去空格后的字符数」计——英文短句（如记忆里的偏好事实）
        不该因为「单词数少」被误杀，字符数更贴近人读的体量。
        """
        filtered = []
        for item in candidates:
            content = str(item.get("content", ""))
            length = len(content.replace(" ", "").replace("\n", ""))

            if self.min_length <= length <= self.max_length:
                filtered.append(item)

        return filtered

    def _duplicate_filter(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        去重过滤：如果两个记忆的 embedding 相似度 > 阈值，只保留分数更高的

        注意：如果记忆没有 embedding，跳过去重检查
        """
        filtered = []
        duplicate_count = 0

        for item in candidates:
            # 获取当前记忆的 embedding
            embedding = item.get("embedding")
            if embedding is None:
                # 没有 embedding 的记忆直接保留（无法判断相似度）
                filtered.append(item)
                continue

            # 检查是否与已保留的记忆重复
            is_duplicate = False
            for prev_item in filtered:
                prev_embedding = prev_item.get("embedding")
                if prev_embedding is None:
                    continue

                # 计算余弦相似度
                sim = self._cosine_similarity(embedding, prev_embedding)

                if sim > self.duplicate_threshold:
                    # 发现重复！比较分数，保留分数更高的
                    duplicate_count += 1
                    if item["adjusted_score"] > prev_item["adjusted_score"]:
                        # 当前记忆分数更高，替换掉旧的
                        filtered.remove(prev_item)
                        filtered.append(item)
                        LOGGER.debug("    🔄 替换重复记忆：id=%s (分数 %.3f) 替换 id=%s (分数 %.3f)，相似度=%.3f",
                                    item.get("id"), item["adjusted_score"],
                                    prev_item.get("id"), prev_item["adjusted_score"], sim)
                    else:
                        LOGGER.debug("    🚫 丢弃重复记忆：id=%s (分数 %.3f) 与 id=%s 重复，相似度=%.3f",
                                    item.get("id"), item["adjusted_score"], prev_item.get("id"), sim)
                    # 否则就是旧记忆分数更高，当前记忆被丢弃
                    is_duplicate = True
                    break

            if not is_duplicate:
                filtered.append(item)

        if duplicate_count > 0:
            LOGGER.debug("    📊 去重统计：发现 %d 对重复记忆", duplicate_count)

        return filtered

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """计算两个向量的余弦相似度"""
        try:
            a_array = np.array(a)
            b_array = np.array(b)
            dot_product = np.dot(a_array, b_array)
            norm_a = np.linalg.norm(a_array)
            norm_b = np.linalg.norm(b_array)

            if norm_a == 0 or norm_b == 0:
                return 0.0

            return float(dot_product / (norm_a * norm_b))

        except (ValueError, TypeError):
            return 0.0
