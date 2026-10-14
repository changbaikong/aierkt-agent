"""
媒体模块构建器

创建和配置媒体模块（TTS/ASR）。
"""
import os
from pathlib import Path

from agent_core.contracts import ToolSpec
from agent_core.media_persistence import MediaTaskStore
from agent_core.media_service import MediaTaskService
from agent_core.modules import ModuleSpec
from agent_tools.media import (
    make_media_cancel_tool,
    make_media_get_artifact_tool,
    make_media_get_status_tool,
    make_media_submit_tts_tool,
)


def build_media_module(
    db_path: Path | None = None,
    artifact_dir: Path | None = None,
    tts_url: str | None = None,
    tts_max_workers: int | None = None,
    asr_url: str | None = None,
    asr_max_workers: int | None = None,
    artifact_expiry_hours: int | None = None,
) -> ModuleSpec:
    """
    构建媒体模块

    Args:
        db_path: 任务数据库路径（默认: ./data/media_tasks.db）
        artifact_dir: Artifact 存储目录（默认: ./data/media_artifacts）
        tts_url: TTS 服务 URL（默认: TTS_SERVICE_URL 环境变量或 http://127.0.0.1:8766）
        tts_max_workers: TTS 最大并发数（默认: TTS_MAX_WORKERS 环境变量或 2）
        asr_url: ASR 服务 URL（默认: ASR_SERVICE_URL 环境变量或 None）
        asr_max_workers: ASR 最大并发数（默认: ASR_MAX_WORKERS 环境变量或 4）
        artifact_expiry_hours: Artifact 过期小时数（默认: MEDIA_ARTIFACT_EXPIRY_HOURS 环境变量或 24）

    Returns:
        媒体模块规格
    """
    # 读取配置
    if db_path is None:
        db_path = Path(os.getenv("MEDIA_TASK_DB", "./data/media_tasks.db"))

    if artifact_dir is None:
        artifact_dir = Path(os.getenv("MEDIA_ARTIFACT_DIR", "./data/media_artifacts"))

    if tts_url is None:
        tts_url = os.getenv("TTS_SERVICE_URL", "http://127.0.0.1:8766")

    if tts_max_workers is None:
        tts_max_workers = int(os.getenv("TTS_MAX_WORKERS", "2"))

    if asr_url is None:
        asr_url = os.getenv("ASR_SERVICE_URL")

    if asr_max_workers is None:
        asr_max_workers = int(os.getenv("ASR_MAX_WORKERS", "4"))

    if artifact_expiry_hours is None:
        artifact_expiry_hours = int(os.getenv("MEDIA_ARTIFACT_EXPIRY_HOURS", "24"))

    # 创建服务实例
    store = MediaTaskStore(db_path)
    service = MediaTaskService(
        store=store,
        artifact_dir=artifact_dir,
        tts_url=tts_url,
        tts_max_workers=tts_max_workers,
        asr_url=asr_url,
        asr_max_workers=asr_max_workers,
        artifact_expiry_hours=artifact_expiry_hours,
    )

    def build_tools() -> list[ToolSpec]:
        """构建媒体工具集。

        ASR（submit_asr）暂时不暴露：ASR 逻辑尚未实现（media_service.py 的
        submit_asr 会直接 raise NotImplementedError），注册出去只会让模型
        调一个必失败的工具。实现 ASR 后把 make_media_submit_asr_tool 加回
        build_tools 和下面的 declared_tools 即可（声明清单测试会跟着改）。
        """
        return [
            make_media_submit_tts_tool(service),
            make_media_get_status_tool(service),
            make_media_cancel_tool(service),
            make_media_get_artifact_tool(service),
        ]

    spec = ModuleSpec(
        module_id="media",
        build_tools=build_tools,
        memory_policy="private_state",  # 媒体任务状态是模块私有的
        max_concurrency=1,
        description="语音合成",
        start=service.start,
        stop=service.stop,
        runtime=service,
        # 与 build_tools 保持一致：ASR 未实现，暂不声明 submit_asr。
        declared_tools=("submit_tts", "get_status", "cancel", "get_artifact"),
    )
    return spec
