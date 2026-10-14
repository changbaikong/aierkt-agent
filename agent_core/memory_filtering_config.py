"""
过滤精排层配置文件

根据不同场景调整参数：
- 新闻/实时信息：短半衰期（30 天）
- 技术文档：中等半衰期（90-180 天）
- 基础知识：长半衰期（365 天）
"""

from dataclasses import dataclass


@dataclass
class FilteringConfig:
    """过滤精排层配置"""

    # 时间衰减参数
    half_life_days: int = 180  # 半衰期（天数）

    # 质量过滤参数
    min_score_threshold: float = 0.01  # RRF 分数阈值（k=60 时正常命中约 0.011~0.033，低于此值近似无命中）

    # 长度过滤参数
    min_length: int = 2    # 最短字符数（中英文统一按去空格后的字符数计）
    max_length: int = 800   # 最长字符数

    # 去重过滤参数
    duplicate_threshold: float = 0.85  # 余弦相似度阈值（> 此值认为重复）


# 预设配置

# 1. 新闻/实时信息场景（快速过时）
NEWS_CONFIG = FilteringConfig(
    half_life_days=30,           # 1 个月后权重降到 50%
    min_score_threshold=0.012,   # RRF 双路命中左右（约 0.033 的一档以下）
    min_length=40,
    max_length=500,              # 新闻通常较短
    duplicate_threshold=0.90,    # 新闻重复度高，提高去重阈值
)

# 2. 技术文档场景（中等变化速度）
TECH_DOC_CONFIG = FilteringConfig(
    half_life_days=180,          # 半年后权重降到 50%
    min_score_threshold=0.01,    # RRF 单路命中下限
    min_length=2,
    max_length=800,
    duplicate_threshold=0.85,
)

# 3. 基础知识场景（几乎不变）
FUNDAMENTAL_CONFIG = FilteringConfig(
    half_life_days=365,          # 1 年后权重降到 50%
    min_score_threshold=0.008,   # 略放宽，保留更多基础事实
    min_length=2,
    max_length=1000,             # 基础知识可以更长
    duplicate_threshold=0.80,
)

# 4. 严格模式（只要最高质量的）
STRICT_CONFIG = FilteringConfig(
    half_life_days=90,
    min_score_threshold=0.016,   # 只保双路命中的高相关记忆
    min_length=80,               # 更长的最小长度
    max_length=600,
    duplicate_threshold=0.90,    # 更严格的去重
)

# 5. 宽松模式（尽量保留候选）
LOOSE_CONFIG = FilteringConfig(
    half_life_days=365,
    min_score_threshold=0.006,   # 接近全部放行
    min_length=2,                # 更短的最小长度
    max_length=1200,
    duplicate_threshold=0.75,    # 宽松的去重
)

# 默认配置（推荐用于编程知识 RAG）
DEFAULT_CONFIG = TECH_DOC_CONFIG


def get_config(preset: str = "default") -> FilteringConfig:
    """
    获取预设配置

    参数：
        preset: 预设名称
            - "default": 默认配置（技术文档场景）
            - "news": 新闻/实时信息
            - "tech": 技术文档
            - "fundamental": 基础知识
            - "strict": 严格模式
            - "loose": 宽松模式

    返回：
        FilteringConfig 实例
    """
    configs = {
        "default": DEFAULT_CONFIG,
        "news": NEWS_CONFIG,
        "tech": TECH_DOC_CONFIG,
        "fundamental": FUNDAMENTAL_CONFIG,
        "strict": STRICT_CONFIG,
        "loose": LOOSE_CONFIG,
    }

    config = configs.get(preset.lower())
    if config is None:
        raise ValueError(
            f"未知的预设: {preset}. "
            f"可用预设: {', '.join(configs.keys())}"
        )

    return config


# 使用示例：
#
# from agent_core.memory_filtering import FilteringRanker
# from agent_core.memory_filtering_config import get_config
#
# # 使用预设配置
# config = get_config("tech")
# ranker = FilteringRanker(
#     half_life_days=config.half_life_days,
#     min_score_threshold=config.min_score_threshold,
#     min_length=config.min_length,
#     max_length=config.max_length,
#     duplicate_threshold=config.duplicate_threshold,
# )
#
# # 或者自定义配置
# custom_config = FilteringConfig(
#     half_life_days=120,
#     min_score_threshold=0.3,
#     min_length=50,
#     max_length=700,
#     duplicate_threshold=0.88,
# )
