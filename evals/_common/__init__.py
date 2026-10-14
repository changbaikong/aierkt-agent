"""矩阵共享层：统一 Case/Turn、RecordingProvider、受控假工具、运行时工具、Scorer、Runner。

套 5/6/7（LLM evals）继承 MatrixRunner + 用 build_registry + pick_scorer；
套 1-4（pytest 单元）主要直接用 agent_core 的类 + tests/_common/fake_registry。
"""

from .case import Case, Turn, case_to_record
from .fake_tools import DEFAULT_TOOLS, build_registry
from .recording_provider import RecordingProvider
from .runner_base import MatrixRunner
from .runtime import fixed_runtime_time, new_basetemp, new_temp_sqlite
from .scorer import (
    CompositeScorer,
    HumanScorer,
    RedlineScorer,
    RegexScorer,
    Score,
    Scorer,
    StatusScorer,
    ToolCallScorer,
    pick_scorer,
)

__all__ = [
    "Case",
    "Turn",
    "case_to_record",
    "RecordingProvider",
    "DEFAULT_TOOLS",
    "build_registry",
    "fixed_runtime_time",
    "new_basetemp",
    "new_temp_sqlite",
    "MatrixRunner",
    "Scorer",
    "Score",
    "StatusScorer",
    "RegexScorer",
    "ToolCallScorer",
    "CompositeScorer",
    "RedlineScorer",
    "HumanScorer",
    "pick_scorer",
]
