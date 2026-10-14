"""
HTTP ingress for the web chat client.

Web 渠道入口，负责处理来自 Web 前端的请求。

已改进：
- 添加 CORS 支持
- 添加输入验证
- 添加详细的健康检查端点
- 添加日志记录
"""
from __future__ import annotations

import os
import re
import logging
import asyncio
import base64
import json
import sqlite3
import time
import traceback
import uuid
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from dotenv import load_dotenv

from agent_core import AgentLoop, AgentRequest, ConversationManager, MemoryAgentSession, ModuleRegistry, PromptComposer, Scope
from agent_core.capability_runtime import RuntimeBootstrapError, build_runtime, resolve_work_roots
from agent_core.search_gate import build_search_gate
from agent_core.events import Event, EventType, InMemoryEventBus
from agent_core.presentation_policy import expression_for_event
from agent_core.presentation import background_summary, project_presentation
from agent_core.opening_greeting import (
    ABSENCE_TIERS,
    DEFAULT_ABSENCE_TIER,
    DEFAULT_GREETING,
    OpeningGreetingCache,
    OpeningGreetingService,
)
from agent_core.opening_pool import prepared_greeting
from agent_core.opening_recall import OpeningRecallStore
from agent_core.memory_postprocess import extract_and_store_memory_turn
from agent_core.memory_repositories import MemoryRepositories, initialize_memory_repositories
from agent_core.memory_retrieval import MemoryRetriever
from agent_core.memory_switches import (
    cloud_sync_enabled,
    content_maintenance_enabled,
    extraction_enabled,
    local_processing_enabled,
    main_chat_practical_enabled,
    maintenance_enabled,
    semantic_memory_enabled,
)
from agent_core.memory_sync import MemorySyncRunner, build_runner_from_env
from agent_core.memory_sync_store import MemorySyncStore
from agent_core.memory_writes import EmbeddingRetryWorker, MemoryMaintenanceWorker, MemoryWriter
from agent_core.memory_extract import build_extractor_from_env, build_semantic_extractor_from_env
from agent_core.memory_model_agent import MemoryModelAgent
from agent_core.memory_maintenance import MemoryMaintenanceScheduler, MemoryMaintenanceService
from agent_core.memory_queue import (
    MemoryCandidateQueue,
    MemoryCandidateWorker,
    MemoryMaintenanceWorker,
)
from agent_core.memory_clarification import (
    PROPOSE_TOOL_NAME,
    ClarificationStore,
    ClarificationTurn,
    build_clarification_context,
    build_propose_tool_spec,
)
from agent_core.memory_turn import MemoryTurnPacket
from agent_core.memory_events import event_from_task_result, record_event
from agent_core.memory_turn import MemoryTurnPacket, turn_from_messages
from agent_core.personas import ARCUEID_PERSONA as ARcueid_PERSONA
from agent_core.intent import SQLiteIntentStateStore
from agent_core.persistence import SQLiteConfirmationStore, SQLiteRouteStore, SQLiteRunStore
from agent_core.routing import TaskRouter
from agent_core.conversation_repository import ADVANCE_BLOCKED, ConversationRepository
from agent_core.scenario_api import make_scenario_router
from agent_core.scenario_runtime import ScenarioRuntime, ScenarioStore
from agent_core.approval_coordinator import (
    APPROVAL_KIND_CHAT,
    DECISION_CONFIRMED,
    DECISION_REJECTED,
    ApprovalCoordinator,
)
from agent_core.contracts import AccountTaskScope
from agent_core.work_host import WorkHost
from agent_core.work_bridge import WorkResultBridge
from agent_core.work_progress_bridge import build_work_progress_bridge
from agent_core.task_result_bridge import (
    TaskResultBridge,
    public_error_code,
    public_task_result,
)
from agent_core.steward import (
    STEWARD_PROGRESS_KIND,
    StewardContext,
    StewardNarrator,
    StewardReporter,
)
from agent_core.local_worker_provider import LocalWorkerProvider
from agent_core.work_module import (
    attach_code_agent_memory_retriever,
    resolve_work_provider,
)
from agent_core.permission_mode import (
    MODE_FULL_ACCESS,
    PERMISSION_MODES,
    PermissionLayer,
    is_loopback_host,
)
from agent_core.chat_attachments import (
    AttachmentUnavailable,
    ChatAttachmentStore,
    InvalidImageError,
)
from agent_core.validation import (
    validate_field_length,
    validate_safe_id,
    validate_message_size,
    MAX_OWNER_ID_LENGTH,
    MAX_CONVERSATION_ID_LENGTH,
    ValidationError as InputValidationError
)
from agent_core.metrics import metrics, track_request
from agent_core.project_env import PROJECT_ENV_PATH
from agent_core.prompts import build_relative_time_context
from agent_core_adapters.bigmodel_embeddings import BigModelEmbeddingProvider
from agent_core_adapters.openai_compatible import OpenAICompatibleProvider
from agent_tools.location import LocationStore

logger = logging.getLogger(__name__)

# 开场白的降级日志（"模型调用超时""输出不合格""素材检索失败"…）都是 INFO 级，
# 而 root logger 默认是 WARNING —— 于是这些行在真实服务里一条都看不到。
# 结果是：用户看到"开场白永远是那一句"，而日志里干干净净，排查只能靠耗时反推
# （2026-09-19 实测，代价是两轮完整的排查）。
# 只给这一个模块定向放开，不动全局级别：兜底句是用户可感知的降级，值得留痕，
# 但不至于让整个服务的 INFO 日志淹掉终端。
logging.getLogger("agent_core.opening_greeting").setLevel(logging.INFO)




class ChatRequest(BaseModel):
    """
    聊天请求模型

    已添加输入验证：
    - message: 可选，最大 100KB；必须与 attachment_id 至少存在一个
    - account_id: 长度限制，只允许安全字符
    - conversation_id: 长度限制，只允许安全字符
    """
    message: str = Field(default="", max_length=100_000)
    attachment_id: str | None = Field(default=None, max_length=96)
    account_id: str = Field(default="local-user", min_length=1)
    conversation_id: str = Field(default="web", min_length=1)

    @field_validator('message')
    @classmethod
    def validate_message_field(cls, v: str) -> str:
        """验证消息内容"""
        try:
            validate_message_size(v)
        except InputValidationError as e:
            raise ValueError(e.message)
        return v

    @field_validator('account_id')
    @classmethod
    def validate_account_id_field(cls, v: str) -> str:
        """验证账户ID"""
        try:
            validate_field_length(v, MAX_OWNER_ID_LENGTH, "account_id")
            validate_safe_id(v, "account_id")
        except InputValidationError as e:
            raise ValueError(e.message)
        return v

    @field_validator('conversation_id')
    @classmethod
    def validate_conversation_id_field(cls, v: str) -> str:
        """验证对话ID"""
        try:
            validate_field_length(v, MAX_CONVERSATION_ID_LENGTH, "conversation_id")
            validate_safe_id(v, "conversation_id")
        except InputValidationError as e:
            raise ValueError(e.message)
        return v

    @field_validator('attachment_id')
    @classmethod
    def validate_attachment_id_field(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            validate_safe_id(v, "attachment_id")
        except InputValidationError as e:
            raise ValueError(e.message)
        return v


class DecisionRequest(BaseModel):
    """决策请求模型"""
    account_id: str = Field(default="local-user", min_length=1)
    conversation_id: str = Field(default="web", min_length=1)
    action_id: str = Field(min_length=1, max_length=128)
    decision: str = Field(pattern="^(confirmed|rejected)$")

    @field_validator('account_id')
    @classmethod
    def validate_account_id_field(cls, v: str) -> str:
        """验证账户ID"""
        try:
            validate_field_length(v, MAX_OWNER_ID_LENGTH, "account_id")
            validate_safe_id(v, "account_id")
        except InputValidationError as e:
            raise ValueError(e.message)
        return v

    @field_validator('conversation_id')
    @classmethod
    def validate_conversation_id_field(cls, v: str) -> str:
        """验证对话ID"""
        try:
            validate_field_length(v, MAX_CONVERSATION_ID_LENGTH, "conversation_id")
            validate_safe_id(v, "conversation_id")
        except InputValidationError as e:
            raise ValueError(e.message)
        return v

    @field_validator('action_id')
    @classmethod
    def validate_action_id_field(cls, v: str) -> str:
        try:
            validate_safe_id(v, "action_id")
        except InputValidationError as e:
            raise ValueError(e.message)
        return v


class WorkTaskRequest(BaseModel):
    task_type: str
    objective: str = Field(min_length=1, max_length=4000)
    input: dict[str, Any] = Field(default_factory=dict)
    account_id: str = Field(default="local-user", min_length=1)
    conversation_id: str = Field(default="web", min_length=1)


class WorkApprovalRequest(BaseModel):
    decision: str = Field(pattern="^(approved|rejected|expired)$")


class PermissionUpdateRequest(BaseModel):
    """账号级权限模式的写入体。

    ``mode`` 用 pattern 而不是枚举：值域由 ``PERMISSION_MODES`` 单一持有，
    在这里再抄一份就成了第二个真相来源，加一档时必然漏改。
    """
    account_id: str = Field(default="local-user", min_length=1)
    mode: str = Field(pattern="^(standard|full_access)$")

    @field_validator('account_id')
    @classmethod
    def validate_permission_account_id(cls, v: str) -> str:
        try:
            validate_field_length(v, MAX_OWNER_ID_LENGTH, "account_id")
            validate_safe_id(v, "account_id")
        except InputValidationError as e:
            raise ValueError(e.message)
        return v


class WorkPlanRequest(BaseModel):
    """A candidate plan. The Host validates and owns execution, not this model."""

    objective: str = Field(min_length=1, max_length=4000)
    steps: list[dict[str, Any]] = Field(min_length=1)
    account_id: str = Field(default="local-user", min_length=1)
    conversation_id: str = Field(default="web", min_length=1)


class PresentationAdvanceRequest(BaseModel):
    """确认一条助手消息已经在舞台上播放完（第二次点击）。

    ``message_id`` 由服务端在 presentation 快照里给出，客户端只负责回传 ——
    客户端不能自己编一条消息的身份，也不能凭"我看到的内容"来确认。
    """

    account_id: str = Field(default="local-user", min_length=1)
    message_id: int = Field(gt=0)

    @field_validator('account_id')
    @classmethod
    def validate_account_id_field(cls, v: str) -> str:
        try:
            validate_field_length(v, MAX_OWNER_ID_LENGTH, "account_id")
            validate_safe_id(v, "account_id")
        except InputValidationError as e:
            raise ValueError(e.message)
        return v


class ConversationCreateRequest(BaseModel):
    """Create one durable Web conversation."""
    account_id: str = Field(default="local-user", min_length=1)
    title: str = Field(default="", max_length=200)

    @field_validator('account_id')
    @classmethod
    def validate_account_id_field(cls, v: str) -> str:
        try:
            validate_field_length(v, MAX_OWNER_ID_LENGTH, "account_id")
            validate_safe_id(v, "account_id")
        except InputValidationError as e:
            raise ValueError(e.message)
        return v


#: ``visit_id`` 的合法形状。"这一次进入"的身份只由前端生成，字符集收窄到
#: URL 安全集合，免得它有机会变成注入到别处的载荷。
_VISIT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class OpeningGreetingRequest(BaseModel):
    """进入会话时请求一次开场白。

    ``visit_id`` 由前端为**每一次进入**生成 —— 而"一次进入"的界限是
    **标签页会话**（2026-09-18 定）：同一标签页里刷新 / 切设置页再切回 /
    切会话再切回，都复用同一个 visit_id（于是不重演问候、不再花一次模型调用），
    只有叉掉标签页后重新打开才是新一次进入。它既是幂等键（React 重挂载、
    网络重试不会重复花 Token），也是防迟到响应的键（对不上就直接丢弃）。

    ``absence_tier`` **已停用**（2026-09-18）：字段与枚举校验保留，只为让老客户端
    继续可用；它不再参与任何判断与措辞 —— 那句话的措辞由模型按素材自己决定。
    新客户端已经不发送这个字段。
    """

    account_id: str = Field(default="local-user", min_length=1)
    visit_id: str = Field(min_length=8, max_length=64)
    absence_tier: str = Field(default=DEFAULT_ABSENCE_TIER)

    @field_validator('account_id')
    @classmethod
    def validate_account_id_field(cls, v: str) -> str:
        try:
            validate_field_length(v, MAX_OWNER_ID_LENGTH, "account_id")
            validate_safe_id(v, "account_id")
        except InputValidationError as e:
            raise ValueError(e.message)
        return v

    @field_validator('visit_id')
    @classmethod
    def validate_visit_id_field(cls, v: str) -> str:
        if not _VISIT_ID_PATTERN.match(v or ""):
            raise ValueError("visit_id 只能是 8-64 位的字母、数字、连字符或下划线")
        return v

    @field_validator('absence_tier')
    @classmethod
    def validate_absence_tier_field(cls, v: str) -> str:
        # 档位已停用（2026-09-18），但校验留着：字段仍在契约里，收下一个认不出的
        # 值仍然应该报错，而不是静默按默认值处理 —— 老客户端传错时早点看到 422，
        # 比"它明明传了却不生效"好排查。
        if v not in ABSENCE_TIERS:
            raise ValueError(f"absence_tier 必须是 {' / '.join(ABSENCE_TIERS)} 之一")
        return v


class SettingsUpdateRequest(BaseModel):
    """设置更新请求模型"""
    settings: dict[str, str]  # key: env变量名, value: 新值

    @field_validator('settings')
    @classmethod
    def validate_settings_keys(cls, v: dict) -> dict:
        """只允许修改白名单内的配置项"""
        allowed_keys = {
            "GEMINI_API_KEY", "GOOGLE_GEMINI_BASE_URL", "GEMINI_MODEL",
            "ZHIPUAI_API_KEY", "ZHIPUAI_EMBEDDING_MODEL",
            "ZHIPUAI_EMBEDDING_BASE_URL", "ZHIPUAI_EMBEDDING_DIMENSIONS",
            "GPT_SOVITS_URL", "OPENAI_API_KEY", "OPENAI_API_BASE",
            "OPENAI_API_MODEL", "TTS_REF_AUDIO", "TTS_REF_TEXT",
            "BOCHA_API_KEY", "BOCHA_BASE_URL", "AMAP_WEB_SERVICE_KEY",
        }
        for key in v.keys():
            if key not in allowed_keys:
                raise ValueError(f"Invalid config key: {key}")
        return v


class LocationRequest(BaseModel):
    latitude: float
    longitude: float
    accuracy_m: float | None = None
    captured_at: datetime
    account_id: str = Field(default="local-user", min_length=1)
    conversation_id: str = Field(default="web", min_length=1)

    @field_validator("account_id")
    @classmethod
    def validate_location_account_id(cls, value: str) -> str:
        try:
            validate_field_length(value, MAX_OWNER_ID_LENGTH, "account_id")
            validate_safe_id(value, "account_id")
        except InputValidationError as exc:
            raise ValueError(exc.message) from exc
        return value

    @field_validator("conversation_id")
    @classmethod
    def validate_location_conversation_id(cls, value: str) -> str:
        try:
            validate_field_length(value, MAX_CONVERSATION_ID_LENGTH, "conversation_id")
            validate_safe_id(value, "conversation_id")
        except InputValidationError as exc:
            raise ValueError(exc.message) from exc
        return value


manager: Any = None
worker_provider: Any = None
work_host: WorkHost | None = None
# work→chat 回传桥的后台任务（lifespan 启动 / 关停时取消）
_work_bridge_task: asyncio.Task[None] | None = None
# 代码任务过程桥的后台任务：与结果桥各自订阅 Host 事件流（events() 每次调用
# 返回独立队列），互不抢事件。
_work_progress_bridge_task: asyncio.Task[None] | None = None
confirmation_store: Any = None
conversation_repository: ConversationRepository | None = None
# 会话语义状态（在聊什么、正等哪个补充信息）。必须是全局而不是 _initialize_runtime
# 里的局部变量：删会话的接口和定时清理都要能把它一起清掉，否则消息没了、
# "正在追问天气"这条线索还留着，新会话会继承旧会话的追问。
intent_state_store: SQLiteIntentStateStore | None = None
# 管家式过程表达：主 Chat 在关键节点说一两句人话（见 agent_core/steward.py）。
# None 时整条链路退化成"什么都不说"，不影响任何任务执行。
steward_reporter: StewardReporter | None = None
steward_narrator: StewardNarrator | None = None
_repositories: Any = None
_memory_writer: MemoryWriter | None = None
_memory_retriever: MemoryRetriever | None = None
_memory_extractor: Any = None
_memory_maintenance_worker: MemoryMaintenanceWorker | None = None
_memory_embedding_worker: EmbeddingRetryWorker | None = None
#: 云端记忆同步：只在 MEMORY_CLOUD_SYNC_ENABLED 打开且配了口令时才有值。
_memory_sync_store: MemorySyncStore | None = None
_memory_sync_runner: MemorySyncRunner | None = None
_memory_sync_task: asyncio.Task[Any] | None = None
#: 新版语义记忆：只在 MEMORY_SEMANTIC_ENABLED 打开时才有值。
#: 队列落库、提炼异步 —— 正文发布不等待它，重启也丢不了作业。
_memory_candidate_queue: MemoryCandidateQueue | None = None
_memory_candidate_worker: MemoryCandidateWorker | None = None
_memory_candidate_task: asyncio.Task[Any] | None = None
#: 实施工作 B：当前会话的疑点澄清。只在 MAIN_CHAT_PRACTICAL_ENABLED 打开且
#: 候选队列已装配时才有值 —— 它复用同一张作业表，不新增库、不新增索引。
_memory_clarification_store: Any = None
#: 每轮登记的提问草稿（回合号 → 草稿）。只存活一轮，不是持久状态。
_clarification_drafts: dict[str, Any] = {}
#: 内容维护：把「对已有记录的更新建议」应用到库里，并定期把到点记录排入复核。
#: 与候选共用一张作业表、不同类别；只在本地被指定为处理端时装配。
_memory_maintenance_service: MemoryMaintenanceService | None = None
_memory_maintenance_job_worker: MemoryMaintenanceWorker | None = None
_memory_review_scheduler: MemoryMaintenanceScheduler | None = None
_memory_maintenance_task: asyncio.Task[Any] | None = None
_memory_review_task: asyncio.Task[Any] | None = None
_task_services: list[Any] = []
_module_registry: ModuleRegistry | None = None
_tool_registry: Any = None
_opening_gateway: Any = None
_tool_registry_owner: Any = None
_registration_failures: dict[str, str] = {}
_capability_snapshot_cache: tuple[tuple[Any, ...], float, dict[str, Any]] | None = None
_capability_snapshot_lock: asyncio.Lock | None = None
_capability_refresh_task: asyncio.Task[dict[str, Any]] | None = None
_runtime_initialization_error = ""
_task_services_started = False
#: 定时任务终态回执桥。任务的完成/失败/取消在对话里说什么，只有它一个出口。
_task_result_bridge: TaskResultBridge | None = None
_memory_workers_started = False
_memory_postprocess_tasks: set[asyncio.Task[Any]] = set()
event_bus: InMemoryEventBus | None = None
_location_store: LocationStore = LocationStore()
_location_last_updates: dict[tuple[str, str, str], float] = {}
_conversation_maintenance_task: asyncio.Task[Any] | None = None
_chat_attachment_store: ChatAttachmentStore | None = None
#: 开场白（见 agent_core/opening_greeting.py）。与舞台投影刻意分成两条链路：
#: presentation 会被轮询、被 SSE 反复拉动，模型调用放进去等于每次刷新都花 Token。
_opening_greeting_service: OpeningGreetingService | None = None
#: 同一 visit_id 的幂等缓存 + 并发合并。进程内即可，不需要跨进程共享 ——
#: 多进程部署时各自多花一次 Token，代价远小于为它引入一个共享存储。
_opening_greeting_cache: OpeningGreetingCache | None = None
#: 联想使用记录（"最近提过什么"的台账）。建在 agent_core.db，与对话同库，
#: 但它既不是长期记忆、也不写聊天账本 —— 只是防止同一条旧事被反复提起的索引。
_opening_recall_store: OpeningRecallStore | None = None
#: 账号级权限层（store / controller / policy / cleanup 四件套）。
#: 建在 `_initialize_runtime` 里、注册模块之前 —— work 模块构造 WorkHost 时需要它。
permission_layer: PermissionLayer | None = None


def _chat_attachments() -> ChatAttachmentStore:
    global _chat_attachment_store
    if _chat_attachment_store is None:
        database_path = Path(os.getenv("AGENT_CORE_DB", "agent_core.db"))
        storage_path = Path(os.getenv(
            "CHAT_ATTACHMENT_DIR",
            str(Path(tempfile.gettempdir()) / "agent-chat-attachments"),
        ))
        _chat_attachment_store = ChatAttachmentStore(
            database_path,
            storage_path,
            max_bytes=int(os.getenv("CHAT_ATTACHMENT_MAX_BYTES", str(10 * 1024 * 1024))),
            ttl_seconds=int(os.getenv("CHAT_ATTACHMENT_TTL_SECONDS", str(15 * 60))),
        )
    _chat_attachment_store.cleanup_expired()
    return _chat_attachment_store


def _conversation_settings() -> dict[str, int]:
    """Read bounded repository settings once during runtime creation."""
    def configured(name: str, default: int, minimum: int = 1) -> int:
        try:
            return max(minimum, int(os.getenv(name, str(default))))
        except ValueError:
            logger.warning("Ignoring invalid %s value", name)
            return default

    return {
        "history_max_messages": configured("CONVERSATION_HISTORY_MAX_MESSAGES", 1000),
        "auto_archive_days": configured("CONVERSATION_AUTO_ARCHIVE_DAYS", 30),
        "archive_retention_days": configured("CONVERSATION_ARCHIVE_RETENTION_DAYS", 90),
    }


def _conversation_cleanup_interval() -> float:
    try:
        return max(1.0, float(os.getenv("CONVERSATION_CLEANUP_INTERVAL_SECONDS", "60")))
    except ValueError:
        logger.warning("Ignoring invalid CONVERSATION_CLEANUP_INTERVAL_SECONDS value")
        return 60.0


def build_web_scope(account_id: str = "local-user", conversation_id: str = "web") -> Scope:
    return Scope(owner_id=account_id.strip(), channel="web", conversation_id=conversation_id.strip(), module_id="companion")


def _steward_sink(scope: Scope, message: dict[str, Any], stable_key: str) -> bool:
    """把一句管家过程话术写进对话历史。

    走 ConversationManager 而不是直接写仓库：这样**当前进程里的对话历史**
    也立刻多出这一条，用户接着说"继续"时模型能看到进度。
    ``touch_activity=False`` —— 系统自己说的话不算用户活动，不该把
    一个没人说话的对话顶到会话列表最前面。
    """
    if manager is None:
        return False
    content = str(message.get("content") or "").strip()
    if not content:
        return False
    extra = {
        "kind": STEWARD_PROGRESS_KIND,
        "phase": message.get("phase", ""),
        "run_id": message.get("run_id", ""),
        "plan_id": message.get("plan_id", ""),
        "step_id": message.get("step_id", ""),
    }
    return bool(
        manager.append_message(
            scope,
            "assistant",
            content,
            stable_key=stable_key,
            touch_activity=False,
            **{key: value for key, value in extra.items() if value},
        )
    )


def _web_max_steps() -> int:
    try:
        configured = int(os.getenv("AGENT_WEB_MAX_STEPS", "5"))
    except ValueError:
        configured = 5
    return min(12, max(1, configured))


def _initialize_runtime() -> None:
    global manager, worker_provider, work_host, confirmation_store, conversation_repository, intent_state_store, _repositories, _task_services, event_bus, _location_store, _module_registry, _tool_registry, _tool_registry_owner, _registration_failures, _opening_gateway
    global _memory_writer, _memory_retriever, _memory_extractor, _memory_maintenance_worker, _memory_embedding_worker
    global _memory_sync_store
    global _memory_candidate_queue, _memory_candidate_worker
    global _memory_clarification_store
    global _memory_maintenance_service, _memory_maintenance_job_worker, _memory_review_scheduler
    global steward_reporter, steward_narrator
    global permission_layer
    if manager is not None:
        return
    provider = OpenAICompatibleProvider.from_env()
    worker_provider = provider
    # Work 允许触达的目录。**与 TaskRouter / Worker 同源**（都来自
    # AGENT_WORK_ROOTS），这样"路由说合法"与"Worker 会执行"才是同一个判据。
    # 默认值的解释只有一处（capability_runtime.resolve_work_roots），
    # 这里不再重复写 `str(Path.cwd())` —— 写两遍就会有"改了一边忘了另一边"。
    roots = resolve_work_roots()
    # 这里**不再**自建 WorkHost：work 模块（build_work_module）会建一个并随模块生命周期
    # 启停。再建一个的话，两个 host 各自 start() 时都会跑 recover_runtime()，
    # 互相把对方 running 的 attempt 判成 interrupted（claim_attempt 是原子的，
    # 但 recover 不是）。实际实例在 register_modules 之后从模块里取（见
    # _resolve_work_host），模块被禁用（AGENT_DISABLED_MODULES=work）才兜底自建。
    embeddings = BigModelEmbeddingProvider.from_env()
    event_bus = InMemoryEventBus()
    # 账号级权限层：work 模块要拿它构造 WorkHost（Host 是闸门二的执行者）。
    # 数据库与监听地址的口径统一由 PermissionLayer 决定（AGENT_CORE_DB / WEB_HOST），
    # Web 与 CLI 各自不重复解释一遍。
    # 现在通过 BuildContext 显式传给各模块，不再靠模块级全局槽。
    permission_layer = PermissionLayer(
        db_path=os.getenv("AGENT_CORE_DB", "agent_core.db"),
        web_host=os.getenv("WEB_HOST", "127.0.0.1"),
    )
    # 装配走与 CLI **同一个**构建函数（agent_core.capability_runtime.build_runtime）。
    # 以前 Web 在这里有自己的一套：自建 ModuleRegistry + register_modules +
    # 默认任务库路径，于是两个入口悄悄分叉（Web 有 TaskRouter、CLI 没有）。
    # TaskRouter 是内核组件：它装配失败时 build_runtime 抛 RuntimeBootstrapError，
    # 这里必须让它冒上去，不能降级成"没有路由"。
    runtime = build_runtime(
        provider=provider,
        event_bus=event_bus,
        permission_layer=permission_layer,
    )
    modules = runtime.modules
    _module_registry = modules
    registry = runtime.registry
    task_router = runtime.task_router
    # Chat 与后台能力共用同一个网关实例：审计只有一份，
    # "同一个动作编号不跑第二次"这条保护才是全系统成立的。
    action_gateway = runtime.gateway
    _opening_gateway = action_gateway
    _registration_failures = dict(runtime.registration_failures)
    enabled = list(runtime.enabled_modules)
    configured_location_store = runtime.location_store
    if configured_location_store is not None:
        _location_store = configured_location_store
    _task_services = [
        service for name in ("task_service", "condition_task_service")
        if (service := getattr(runtime, name, None)) is not None
    ]
    _tool_registry = registry
    _tool_registry_owner = modules
    # 聊天侧的 work 工具（work__submit/list/get/cancel/approve + work__plan_*）
    # 全部由 work 模块产出，这里不再补注册 —— 同一个工具名有两个注册来源时，
    # 后注册的那个只能靠"先 resolve 再注册"绕开重名校验，CLI 与 Web 的工具集
    # 也会悄悄分叉。Web 这一侧只保留 HTTP 适配（_resolve_work_host + /api/work/*）。
    work_host = _resolve_work_host(modules, roots)
    database_path = os.getenv("AGENT_CORE_DB", "agent_core.db")
    confirmation_store = SQLiteConfirmationStore(database_path)
    run_store = SQLiteRunStore(database_path)
    # 到这里 confirmation_store 与 work_host 都有了，把清理链路的第二段接上：
    # 切换完全访问时要一起拒掉的，正是这两处的挂起记录。
    permission_layer.attach(confirmation_store=confirmation_store, work_host=work_host)
    # 意图状态存进同一个 agent_core.db：chat_cli 也读这一个库，两边共享同一份上下文。
    intent_state_store = SQLiteIntentStateStore(database_path)
    conversation_repository = ConversationRepository(database_path, **_conversation_settings())
    # A process restart cannot safely resume an in-flight provider/tool call.
    # Keep its public history and pending confirmation, but make the runtime
    # state explicit instead of replaying an external action.
    conversation_repository.mark_running_interrupted()
    _repositories = initialize_memory_repositories(vector_size=embeddings.dimensions)
    _memory_writer = MemoryWriter(_repositories, embeddings)
    _memory_retriever = MemoryRetriever(_repositories, embeddings)
    attach_code_agent_memory_retriever(work_host, _memory_retriever)
    # 云端模式下本地不再自己提炼、也不自己轮转：记忆统一由云端提炼和维护，
    # 本地只负责记录与读取。两个开关默认都是开，行为与改造前一致。
    _memory_extractor = build_extractor_from_env() if extraction_enabled() else None
    _memory_maintenance_worker = MemoryMaintenanceWorker(_memory_writer) if maintenance_enabled() else None
    # 向量补偿**不随开关关闭**：合并云端变更回来的记录，以及本地自己写的新记录，
    # 都要靠它把向量补上。它平时空转，关掉只会让"忘了算向量"的记录一直查不到。
    _memory_embedding_worker = EmbeddingRetryWorker(_memory_writer)
    # 同步表与记忆记录**必须同一个库文件**：合并云端变更时要和记录写入在同一个
    # 事务里提交，才能保证"记录写进去了、水位也推了"这两件事要么都成、要么都不成。
    # 路径直接从记忆仓库取，不另读一次环境变量 —— 两处各读一次就会分叉。
    _memory_sync_store = MemorySyncStore(_repositories.db_path)
    _memory_sync_store.initialize()
    # 新版语义记忆（B 计划）：开关打开时，同一个回合只跑新版候选流程 ——
    # 旧的陪伴提炼器必须**关掉这一轮的入口**，否则同一回合会并发写两套记忆
    # （旧的按槽位、新的按七类），重复且互相打架。开关默认关闭。
    #
    # 队列只在**本端被指定为处理端**时装配（`MEMORY_PROCESSING_ENDPOINT`）。
    # 装配了队列就等于本回合走本地，云端上传会让位 —— 处理端永远只有一个。
    if local_processing_enabled():
        _memory_extractor = None
        _memory_candidate_queue = MemoryCandidateQueue(_repositories.db_path)
        _memory_candidate_queue.initialize()
        # 内容维护：更新建议走同一个队列的另一个类别，只有这一个出口。
        # 独立开关默认关闭 —— 关掉它只停止产生新的维护任务，候选链路照常。
        if content_maintenance_enabled():
            _memory_maintenance_service = MemoryMaintenanceService(_memory_writer, _repositories)
            _memory_maintenance_job_worker = MemoryMaintenanceWorker(
                _memory_candidate_queue, _memory_maintenance_service
            )
            _memory_review_scheduler = MemoryMaintenanceScheduler(_memory_maintenance_service)
        semantic_extractor = build_semantic_extractor_from_env()
        _memory_candidate_worker = MemoryCandidateWorker(
            _memory_candidate_queue,
            semantic_extractor,
            _memory_writer,
            maintenance_queue=_memory_candidate_queue if _memory_maintenance_service else None,
            model_agent=MemoryModelAgent(_memory_writer, getattr(semantic_extractor, 'provider', None)),
        )
        if _memory_maintenance_job_worker is not None:
            _memory_maintenance_job_worker.review_provider = _memory_candidate_worker.review_provider
        # 实施工作 B：当前会话的疑点澄清。**复用同一张作业表**（只补一个可选的
        # clarification_state 文本列），不新增库、不新增向量索引。开关默认关闭。
        if _main_chat_practical_enabled():
            _memory_clarification_store = ClarificationStore(_memory_candidate_queue.db_path)
            _memory_clarification_store.initialize()
            logger.info("主聊天实用能力已启用：关键背景 + 当前会话澄清闭环")
        logger.info("新版语义记忆已启用：候选提炼走持久队列，旧陪伴提炼器接手关闭")
    prompt_composer = PromptComposer()
    # 回答前搜索门：默认开启（SEARCH_GATE_ENABLED=false 可整体关闭），关闭时为 None。
    search_gate = build_search_gate(provider)

    # 子代理派发工具**不在这里注册**了。
    #
    # 以前这里自己调 `register_subagent_tool`，还包在 try/except 里只记一条 warning ——
    # 于是"Web 上没有子代理"是一种静默降级，能力快照上看不出来。
    # 现在唯一来源是能力目录（`build_runtime`），Web 与 CLI 拿到的是同一份。
    # 守卫见 tests/test_subagent_registration.py。

    # 任务形态路由：结构化任务直接交给 WorkHost，其余只给建议 + 落观测记录。
    # router 由 build_runtime 统一创建（必要时抛 RuntimeBootstrapError），
    # 这里不再自建 —— 自建会让 Web 拿到一个 roots 与 Worker 不同的实例，
    # 表现为"路由说这个目录合法、Worker 拒绝"。
    # 注意 roots（上面从 AGENT_WORK_ROOTS 解释）与 runtime.work_roots 必须同源：
    # 两者不一致说明装配用的环境变量被改过一次。
    route_store = SQLiteRouteStore(database_path)
    task_router = runtime.task_router
    if list(runtime.context.work_roots) != [str(item) for item in roots]:
        logger.warning(
            "AGENT_WORK_ROOTS changed between work-root resolution and runtime assembly; "
            "the router will use the runtime value"
        )

    # ── 管家式过程表达 ──────────────────────────────────────────────
    # narrator 只负责措辞；reporter 负责落库 + 广播 steward.progress。
    # forbidden_terms 用 registry 里的真实工具名：模型一旦把工具名写进句子，
    # steward.steward_line_issue 直接判不合格并回退模板。
    steward_narrator = StewardNarrator(
        provider,
        persona=ARcueid_PERSONA,
        forbidden_terms={spec.name for spec in registry},
    )
    steward_reporter = StewardReporter(
        steward_narrator,
        sink=_steward_sink,
        event_bus=event_bus,
    )

    def session_factory(scope: Scope) -> MemoryAgentSession:
        loop = AgentLoop(
            provider,
            registry,
            scope,
            run_store=run_store,
            confirmation_store=confirmation_store,
            # 闸门一：账号在完全访问模式下，高风险工具不再挂起等人确认。
            # 策略是同一个实例被所有会话共享 —— 它内部每次授权都重新读库，
            # 所以"切换后新会话才生效"这种问题不会发生（也**不能**发生：
            # 账号级切换的心智就是"立刻对所有会话生效"）。
            risk_policy=permission_layer.policy,
            event_bus=event_bus,
            intent_state_store=intent_state_store,
            router=task_router,
            route_store=route_store,
            max_steps=_web_max_steps(),
            routing_mode=runtime.context.routing_mode,
            legacy_shadow_enabled=runtime.context.legacy_shadow_enabled,
            steward=steward_reporter,
            gateway=action_gateway,
            search_gate=search_gate,
        )
        return MemoryAgentSession(
            loop,
            _memory_writer,
            _memory_retriever,
            prompt_composer=prompt_composer,
            persona_override=ARcueid_PERSONA,
            routing_mode=runtime.context.routing_mode,
        )

    manager = ConversationManager(
        session_factory,
        confirmation_store=confirmation_store,
        event_bus=event_bus,
        conversation_repository=conversation_repository,
        intent_state_store=intent_state_store,
    )


def _resolve_work_host(modules: Any, roots: list[Path]) -> WorkHost:
    """复用 work 模块创建的那个 WorkHost，而不是再建一个。

    同一个进程里两个 host 指向同一个 AGENT_WORK_DB 时，`WorkHost.start()` 的
    recover_runtime() 会把对方正在跑的 attempt 判成 interrupted。start()/stop()
    都有幂等守卫，复用同一实例是安全的。
    """
    resolve = getattr(modules, "resolve", None)
    spec = resolve("work") if callable(resolve) else None
    runtime = getattr(spec, "runtime", None)
    if runtime is not None:
        return runtime
    return WorkHost(os.getenv("AGENT_WORK_DB", "agent_work.db"), provider=LocalWorkerProvider(allowed_roots=roots))


def _memory_store() -> MemoryRepositories | None:
    if _repositories is not None:
        return _repositories
    if _memory_writer is not None:
        return _memory_writer.repositories
    return None


def _memory_status_from_rows(rows: list[dict[str, Any]]) -> dict[str, int]:
    totals: dict[str, int] = {"hot": 0, "warm": 0, "cold": 0}
    for row in rows:
        layer = str(row.get("layer", ""))
        if layer in totals:
            totals[layer] += 1
    return totals


def _memory_projection(record: dict[str, Any]) -> dict[str, Any]:
    metadata_raw = record.get("metadata") or "{}"
    try:
        metadata = json.loads(metadata_raw) if isinstance(metadata_raw, str) else dict(metadata_raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        metadata = {}
    provenance = {
        "source": metadata.get("source", ""),
        "source_type": metadata.get("source_type", ""),
        "turn_id": metadata.get("turn_id", ""),
        "run_id": metadata.get("run_id", ""),
        "memory_index": metadata.get("memory_index", ""),
    }
    return {
        "id": record.get("id", ""),
        "content": record.get("content", ""),
        "layer": record.get("layer", ""),
        "kind": record.get("kind", ""),
        "status": record.get("status", ""),
        "embedding_status": record.get("embedding_status", ""),
        "conflict_key": record.get("conflict_key", ""),
        "confidence": record.get("confidence"),
        "importance": record.get("importance"),
        "expires_at": record.get("expires_at"),
        "created_at": record.get("created_at", ""),
        "updated_at": record.get("updated_at", ""),
        "last_accessed_at": record.get("last_accessed_at"),
        "metadata": metadata,
        "provenance": provenance,
    }


def _memory_console_summary(
    *,
    account_id: str,
    module_id: str = "companion",
    limit: int = 20,
) -> dict[str, Any]:
    store = _memory_store()
    if store is not None:
        connection = store.connect()
        close_connection = True
    else:
        db_path = Path(os.getenv("AGENT_CORE_DB", "agent_core.db"))
        if not db_path.exists():
            return {
                "status": "degraded",
                "scope": {"owner_id": account_id, "channel": "web", "module_id": module_id},
                "checks": {
                    "memory_store": {"status": "unavailable", "repositories_initialized": False},
                    "memory_extraction": {"status": "degraded", "configured": False},
                    "memory_cycle": {
                        "status": "degraded",
                        "maintenance_worker_started": False,
                        "embedding_worker_started": False,
                    },
                },
                "summary": {
                    "total": 0,
                    "layers": {"hot": 0, "warm": 0, "cold": 0},
                    "status_counts": {},
                    "embedding_counts": {},
                    "recent_count": 0,
                },
                "latest_embedding_error": "",
                "recent_records": [],
                "timestamp": datetime.now().isoformat(),
            }
        connection = sqlite3.connect(db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        close_connection = True

    try:
        try:
            rows = [dict(row) for row in connection.execute(
                """
                SELECT *
                FROM memory_records
                WHERE owner_id=? AND channel=? AND module_id=?
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (account_id, "web", module_id, limit),
            ).fetchall()]
            counts = [dict(row) for row in connection.execute(
                """
                SELECT layer, status, COUNT(*) AS count
                FROM memory_records
                WHERE owner_id=? AND channel=? AND module_id=?
                GROUP BY layer, status
                """,
                (account_id, "web", module_id),
            ).fetchall()]
            embedding_counts = {
                str(row["embedding_status"]): int(row["count"])
                for row in connection.execute(
                    """
                    SELECT embedding_status, COUNT(*) AS count
                    FROM memory_records
                    WHERE owner_id=? AND channel=? AND module_id=?
                    GROUP BY embedding_status
                    """,
                    (account_id, "web", module_id),
                ).fetchall()
            }
            error_row = connection.execute(
                """
                SELECT last_embedding_error
                FROM memory_records
                WHERE owner_id=? AND channel=? AND module_id=?
                  AND last_embedding_error IS NOT NULL AND last_embedding_error <> ''
                ORDER BY updated_at DESC, rowid DESC
                LIMIT 1
                """,
                (account_id, "web", module_id),
            ).fetchone()
            total = sum(int(row["count"]) for row in counts)
            layer_counts = {"hot": 0, "warm": 0, "cold": 0}
            status_counts: dict[str, int] = {}
            for row in counts:
                layer = str(row["layer"])
                status = str(row["status"])
                count = int(row["count"])
                if layer in layer_counts:
                    layer_counts[layer] += count
                status_counts[status] = status_counts.get(status, 0) + count
            summary = {
                "total": total,
                "layers": layer_counts,
                "status_counts": status_counts,
                "embedding_counts": embedding_counts,
                "recent_count": len(rows),
            }
            store_status = "ok"
            if store is None and not db_path.exists():
                store_status = "unavailable"
        except (sqlite3.Error, OSError):
            return {
                "status": "degraded",
                "scope": {"owner_id": account_id, "channel": "web", "module_id": module_id},
                "checks": {
                    "memory_store": {"status": "unavailable", "repositories_initialized": store is not None},
                    "memory_extraction": {"status": "degraded", "configured": _memory_extractor is not None},
                    "memory_cycle": {
                        "status": "degraded",
                        "maintenance_worker_started": False,
                        "embedding_worker_started": False,
                    },
                },
                "summary": {
                    "total": 0,
                    "layers": {"hot": 0, "warm": 0, "cold": 0},
                    "status_counts": {},
                    "embedding_counts": {},
                    "recent_count": 0,
                },
                "latest_embedding_error": "",
                "recent_records": [],
                "timestamp": datetime.now().isoformat(),
            }
    finally:
        if close_connection:
            connection.close()

    return {
        "status": "ok" if _memory_extractor is not None and _memory_workers_started else "degraded",
        "scope": {"owner_id": account_id, "channel": "web", "module_id": module_id},
        "checks": {
            "memory_store": {
                "status": store_status,
                "repositories_initialized": True,
            },
            "memory_extraction": {
                "status": "ok" if _memory_extractor is not None else "degraded",
                "configured": _memory_extractor is not None,
            },
            "memory_cycle": {
                "status": "ok" if _memory_workers_started else "degraded",
                "maintenance_worker_started": _memory_workers_started and _memory_maintenance_worker is not None,
                "embedding_worker_started": _memory_workers_started and _memory_embedding_worker is not None,
            },
        },
        "summary": summary,
        "latest_embedding_error": str(error_row[0]) if error_row and error_row[0] else "",
        "recent_records": [_memory_projection(row) for row in rows],
        "timestamp": datetime.now().isoformat(),
    }


def _cloud_sync_health() -> dict:
    """云端同步的健康快照。

    没开同步 → ``disabled``；开了但装配不起来 → ``misconfigured``，
    并把两种原因分开写：一种是环境变量没配（URL / 口令），
    另一种是同步表压根没建起来（运行时初始化没走到那一步）。
    混成一句话的话，排查时会朝着错的方向找。
    """
    if not cloud_sync_enabled():
        return {"status": "disabled"}
    if _memory_sync_store is None:
        return {"status": "misconfigured", "reason": "同步存储未初始化（运行时初始化未完成）"}
    if _memory_sync_runner is None:
        return {"status": "misconfigured", "reason": "缺 CLOUD_MEMORY_URL 或口令"}
    try:
        state = _memory_sync_store.status()
    except Exception as exc:  # noqa: BLE001 - 健康检查自己不抛异常
        return {"status": "unknown", "reason": type(exc).__name__}
    worker_started = _memory_sync_task is not None and not _memory_sync_task.done()
    return {
        "status": "ok" if worker_started else "degraded",
        "worker_started": worker_started,
        "last_sync_at": state["last_sync_at"],
        "upload_pending": state["upload_pending"],
        "access_pending": state["access_pending"],
        "change_cursor": state["change_cursor"],
        "last_result": _memory_sync_runner.last_result,
    }


def _ensure_task_result_bridge() -> TaskResultBridge | None:
    """接入定时任务终态回执桥（幂等）。

    这条链路上**只有**这一个出口写任务回执进对话。以前这里是
    ``_task_event_message()``：直接把事件里的 error / result 拼成
    "任务失败：标题（RuntimeError: ...）" 写进历史，异常原文与本机绝对路径
    会原样出现在用户的聊天气泡里；而且它从没被 lifespan 调用过 ——
    真实链路上任务结果既不干净、也不一定出现。
    """
    global _task_result_bridge
    if event_bus is None:
        return None
    if _task_result_bridge is None:
        _task_result_bridge = TaskResultBridge(
            event_bus,
            conversation_repository,
            conversation_manager=manager,
            on_terminal=_record_task_result_event,
        )
    _task_result_bridge.subscribe()
    return _task_result_bridge


async def _record_task_result_event(payload: dict, scope: Scope) -> None:
    """任务终态 → 业务结果记忆（B 计划第五步）。

    只记**真的结束了**的任务：进度播报不是结论，``record_event`` 会直接返回空。
    开关关着或记忆写入没装配时什么都不做。整段在后台线程里跑，
    不拖慢任务回执的广播。
    """
    if not semantic_memory_enabled() or _memory_writer is None:
        return
    event = event_from_task_result(
        payload,
        event_id="task:{}:{}:{}".format(
            payload.get("task_id", ""), payload.get("run_id", ""), payload.get("status", "")
        ),
    )
    try:
        await asyncio.to_thread(record_event, _memory_writer, scope, event)
    except Exception:
        logger.exception("业务结果写入记忆失败", extra={"task_id": payload.get("task_id", "")})


async def _start_task_services() -> None:
    global _task_services_started, _module_registry
    if _task_services_started:
        return
    if _module_registry is not None:
        await _module_registry.start_all()
    for service in _task_services:
        start = getattr(service, "start", None)
        if callable(start):
            await start()
    _task_services_started = True


async def _start_memory_workers() -> None:
    global _memory_workers_started, _memory_sync_runner, _memory_sync_task, _memory_candidate_task
    if _memory_workers_started:
        return
    if _memory_embedding_worker is not None:
        _memory_embedding_worker.start()
    if _memory_maintenance_worker is not None:
        _memory_maintenance_worker.start()
    if _memory_candidate_worker is not None and _memory_candidate_queue is not None:
        # 候选提炼与聊天共用事件循环；写 SQLite 的部分在 worker 内部走 to_thread。
        _memory_candidate_task = asyncio.create_task(
            _memory_candidate_worker.run_forever(), name="memory-candidate-queue"
        )
    if _memory_maintenance_job_worker is not None and _memory_candidate_queue is not None:
        # 内容维护作业与候选作业同一个队列、不同类别，各自排空自己的那一类。
        _memory_maintenance_task = asyncio.create_task(
            _memory_maintenance_job_worker.run_forever(), name="memory-maintenance-jobs"
        )
    if _memory_review_scheduler is not None:
        # 定期复核：30 分钟一批。没有新证据时这一趟**不调模型**，只把到点记录
        # 排到下一次 —— 到点不等于过期。
        _memory_review_task = asyncio.create_task(
            _memory_review_scheduler.run_forever(), name="memory-content-review"
        )
    if cloud_sync_enabled():
        # 这里刻意不写成 `if cloud_sync_enabled() and store is not None`：
        # 那种写法在 store 缺失时是**静默跳过**，看起来跟"没开同步"一模一样。
        # 分开判，缺什么就报什么。
        if _memory_sync_store is None or _repositories is None:
            logger.warning("云端同步已打开，但同步存储没建起来，同步未启动")
        else:
            runner = build_runner_from_env(_repositories, _memory_sync_store)
            if runner is None:
                logger.warning("云端同步已打开，但 CLOUD_MEMORY_URL / CLOUD_MEMORY_TOKEN 没配全，同步未启动")
            else:
                _memory_sync_runner = runner
                # 同步循环与聊天共用事件循环：本地向量库是单进程独占的嵌入式模式，
                # 另起一个进程会直接抢不到向量目录。所有阻塞写入都走 to_thread。
                _memory_sync_task = asyncio.create_task(runner.run(), name="memory-cloud-sync")
    _memory_workers_started = True


async def _stop_memory_workers() -> None:
    global _memory_workers_started, _memory_sync_runner, _memory_sync_task, _memory_candidate_task
    global _memory_maintenance_task, _memory_review_task
    if not _memory_workers_started:
        return
    for task_name in ("_memory_maintenance_task", "_memory_review_task"):
        task = globals()[task_name]
        if task is not None:
            # 与候选作业同理：作业已经落库，这里只管停循环，没跑完的下次启动继续。
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            globals()[task_name] = None
    if _memory_candidate_task is not None:
        # 作业已经落库：这里只管停循环，没跑完的留着下次启动继续（不会丢）。
        _memory_candidate_task.cancel()
        with suppress(asyncio.CancelledError):
            await _memory_candidate_task
        _memory_candidate_task = None
    if _memory_sync_runner is not None:
        _memory_sync_runner.request_stop()
        # 退出前补传一次，最多等 5 秒。传不完的留在队列里，下次启动接着传 ——
        # 这也是"聊天不等待同步完成"能成立的前提：代价被限制在退出这一刻。
        await _memory_sync_runner.flush_for_shutdown()
    if _memory_sync_task is not None:
        _memory_sync_task.cancel()
        with suppress(asyncio.CancelledError):
            await _memory_sync_task
        _memory_sync_task = None
    _memory_sync_runner = None
    if _memory_maintenance_worker is not None:
        _memory_maintenance_worker.stop(timeout=5)
    if _memory_embedding_worker is not None:
        _memory_embedding_worker.stop(timeout=5)
    _memory_workers_started = False


async def _conversation_maintenance_loop() -> None:
    """Periodically trim inactive runtime cache entries and durable history."""
    while True:
        await asyncio.sleep(_conversation_cleanup_interval())
        try:
            if manager is not None:
                await manager.evict_idle()
            # `cleanup()` 与 `clear_orphans()` 都是同步 SQLite，必须走工作线程：
            # 这个循环跑在 event loop 上，而 loop 上同时还有 SSE 推送、
            # 开场白请求和定时任务 —— 一次全表 DELETE 就能让所有这些一起卡住。
            if conversation_repository is not None:
                await asyncio.to_thread(conversation_repository.cleanup)
            # 顺序：先按保留期删会话，再清"父会话已经没了"的意图状态。
            # 这一轮 cleanup() 删掉的会话，它的状态就在本轮被一起回收，
            # 不用等下一个清理周期再发现。
            # **这个先后不能反**，也不能并行：反了就会把这一轮刚删的会话的
            # 状态留成孤儿，而要等下一轮才清。
            if intent_state_store is not None:
                await asyncio.to_thread(intent_state_store.clear_orphans)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Conversation maintenance failed")


def _start_conversation_maintenance() -> None:
    global _conversation_maintenance_task
    if _conversation_maintenance_task is None or _conversation_maintenance_task.done():
        _conversation_maintenance_task = asyncio.create_task(
            _conversation_maintenance_loop(), name="conversation-maintenance"
        )


async def _stop_conversation_maintenance() -> None:
    global _conversation_maintenance_task
    task = _conversation_maintenance_task
    _conversation_maintenance_task = None
    if task is None or task.done():
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _track_memory_postprocess_task(task: asyncio.Task[Any]) -> asyncio.Task[Any]:
    _memory_postprocess_tasks.add(task)
    task.add_done_callback(_memory_postprocess_tasks.discard)
    return task


async def _drain_memory_postprocess_tasks(timeout: float = 10.0) -> None:
    if not _memory_postprocess_tasks:
        return
    tasks = list(_memory_postprocess_tasks)
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout)
    except asyncio.TimeoutError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        _memory_postprocess_tasks.difference_update(tasks)


async def _shutdown_runtime() -> None:
    global manager, work_host, conversation_repository, intent_state_store, _repositories, event_bus, _task_services_started, _task_result_bridge, _module_registry, _work_bridge_task, _work_progress_bridge_task
    await _stop_conversation_maintenance()
    # 先退订任务回执桥：它挂在事件总线上，总线下线后订阅表会被清空，
    # 这里显式退订是为了让桥自己的订阅 id 也归零，重复初始化时不会漏订阅。
    if _task_result_bridge is not None:
        _task_result_bridge.unsubscribe()
        _task_result_bridge = None
    # 先停桥再停 Host：桥订阅的是 Host 的事件流，Host 停了流不会自己断，
    # 必须显式取消，否则进程收不到这个 task 的取消信号。
    # 过程桥必须先收口：它可能还有一条待发的过程句，不能排到终态回执后面。
    progress_task = _work_progress_bridge_task
    _work_progress_bridge_task = None
    if progress_task is not None and not progress_task.done():
        progress_task.cancel()
        await asyncio.gather(progress_task, return_exceptions=True)
    task = _work_bridge_task
    _work_bridge_task = None
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    if manager is not None:
        await manager.shutdown()
        manager = None
    if work_host is not None:
        await work_host.stop()
        work_host = None
    for service in _task_services:
        stop = getattr(service, "stop", None)
        if callable(stop):
            await stop()
    if _module_registry is not None:
        await _module_registry.stop_all()
        _module_registry = None
    await _stop_memory_workers()
    await _drain_memory_postprocess_tasks()
    if event_bus is not None:
        await event_bus.shutdown()
        event_bus = None
    _task_services_started = False
    if _repositories is not None:
        _repositories.close()
        _repositories = None
    conversation_repository = None
    intent_state_store = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _runtime_initialization_error, _work_bridge_task, _work_progress_bridge_task
    try:
        _initialize_runtime()
        await _start_task_services()
        await _start_memory_workers()
        _start_conversation_maintenance()
        # work→chat 回传桥：必须挂在 _start_task_services 之后 ——
        # Host 没 start 时 events() 也能订阅，但先启动再订阅能保证不漏首批事件。
        # 代码任务过程桥先建：结果桥写终态回执之前要先通过 progress_hook
        # 把那次执行的过程进度关掉（顺序反了会出现"回执之后又冒进度"）。
        progress_bridge = build_work_progress_bridge(work_host, steward_reporter)
        if progress_bridge is not None:
            _work_progress_bridge_task = asyncio.create_task(
                progress_bridge.run(), name="work-progress-bridge"
            )
        if work_host is not None and conversation_repository is not None:
            # 桥与主循环共用同一个 narrator：后台任务的结果和过程句
            # 出自同一张嘴，不会出现"前半句像人、后半句像系统"的割裂。
            bridge = WorkResultBridge(
                work_host,
                conversation_repository,
                event_bus,
                steward=steward_narrator,
                conversation_manager=manager,
                progress_hook=(
                    progress_bridge.close_attempt if progress_bridge is not None else None
                ),
            )
            _work_bridge_task = asyncio.create_task(bridge.run(), name="work-result-bridge")
        # 代码任务过程桥：把执行层的真实阶段（看文件 / 改代码 / 跑验证 / 补丁好了）
        # 写回发起会话。与结果桥**各自订阅** host.events()（每次调用一个新队列），
        # 所以两边都能拿到全部事件，不存在抢事件的问题。
        # 定时任务终态回执桥：挂在事件总线上，订阅即接入。它必须在
        # _start_task_services 之后 —— 任务调度器先起来，才不会再出现
        # "任务跑完了但对话里没人说"的空窗。
        _ensure_task_result_bridge()
        _runtime_initialization_error = ""
    except Exception:
        # Keep import and health diagnostics available when optional credentials
        # are not configured; the first chat request returns a clear failure.
        _runtime_initialization_error = "runtime initialization failed"
        logger.exception("web runtime initialization failed")
    yield
    await _shutdown_runtime()


app = FastAPI(title="Arcueid Agentcore Web", lifespan=lifespan)


_scenario_instance: ScenarioRuntime | None = None


def _get_scenario_runtime() -> ScenarioRuntime:
    """Create the scenario loop without bootstrapping main chat or memory."""
    global _scenario_instance
    if _scenario_instance is None:
        root = Path(__file__).resolve().parent
        _scenario_instance = ScenarioRuntime(
            ScenarioStore(Path(os.getenv("AGENT_SCENARIO_DB", str(root / "data" / "scenario_runtime.db")))),
            OpenAICompatibleProvider.from_env(),
            Path(os.getenv("AGENT_SCENARIO_STICKERS", str(root / "data" / "scenario_stickers"))),
            analysis_provider=OpenAICompatibleProvider.from_env(thinking_enabled=True),
        )
    return _scenario_instance


app.include_router(make_scenario_router(_get_scenario_runtime))


#: 未处理异常的落地文件。默认相对项目根（服务的 cwd 就是项目根）。
ERROR_LOG_PATH = Path(os.getenv("AGENT_ERROR_LOG", "data/errors.log"))


@app.exception_handler(Exception)
async def _log_unhandled_exception(request: Request, exc: Exception) -> PlainTextResponse:
    """把未处理异常的调用栈写进文件，再按 FastAPI 的默认语义回 500。

    为什么要自己接这一手：FastAPI 默认只把 traceback 打到 stdout，而这个服务
    通常是在某个终端窗口里前台启动的 —— 窗口一关、或日志滚过去，现场就没了。
    排查"开场白接口稳定 500"时就卡在这里：接口每次都 500，可日志里连一行都没有，
    只能靠"猜一个原因、改一处代码、重启一次"去逼近，成本全花在看不见上。

    响应体与状态码保持与默认一致（纯文本 Internal Server Error），
    所以这是一次纯粹的"加日志"，不改变任何对外行为。
    """
    try:
        ERROR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with ERROR_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(
                f"\n===== {datetime.now(timezone.utc).isoformat()} "
                f"{request.method} {request.url.path} =====\n"
            )
            handle.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except Exception:  # 日志写不进去也不能影响响应本身
        logger.debug("写异常日志失败", exc_info=True)
    return PlainTextResponse("Internal Server Error", status_code=500)

# CORS 配置 - 允许前端跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",      # Vite 开发服务器
        "http://127.0.0.1:5173",
        "http://localhost:8765",      # 本地文件访问
        "http://127.0.0.1:8765",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


def _scope(request: ChatRequest | DecisionRequest) -> Scope:
    return build_web_scope(request.account_id, request.conversation_id)


def _web_scope_from_path(account_id: str, conversation_id: str) -> Scope:
    """Validate path/query identifiers before constructing the authoritative Scope."""
    try:
        validate_field_length(account_id, MAX_OWNER_ID_LENGTH, "account_id")
        validate_safe_id(account_id, "account_id")
        validate_field_length(conversation_id, MAX_CONVERSATION_ID_LENGTH, "conversation_id")
        validate_safe_id(conversation_id, "conversation_id")
    except InputValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc
    return build_web_scope(account_id, conversation_id)


def _conversation_repository() -> ConversationRepository:
    if _module_registry is None and _repositories is None:
        try:
            _initialize_runtime()
        except Exception as exc:
            logger.warning("health runtime initialization incomplete: %s", exc)
    if conversation_repository is None:
        raise HTTPException(status_code=503, detail="conversation repository is unavailable")
    return conversation_repository


def _today_user_event_context(scope: Scope) -> str:
    if conversation_repository is None:
        return ""
    rows = conversation_repository.today_user_events(scope, limit=20)
    if not rows:
        return ""
    lines = [
        "[TODAY_USER_EVENTS]",
        "以下是同一用户今天在其他会话中的原话，仅代表用户自述，不是已验证事实；不得据此编造医学或法律结论。",
    ]
    budget_chars = 3200
    selected: list[str] = []
    used = 0
    for row in reversed(rows):
        line = f"- {row['created_at']} ({row['conversation_id']}): {row['content']}"
        if used + len(line) > budget_chars:
            continue
        selected.append(line)
        used += len(line)
    lines.extend(reversed(selected))
    return "\n".join(lines)


def _conversation_in_use(scope: Scope) -> bool:
    return bool(manager is not None and manager.is_busy(scope))


def clean_character_reply(value: str) -> str:
    """Remove stage directions from assistant output without touching user text."""
    cleaned = re.sub(r"（[^（）\n]{1,120}）", "", str(value or ""))
    cleaned = re.sub(r"\([^()\n]{1,120}\)", "", cleaned)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


_runtime_time_context = build_relative_time_context


def _location_key(scope: Scope) -> tuple[str, str, str]:
    """位置限流表与 LocationStore 共用同一套键：账号 + 渠道 + 模块。

    刻意**不含 conversation_id**：限流和保存范围必须同口径 ——
    按会话限流、按账号保存，会让"换了会话就能绕开 10 秒一次的限制"，
    而存的还是同一条位置。
    """
    return (scope.owner_id, scope.channel, scope.module_id)


@app.post("/api/location")
async def update_location(request: LocationRequest) -> dict[str, Any]:
    """Store one browser location update in the ephemeral session cache."""
    global _location_last_updates
    scope = build_web_scope(request.account_id, request.conversation_id)
    key = _location_key(scope)
    now = time.monotonic()
    previous = _location_last_updates.get(key)
    if previous is not None and now - previous < 10:
        raise HTTPException(status_code=429, detail="location updates are limited to once every 10 seconds")
    if request.captured_at.tzinfo is None:
        raise HTTPException(status_code=422, detail="captured_at must include timezone")
    try:
        snapshot = _location_store.update(
            scope, request.latitude, request.longitude, request.accuracy_m, request.captured_at
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _location_last_updates[key] = now
    return {"status": "ok", "output": snapshot}


@app.delete("/api/location")
async def clear_location(account_id: str = "local-user", conversation_id: str = "web") -> dict[str, Any]:
    scope = build_web_scope(account_id, conversation_id)
    _location_last_updates.pop(_location_key(scope), None)
    return {"status": "ok", "output": {"cleared": _location_store.clear(scope)}}


def enforce_length_limit(text: str, max_chars: int = 150) -> str:
    """
    强制字数限制：超过 max_chars 时在最后一个标点处截断
    """
    if len(text) <= max_chars:
        return text

    # 在 max_chars 之前找最后一个标点符号
    truncated = text[:max_chars]
    last_punctuation = max(
        truncated.rfind('。'),
        truncated.rfind('！'),
        truncated.rfind('？'),
        truncated.rfind('…')
    )

    if last_punctuation > 50:  # 至少保留 50 字
        return truncated[:last_punctuation + 1]
    else:
        return truncated + '…'


def _capability_cache_key() -> tuple[Any, ...]:
    return (
        id(_module_registry),
        id(_tool_registry),
        os.getenv("AGENT_CORE_DB", "agent_core.db"),
        bool(os.getenv("GEMINI_API_KEY") and os.getenv("GOOGLE_GEMINI_BASE_URL") and os.getenv("GEMINI_MODEL")),
        bool(os.getenv("OPENAI_API_KEY") and os.getenv("OPENAI_API_MODEL")),
    )


def _provider_health() -> tuple[bool, dict[str, Any]]:
    configured = bool(
        (os.getenv("GEMINI_API_KEY") and os.getenv("GOOGLE_GEMINI_BASE_URL") and os.getenv("GEMINI_MODEL"))
        or (os.getenv("OPENAI_API_KEY") and os.getenv("OPENAI_API_MODEL"))
    )
    return configured, {
        "status": "healthy" if configured else "unhealthy",
        "configured": configured,
        "code": "" if configured else "provider_not_configured",
        "reason": "" if configured else "model provider is not configured",
    }


def _database_health() -> dict[str, Any]:
    conn: sqlite3.Connection | None = None
    try:
        db_path = Path(os.getenv("AGENT_CORE_DB", "agent_core.db")).expanduser().resolve()
        conn = sqlite3.connect(f"{db_path.as_uri()}?mode=rw", uri=True, timeout=5.0)
        quick_check = conn.execute("PRAGMA quick_check").fetchone()
        if quick_check != ("ok",):
            raise sqlite3.DatabaseError("database integrity check failed")
        return {"status": "healthy", "writable": True, "code": "", "reason": ""}
    except Exception:
        logger.exception("capability database probe failed")
        return {
            "status": "unhealthy",
            "writable": False,
            "code": "database_unavailable",
            "reason": "database is not writable",
        }
    finally:
        if conn is not None:
            conn.close()


async def _build_capability_snapshot(*, force: bool) -> dict[str, Any]:
    global _runtime_initialization_error
    try:
        _initialize_runtime()
        _runtime_initialization_error = ""
    except Exception:
        _runtime_initialization_error = "runtime initialization failed"
        logger.exception("capability runtime initialization failed")

    provider_configured, provider = _provider_health()
    # `PRAGMA quick_check` 是同步磁盘扫描，必须走工作线程：它在能力探测接口里，
    # 而这个接口会被前端定期拉；一次大库的 quick_check 能让整个 loop 停住。
    database = await asyncio.to_thread(_database_health)
    if _module_registry is None:
        modules: list[dict[str, Any]] = []
    else:
        from agent_core.tools import ToolRegistry

        registry = _tool_registry if _tool_registry_owner is _module_registry and _tool_registry is not None else ToolRegistry()
        modules = (await _module_registry.capability_snapshot_async(registry, force=force))["modules"]

    checked_at = datetime.now(timezone.utc).isoformat()
    for module in modules:
        module["checked_at"] = checked_at

    module_diagnostics: dict[str, dict[str, str]] = {}
    module_failures: dict[str, str] = {}
    for module in modules:
        code = str(module.get("code") or "")
        reason = str(module.get("reason") or "")
        if not code:
            for tool in module.get("tools", []):
                if tool.get("registration") != "registered" or not tool.get("advertised_to_model"):
                    code = str(tool.get("code") or "module_degraded")
                    reason = str(tool.get("reason") or "module capability is degraded")
                    break
        module_diagnostics[str(module["id"])] = {
            "status": str(module["status"]),
            "code": code,
            "reason": reason,
        }
        if module["status"] != "healthy":
            module_failures[str(module["id"])] = code or "module_degraded"

    checks: dict[str, Any] = {
        "provider": provider,
        "database": database,
        "modules": {
            "status": "healthy" if all(module["status"] == "healthy" for module in modules) else "degraded",
            "registered": module_diagnostics,
            "failures": module_failures,
        },
        "mcp": {"status": "unavailable", "code": "mcp_not_configured", "reason": "MCP is not configured"},
        "player": {"status": "unavailable", "code": "player_unavailable", "reason": "player is unavailable"},
        "memory": {"status": "healthy" if _repositories is not None else "degraded", "repositories_initialized": _repositories is not None},
        "memory_extraction": {"status": "healthy" if _memory_extractor is not None else "degraded", "configured": _memory_extractor is not None},
        "memory_cycle": {
            "status": "healthy" if _memory_workers_started else "degraded",
            "maintenance_worker_started": _memory_workers_started and _memory_maintenance_worker is not None,
            "embedding_worker_started": _memory_workers_started and _memory_embedding_worker is not None,
        },
        # 云端同步的状态要能一眼看出来。隧道没开的时候这里会一直显示积压往上走、
        # 上次同步时间停在很久以前 —— 比翻日志快得多，也不用去连服务器。
        "cloud_sync": _cloud_sync_health(),
    }
    if _runtime_initialization_error:
        checks["runtime"] = {
            "status": "unhealthy",
            "code": "runtime_initialization_failed",
            "reason": _runtime_initialization_error,
        }

    for module in modules:
        for dependency in module.get("dependencies", []):
            name = str(dependency.get("name") or "")
            if name in {"mcp", "player"}:
                checks[name] = dict(dependency)

    media_spec = _module_registry.resolve("media") if _module_registry is not None else None
    media_runtime = getattr(media_spec, "runtime", None) if media_spec is not None else None
    tts_configured = bool(getattr(media_runtime, "tts_url", None) and (os.getenv("TTS_SERVICE_URL") or os.getenv("GPT_SOVITS_URL")))
    checks["media"] = {"status": "degraded" if not tts_configured else "healthy", "tts": "healthy" if tts_configured else "unavailable", "asr": "unavailable", "asr_reason": "unavailable"}
    checks["tts"] = {"status": "healthy" if tts_configured else "unavailable", "configured": tts_configured}
    checks["asr"] = {"status": "unavailable", "reason": "unavailable", "implemented": False}

    core_healthy = provider_configured and database.get("status") == "healthy" and not _runtime_initialization_error
    required_modules_healthy = all(
        module["status"] == "healthy"
        for module in modules
        if _module_registry is not None and _module_registry.required_for_readiness(module["id"])
    )
    optional_degraded = any(module["status"] != "healthy" for module in modules) or any(
        item.get("status") in {"degraded", "unavailable"}
        for item in checks.values()
        if isinstance(item, dict)
    )
    status = "unhealthy" if not core_healthy else ("degraded" if optional_degraded else "healthy")
    return {
        "status": status,
        "ready": core_healthy and required_modules_healthy,
        "timestamp": checked_at,
        "channel": "web",
        "provider_configured": provider_configured,
        "checks": checks,
        "modules": modules,
    }


async def _capability_snapshot(*, force: bool = False) -> dict[str, Any]:
    global _capability_snapshot_cache, _capability_snapshot_lock, _capability_refresh_task
    key = _capability_cache_key()
    now = time.monotonic()
    cached = _capability_snapshot_cache
    if not force and cached is not None and cached[0] == key and now - cached[1] < 5.0:
        return cached[2]
    if _capability_snapshot_lock is None:
        _capability_snapshot_lock = asyncio.Lock()

    if force:
        task = _capability_refresh_task
        if task is not None and not task.done():
            return await asyncio.shield(task)
        async with _capability_snapshot_lock:
            task = _capability_refresh_task
            if task is None or task.done():
                task = asyncio.create_task(_build_capability_snapshot(force=True))
                _capability_refresh_task = task
        try:
            snapshot = await asyncio.shield(task)
            _capability_snapshot_cache = (key, time.monotonic(), snapshot)
            return snapshot
        finally:
            if task.done() and _capability_refresh_task is task:
                _capability_refresh_task = None

    async with _capability_snapshot_lock:
        cached = _capability_snapshot_cache
        now = time.monotonic()
        if not force and cached is not None and cached[0] == key and now - cached[1] < 5.0:
            return cached[2]
        snapshot = await _build_capability_snapshot(force=force)
        _capability_snapshot_cache = (key, time.monotonic(), snapshot)
        return snapshot


@app.get("/api/capabilities")
async def capabilities() -> dict[str, Any]:
    """Return the cached, safe capability snapshot for operations UI."""
    return await _capability_snapshot()


@app.post("/api/capabilities/refresh")
async def refresh_capabilities() -> dict[str, Any]:
    """Force a bounded runtime probe without rebuilding the tool registry."""
    return await _capability_snapshot(force=True)


@app.get("/api/ready")
async def readiness() -> JSONResponse:
    snapshot = await _capability_snapshot()
    payload = {"status": "ready" if snapshot["ready"] else "not_ready", "checks": snapshot["checks"]}
    return JSONResponse(status_code=200 if snapshot["ready"] else 503, content=payload)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    """Return an evidence-backed runtime capability snapshot."""
    return await _capability_snapshot()


@app.get("/api/route/stats")
async def route_stats(limit: int = 20) -> dict[str, Any]:
    """路由决策观测：总量、路径分布、旧路由误判与 model 影子分歧。

    通用误判只统计 legacy 模式；model 模式中的 decided_* 是旧路由影子预测，
    与实际路径不同表示“旧新分歧”，不表示主模型误路由。这个端点只读。
    """
    _initialize_runtime()
    store = SQLiteRouteStore(os.getenv("AGENT_CORE_DB", "agent_core.db"))
    try:
        bounded = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        bounded = 20
    return store.stats(limit=bounded)


def _harness_health_states(health: dict[str, Any] | None) -> dict[str, Any]:
    """把分层健康报告折成页面要展示的五档状态。

    每层三态：ok（这层过了）/ failed（这层失败）/ not_run（前面的层就挡住
    了，这层没跑到）。旧格式报告没有分层字段时返回空字典，页面按旧逻辑渲染。
    """
    if not health:
        return {}
    if "probe" not in health and "handshake" not in health:
        return {}
    failure_stage = str(health.get("failure_stage") or "")

    def layer(ok: bool, stage: str) -> str:
        if ok:
            return "ok"
        return "failed" if failure_stage.startswith(stage) or failure_stage == stage else "not_run"

    chain = health.get("host_chain") or {}
    return {
        "install": "failed" if failure_stage == "config" else "ok",
        "probe": layer(bool((health.get("probe") or {}).get("ok")), "probe"),
        "handshake": layer(bool((health.get("handshake") or {}).get("ok")), "handshake"),
        "executable": "ok" if health.get("executable") else "failed",
        "host_chain": {
            "verifier": bool(chain.get("verifier")),
            "auto_apply": bool(chain.get("auto_apply")),
            "auto_apply_enabled": bool(chain.get("auto_apply_enabled")),
        },
        "failure_stage": failure_stage,
    }


@app.get("/api/work/capabilities")
async def work_capabilities() -> dict[str, Any]:
    host = await _ensure_work_host()
    local_provider = resolve_work_provider(host, "file.list")
    volume_available = False
    try:
        volume_available = bool(getattr(local_provider, "_volume_adjust", None))
    except Exception:
        pass
    files = getattr(local_provider, "files", None)
    # Provider registry + Harness health: the page needs both to decide whether
    # "DeepSeek Harness" can be picked right now, and *why* when it cannot.
    registry = host.provider_registry()
    harness = await host.provider_health("deepseek_harness")
    matching = await host.capability_report()
    return {"provider_id": host.provider.capabilities.provider_id, "task_types": list(host.provider.capabilities.task_types), "allowed_roots": [str(path) for path in getattr(files, "allowed_roots", ())], "screenshot_available": "screen.capture" in host.provider.capabilities.task_types, "volume_available": volume_available, "providers": registry, "harness": harness, "harness_states": _harness_health_states(harness), "matching": matching}


@app.get("/api/work/match")
async def work_provider_match(provider_id: str = "", task_type: str = "code.agent") -> dict[str, Any]:
    """诊断口：单个 provider × task_type 为什么匹配、降级或被拒绝。"""
    host = await _ensure_work_host()
    return await host.match_provider(provider_id=provider_id, task_type=task_type)


def _work_scope(account_id: str, conversation_id: str) -> Scope:
    return Scope(owner_id=account_id, channel="web", conversation_id=conversation_id, module_id="work")


async def _ensure_work_host() -> WorkHost:
    global work_host
    _initialize_runtime()
    if work_host is None:
        raise HTTPException(status_code=503, detail="work host unavailable")
    await work_host.start()
    return work_host


@app.get("/api/work/browse")
async def work_browse_directories(path: str | None = None) -> dict[str, Any]:
    """目录选择器的数据源：只允许列举 allowed_roots 之内的子目录。"""
    host = await _ensure_work_host()
    local_provider = resolve_work_provider(host, "file.list")
    files = getattr(local_provider, "files", None)
    if files is None:
        raise HTTPException(status_code=503, detail="work file provider unavailable")
    try:
        return files.list_directories(path)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (NotADirectoryError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/work/tasks", status_code=202)
async def work_task_submit(request: WorkTaskRequest) -> dict[str, Any]:
    host = await _ensure_work_host()
    try:
        return await host.submit(_work_scope(request.account_id, request.conversation_id), request.objective, task_type=request.task_type, input=request.input)
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/work/tasks")
async def work_task_list(account_id: str = "local-user", conversation_id: str = "web") -> dict[str, Any]:
    host = await _ensure_work_host()
    return {"tasks": host.list(_work_scope(account_id, conversation_id))}


@app.get("/api/work/tasks/{work_item_id}")
async def work_task_detail(work_item_id: str, account_id: str = "local-user", conversation_id: str = "web") -> dict[str, Any]:
    host = await _ensure_work_host()
    try:
        return host.get(work_item_id, _work_scope(account_id, conversation_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="work task not found") from exc


@app.post("/api/work/tasks/{work_item_id}/cancel")
async def work_task_cancel(work_item_id: str, account_id: str = "local-user", conversation_id: str = "web") -> dict[str, Any]:
    host = await _ensure_work_host()
    try:
        return {"work_item_id": work_item_id, "cancelled": await host.cancel(work_item_id, _work_scope(account_id, conversation_id))}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="work task not found") from exc


@app.post("/api/work/tasks/{work_item_id}/retry", status_code=202)
async def work_task_retry(work_item_id: str, confirm_unknown: bool = False, account_id: str = "local-user", conversation_id: str = "web") -> dict[str, Any]:
    """重新执行一个已结束的任务。

    `confirm_unknown=1` 是"结果未知"任务的**显式**重跑开关：那类任务的副作用
    可能已经发生了一半，重跑必须是用户的决定，不能是顺手再点一次。
    """
    host = await _ensure_work_host()
    try:
        return await host.retry(work_item_id, _work_scope(account_id, conversation_id), confirm_unknown=confirm_unknown)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="work task not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/work/approvals/{approval_id}", deprecated=True)
async def work_approval_decide(approval_id: str, request: WorkApprovalRequest, account_id: str = "local-user", conversation_id: str = "web") -> dict[str, Any]:
    """**迁移期代理**：老的 Work 审批决策，转发到统一接口。

    只做一件事：把老词表翻成新词表。`approved` → `confirmed`，
    `rejected` 原样。`expired` **不接受** —— 让外界能把一条审批标成"过期"
    等于给了一条"绕过拒绝"的路；过期由账本自己按时间判。
    """
    coordinator = await _approval_coordinator()
    if request.decision == "approved":
        decision = DECISION_CONFIRMED
    elif request.decision == "rejected":
        decision = DECISION_REJECTED
    else:
        raise HTTPException(
            status_code=400,
            detail="decision 'expired' cannot be submitted by a caller; it is decided by the work ledger",
        )
    try:
        return await coordinator.decide(approval_id, _work_scope(account_id, conversation_id), decision)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="approval or work task not found") from exc


@app.get("/api/work/tasks/{work_item_id}/events")
async def work_task_event_history(
    work_item_id: str,
    attempt_id: str | None = None,
    after_sequence: int = 0,
    account_id: str = "local-user",
    conversation_id: str = "web",
) -> dict[str, Any]:
    if after_sequence < 0:
        raise HTTPException(status_code=422, detail="after_sequence must be non-negative")
    host = await _ensure_work_host()
    scope = _work_scope(account_id, conversation_id)
    try:
        detail = host.get(work_item_id, scope)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="work task not found") from exc
    attempts = {attempt["attempt_id"] for attempt in detail["attempts"]}
    if attempt_id is None:
        events = [event for event in detail["events"] if event["sequence"] > after_sequence]
    else:
        if attempt_id not in attempts:
            raise HTTPException(status_code=404, detail="attempt not found")
        events = [event for event in detail["events"] if event["attempt_id"] == attempt_id and event["sequence"] > after_sequence]
    return {"work_item_id": work_item_id, "attempt_id": attempt_id, "after_sequence": after_sequence, "events": events}


@app.get("/api/work/tasks/{work_item_id}/artifacts/{artifact_id}")
async def work_artifact_download(work_item_id: str, artifact_id: str, account_id: str = "local-user", conversation_id: str = "web"):
    host = await _ensure_work_host()
    scope = _work_scope(account_id, conversation_id)
    try:
        detail = host.get(work_item_id, scope)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="work task not found") from exc
    artifact = host.ledger.get_artifact(artifact_id, scope)
    if artifact is None or artifact.attempt_id not in {item["attempt_id"] for item in detail["attempts"]}:
        raise HTTPException(status_code=404, detail="artifact not found")
    try:
        expires = datetime.fromisoformat(artifact.expires_at)
    except ValueError:
        raise HTTPException(status_code=404, detail="artifact expired")
    if expires <= datetime.now(timezone.utc) or not Path(artifact.path).is_file():
        raise HTTPException(status_code=404, detail="artifact expired")
    return FileResponse(artifact.path, media_type=artifact.media_type, filename=f"{artifact.artifact_id}.bin")


@app.post("/api/work/plans", status_code=202)
async def work_plan_submit(request: WorkPlanRequest) -> dict[str, Any]:
    """Accept a candidate plan. Only a Host-validated plan is ever persisted."""
    host = await _ensure_work_host()
    try:
        return await host.submit_plan(_work_scope(request.account_id, request.conversation_id), request.objective, request.steps)
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/work/plans")
async def work_plan_list(account_id: str = "local-user", conversation_id: str = "web") -> dict[str, Any]:
    host = await _ensure_work_host()
    return {"plans": host.list_plans(_work_scope(account_id, conversation_id))}


@app.get("/api/work/plans/{plan_id}")
async def work_plan_detail(plan_id: str, account_id: str = "local-user", conversation_id: str = "web") -> dict[str, Any]:
    host = await _ensure_work_host()
    try:
        return host.get_plan(plan_id, _work_scope(account_id, conversation_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="work plan not found") from exc


@app.post("/api/work/plans/{plan_id}/cancel")
async def work_plan_cancel(plan_id: str, account_id: str = "local-user", conversation_id: str = "web") -> dict[str, Any]:
    host = await _ensure_work_host()
    try:
        return await host.cancel_plan(plan_id, _work_scope(account_id, conversation_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="work plan not found") from exc


@app.post("/api/work/plans/{plan_id}/retry", status_code=202)
async def work_plan_retry(plan_id: str, account_id: str = "local-user", conversation_id: str = "web") -> dict[str, Any]:
    host = await _ensure_work_host()
    try:
        return await host.retry_plan(plan_id, _work_scope(account_id, conversation_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="work plan not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/work/plans/{plan_id}/events")
async def work_plan_event_history(
    plan_id: str,
    after_sequence: int = 0,
    account_id: str = "local-user",
    conversation_id: str = "web",
) -> dict[str, Any]:
    if after_sequence < 0:
        raise HTTPException(status_code=422, detail="after_sequence must be non-negative")
    host = await _ensure_work_host()
    try:
        events = host.plan_events(plan_id, _work_scope(account_id, conversation_id), after_sequence=after_sequence)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="work plan not found") from exc
    return {"plan_id": plan_id, "after_sequence": after_sequence, "events": events}


@app.get("/api/work/plan-events")
async def work_plan_events(account_id: str = "local-user", conversation_id: str = "web"):
    """Plan-scoped SSE. Events are persisted first; this only hints a refresh."""
    host = await _ensure_work_host()
    scope = _work_scope(account_id, conversation_id)

    async def stream():
        yield "event: connected\ndata: {}\n\n"
        async for event in host.events():
            plan_id = event.get("plan_id")
            if not plan_id:
                continue
            try:
                if host.ledger.get_plan(plan_id, scope) is None:
                    continue
            except Exception:  # pragma: no cover - defensive scope guard
                continue
            yield f"event: plan.{event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})


@app.get("/api/work/task-events")
async def work_task_events(account_id: str = "local-user", conversation_id: str = "web"):
    host = await _ensure_work_host()
    scope = _work_scope(account_id, conversation_id)
    async def stream():
        yield "event: connected\ndata: {}\n\n"
        async for event in host.events():
            if event.get("plan_id"):
                # Plan events belong to /api/work/plan-events so the work page can
                # refresh the plan view without re-reading every task.
                continue
            try:
                detail = host.get(event["work_item_id"], scope)
            except KeyError:
                continue
            yield f"event: work.{event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})


@app.post("/api/chat/attachments")
async def chat_attachment_upload(
    file: UploadFile = File(...),
    account_id: str = Form("local-user"),
    conversation_id: str = Form("web"),
) -> dict[str, Any]:
    # Reuse ChatRequest's identifier validation without making the upload API
    # duplicate the web scope rules.
    try:
        validated = ChatRequest(message="attachment", account_id=account_id, conversation_id=conversation_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    store = _chat_attachments()
    media_type = str(file.content_type or "").lower().strip()
    if media_type not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
        raise HTTPException(status_code=415, detail="unsupported image media type")
    content = await file.read(store.max_bytes + 1)
    if len(content) > store.max_bytes:
        raise HTTPException(status_code=413, detail=f"image file exceeds {store.max_bytes} bytes")
    try:
        attachment = store.upload(
            build_web_scope(validated.account_id, validated.conversation_id),
            file.filename or "image",
            media_type,
            content,
        )
    except InvalidImageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "attachment_id": attachment.attachment_id,
        "media_type": attachment.media_type,
        "size_bytes": attachment.size_bytes,
        "expires_at": datetime.fromtimestamp(attachment.expires_at, timezone.utc).isoformat(),
    }


def _main_chat_practical_enabled() -> bool:
    """主聊天三项实用能力开关；关闭时下面所有新路径都不生效。"""
    return main_chat_practical_enabled()


def _open_clarification_turn(scope: Scope, *, turn_id: str) -> Any:
    """打开本轮的澄清登记对象，并把它挂进本轮关键背景。

    返回 ``(session, background_payload, turn)``；能力关闭或没有候选时返回 ``None``
    —— 关闭时原路径完全不变。
    """
    if not _main_chat_practical_enabled() or _memory_clarification_store is None:
        return None
    try:
        scope_key = _clarification_scope_key(scope)
        candidates = _clarification_candidates(scope)
        turn = ClarificationTurn(
            _memory_clarification_store, scope_key=scope_key,
            turn_id=turn_id, candidates=candidates,
        )
        background = None
        if candidates:
            # 只给主模型看"可能相关"的候选（服务端已按范围筛过），选不选由它决定。
            background = {
                "version": 1,
                "user_text": "",
                "selected": [],
                "quotes": [],
                "pending": [item.to_background() for item in candidates],
                "gaps": [],
                "notes": [],
            }
        return (scope, background, turn)
    except Exception:  # noqa: BLE001 - 打开澄清不影响本轮回答
        logger.exception("打开澄清回合失败")
        return None


def _register_clarification_tool(session: Any, turn: Any) -> None:
    """把 ``memory__propose_clarification`` 注册进工具表（**只注册一次**）。

    工具名是常量、注册表不允许重名，所以 handler 不闭包某一个回合，而是按当前
    正在进行的回合去查登记表 —— 否则第二轮注册会直接抛错。
    走现有 ToolRegistry 与网关；只登记本轮提问草稿，不改记忆、不表示已发布。
    """
    registry = _tool_registry
    if registry is None:
        return
    if registry.resolve(PROPOSE_TOOL_NAME) is not None:
        return
    try:
        spec = build_propose_tool_spec(_ClarificationToolRouter())
        registry.register(spec)
    except Exception:  # noqa: BLE001 - 注册失败时模型看不到这个工具，其余照常
        logger.exception("注册澄清提问工具失败")


class _ClarificationToolRouter:
    """把工具调用路由到"当前正在进行的那个回合"的登记对象。

    服务端只认自己登记的回合：没有进行中的回合时明确失败，不做任何猜测。
    """

    def propose(self, candidate_id: str, question: str) -> dict[str, Any]:
        for turn_id in reversed(list(_clarification_drafts)):
            turn = _clarification_drafts.get(turn_id)
            if turn is None:
                continue
            if turn.draft.active:
                # 本轮已经有草稿：交给它统一答复（同轮最多接受一条）。
                return turn.propose(candidate_id, question)
        # 还没有草稿：路由到最近登记的回合，由它登记第一条。
        for turn_id in reversed(list(_clarification_drafts)):
            turn = _clarification_drafts.get(turn_id)
            if turn is not None:
                return turn.propose(candidate_id, question)
        return {"status": "error", "error": "当前没有进行中的澄清回合"}


def _clarification_scope_key(scope: Scope) -> str:
    """疑点范围键：账号/渠道/模块/会话。与候选作业的 scope_key 口径一致。"""
    return "|".join([
        str(getattr(scope, "owner_id", "") or ""),
        str(getattr(scope, "channel", "") or ""),
        str(getattr(scope, "conversation_id", "") or ""),
        str(getattr(scope, "module_id", "") or ""),
    ])


def _clarification_candidates(scope: Scope) -> list[Any]:
    """当前范围内可供追问的疑点候选（每轮最多三项）。"""
    store = _memory_clarification_store
    if store is None:
        return []
    try:
        return store.candidates(scope_key=_clarification_scope_key(scope))
    except Exception:  # noqa: BLE001 - 读疑点失败不影响本轮回答
        logger.exception("读取澄清候选失败")
        return []


def _record_published_clarification(
    request: ChatRequest, handle: Any, reply_text: str, outcome: Any,
) -> None:
    """回合成功完成后，按**实际发布正文**记录本轮问题是否真的问了。

    这是本版的记录边界：只在最终发布的答案确实包含该问题、且回合成功完成时记 asked。
    不做"用户设备已读"回执，也不在后台生成问题时提前记账。
    """
    turn = _clarification_drafts.get(str(getattr(handle, "turn_id", "") or ""))
    if turn is None:
        return
    try:
        asked = turn.record_published(published_text=reply_text)
        if asked:
            logger.info("澄清问题已发布并记账 %s", json.dumps(
                [item.candidate_id for item in asked], ensure_ascii=False))
    except Exception:  # noqa: BLE001 - 记账失败不影响回答
        logger.exception("记录已发布澄清失败")
    finally:
        # 草稿只存活一轮：下轮重新读候选，避免把上一轮的草稿当成还在进行中的提问。
        _clarification_drafts.pop(str(getattr(handle, "turn_id", "") or ""), None)


def _attach_clarification_context(scope: Scope, user_text: str, turn_id: str, packet: Any) -> Any:
    """把下一轮后台处理包补上可选 ``clarification_context``。

    服务端填写关联（候选编号、原作业与事项路径、实际问题及提问回合、原始来源、
    目标记录与版本）；**不接受客户端自报的关联及原文**。程序只提供"可能关联"，
    是否真的回答了问题仍由后台模型判断 —— 所以这里绝不自动确认。
    """
    store = _memory_clarification_store
    if store is None or packet is None:
        return packet
    try:
        candidates = _clarification_candidates(scope)
        context = build_clarification_context(
            store, scope_key=_clarification_scope_key(scope),
            user_text=user_text, turn_id=turn_id, candidates=candidates,
        )
    except Exception:  # noqa: BLE001
        logger.exception("构建澄清关联失败")
        return packet
    if not context:
        return packet
    payload = packet.as_payload()
    payload["clarification_context"] = context
    return MemoryTurnPacket.from_payload(payload)


async def _finish_chat_turn(
    request: ChatRequest,
    handle: Any,
    claimed_attachment: Any,
    attachment_store: ChatAttachmentStore | None,
) -> None:
    """Complete post-processing after the HTTP submit response has returned."""
    try:
        outcome = await handle.result
        if claimed_attachment is not None and attachment_store is not None:
            if outcome.status == "completed":
                attachment_store.delete(_scope(request), claimed_attachment.attachment_id)
            else:
                attachment_store.release(_scope(request), claimed_attachment.attachment_id)
        reply_text = clean_character_reply(outcome.reply)
        dialogue_history = [
            item for item in (manager.history(_scope(request)) if manager is not None else [])
            if item.get("role") != "system"
        ]
        finished = request.message.strip() and outcome.status == "completed" and reply_text
        # 一个回合只拼一次处理包：本地提炼队列和云端上传队列用**同一份**，
        # 各拼一次迟早会在字段上分叉，然后两端看到的就不是同一轮对话。
        packet = None
        needs_packet = finished and (
            _memory_candidate_queue is not None
            or (_memory_sync_store is not None and cloud_sync_enabled())
        )
        if needs_packet:
            packet = turn_from_messages(
                turn_id=handle.turn_id,
                occurred_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                scope=_scope(request),
                user_text=request.message.strip(),
                published_reply=reply_text,
                task_goal=str(getattr(outcome, "task_goal", "") or ""),
                relevant_history=dialogue_history,
                handoff=getattr(outcome, "handoff", None),
            )
            # 实施工作 B：同会话有未解决问题时，把"可能关联"随处理包交给现有后台代理。
            # 程序只提供可能关联，是否真的回答了问题由模型判断 —— 这里绝不自动确认。
            if _main_chat_practical_enabled():
                packet = _attach_clarification_context(
                    _scope(request), request.message.strip(), handle.turn_id, packet,
                )
        # 实施工作 B：澄清闭环的记账与下一轮关联。
        # 记账边界是**服务端发布和持久化终态**：只有"最终确实发布的答案包含该问题、
        # 且回合成功完成"时才记 asked；取消、超时、草稿、核查删掉的问题都不记。
        if finished and _main_chat_practical_enabled():
            _record_published_clarification(request, handle, reply_text, outcome)
        # 一个回合**只走一个处理端**：新版候选队列已经装配时，它就是本回合的处理端，
        # 云端上传不再接手 —— 否则同一个已发布回合会同时产生一条本地候选作业和一次
        # 云端上传，两端各提炼一遍，记下的是两套相近但不相同的记录（B-R4）。
        if (
            finished
            and _memory_sync_store is not None
            and cloud_sync_enabled()
            and _memory_candidate_queue is None
        ):
            # 云端模式下：本地只把**这一轮原始对话**放进待上传队列，不自己提炼。
            # 入队是本地事务、毫秒级，真正的网络请求在后台同步任务里做 ——
            # 聊天不能等同步完成，否则断网时每句话都要卡住。
            try:
                await asyncio.to_thread(
                    _memory_sync_store.enqueue_turn,
                    _scope(request),
                    turn_id=handle.turn_id,
                    user_text=request.message.strip(),
                    assistant_reply=reply_text,
                    occurred_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                    packet=packet.as_payload() if packet is not None else None,
                    schema_version=packet.schema_version if packet is not None else 0,
                )
            except Exception:
                logger.exception("待上传队列入队失败", extra={"turn_id": handle.turn_id})
        if finished and _memory_candidate_queue is not None and packet is not None:
            # 新版语义记忆：只把**已发布回合**的处理包放进持久队列，提炼在后台跑。
            # 这里只入队、不调模型 —— 正文发布不等提炼，断网或模型报错也不影响回答。
            try:
                await asyncio.to_thread(_memory_candidate_queue.enqueue, packet)
            except Exception:
                logger.exception("候选记忆入队失败", extra={"turn_id": handle.turn_id})
        if finished and _memory_extractor is not None and _memory_writer is not None:
            task = asyncio.create_task(extract_and_store_memory_turn(
                _memory_extractor, _memory_writer, _scope(request), request.message.strip(), reply_text,
                history=dialogue_history, turn_id=handle.turn_id, run_id=outcome.run_id or "",
            ))
            _track_memory_postprocess_task(task)
    except Exception:
        logger.exception("background chat post-processing failed", extra={"turn_id": handle.turn_id})


async def _run_worker_chat(scope: Scope, turn_id: str, command: str, user_text: str) -> None:
    try:
        if conversation_repository is not None:
            conversation_repository.append_message(
                scope, {"role": "user", "content": user_text}, stable_key=f"turn:{turn_id}:0",
            )
        result = await run_readonly_command(command, account_id=scope.owner_id, timeout_seconds=60.0)
        if result["stdout"].strip() and not result["stderr"].strip():
            reply = f"已通过 worker 完成文件系统查询。结果：\n{result['stdout'].strip()}"
            status = "completed"
        elif result["status"] == "timeout":
            reply, status = "worker 文件系统查询超时，尚未得到可靠结果。", "failed"
        else:
            reply = f"worker 文件系统查询失败：{result['stderr'].strip() or '未知错误'}"
            status = "failed"
        if conversation_repository is not None:
            conversation_repository.append_message(
                scope, {"role": "assistant", "content": reply}, stable_key=f"turn:{turn_id}:1",
            )
        if event_bus is None:
            return
        await event_bus.emit(Event(EventType.CHAT_DELTA, datetime.now(timezone.utc), asdict(scope), {
            "turn_id": turn_id, "seq": 1, "delta": reply,
        }))
        terminal = EventType.CHAT_COMPLETED if status == "completed" else EventType.CHAT_FAILED
        await event_bus.emit(Event(terminal, datetime.now(timezone.utc), asdict(scope), {
            "turn_id": turn_id, "status": status, "reply": reply,
            **({"error_code": "worker_failed", "message": reply, "retryable": True} if status == "failed" else {}),
        }))
        expression, reason = expression_for_event(terminal.value, {"reply": reply})
        await event_bus.emit(Event(EventType.PORTRAIT_CHANGED, datetime.now(timezone.utc), asdict(scope), {
            "turn_id": turn_id, "expression": expression, "portrait_key": expression,
            "phase": "reply" if status == "completed" else "failure", "reason": reason,
        }))
    except Exception as exc:
        logger.exception("worker chat failed", extra={"turn_id": turn_id})
        if event_bus is not None:
            await event_bus.emit(Event(EventType.CHAT_FAILED, datetime.now(timezone.utc), asdict(scope), {
                "turn_id": turn_id, "status": "failed", "error_code": type(exc).__name__,
                "error": str(exc), "message": "文件系统查询失败，请稍后重试。", "retryable": True,
            }))


@app.post("/api/chat", status_code=202)
@track_request("/api/chat")
async def chat(request: ChatRequest) -> JSONResponse:
    """
    聊天接口 - 处理用户消息并返回 AI 回复

    已改进：
    - 添加输入验证（在 Pydantic 模型中）
    - 添加详细日志记录
    - 改进错误处理
    """
    if not request.message.strip() and not request.attachment_id:
        raise HTTPException(status_code=422, detail="message or attachment_id is required")

    attachment_store: ChatAttachmentStore | None = None
    claimed_attachment = None
    content_parts: list[dict[str, Any]] = []
    attachment_metadata: list[dict[str, Any]] = []
    if request.attachment_id:
        attachment_store = _chat_attachments()
        try:
            claimed_attachment = attachment_store.claim(_scope(request), request.attachment_id)
        except AttachmentUnavailable as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            encoded = base64.b64encode(claimed_attachment.path.read_bytes()).decode("ascii")
        except OSError as exc:
            attachment_store.release(_scope(request), request.attachment_id)
            raise HTTPException(status_code=410, detail="attachment file is no longer available") from exc
        if request.message.strip():
            content_parts.append({"type": "text", "text": request.message.strip()})
        # The provider expects a data URI, while public history gets only this
        # safe metadata object.
        content_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{claimed_attachment.media_type};base64,{encoded}"},
        })
        attachment_metadata = [{
            "attachment_id": claimed_attachment.attachment_id,
            "media_type": claimed_attachment.media_type,
            "size_bytes": claimed_attachment.size_bytes,
        }]

    logger.info(
        f"Received chat request",
        extra={
            "account_id": request.account_id,
            "conversation_id": request.conversation_id,
            "message_length": len(request.message)
        }
    )

    try:
        # 实施工作 B：本轮查一次候选疑点，登记草稿对象与内部工具。
        # 候选按服务端范围校验；未解决且已问过的事项不会重复问。
        clarification_turn = _open_clarification_turn(_scope(request), turn_id="")
        _initialize_runtime()
        await _start_task_services()
        await _start_memory_workers()
        handle = await manager.submit(
            AgentRequest(
                request.message.strip(),
                scope=_scope(request),
                system_prompt=ARcueid_PERSONA,
                persona_override="\n\n".join(part for part in (
                    ARcueid_PERSONA, _runtime_time_context(), _today_user_event_context(_scope(request)),
                ) if part),
                content_parts=content_parts,
                attachment_metadata=attachment_metadata,
                turn_background=clarification_turn[1] if clarification_turn else None,
            )
        )
        if clarification_turn is not None:
            # 回合号只有 submit 之后才有，登记表按它索引。
            session, _background, turn = clarification_turn
            _clarification_drafts[handle.turn_id] = turn
            _register_clarification_tool(session, turn)
        if not handle.accepted:
            if claimed_attachment is not None and attachment_store is not None:
                attachment_store.release(_scope(request), claimed_attachment.attachment_id)
            raise HTTPException(status_code=409, detail=handle.reason or "conversation queue rejected the turn")
        _track_memory_postprocess_task(asyncio.create_task(
            _finish_chat_turn(request, handle, claimed_attachment, attachment_store)
        ))
        return JSONResponse(status_code=202, content={
            "status": "accepted", "turn_id": handle.turn_id, "position": handle.position,
        })
    except HTTPException:
        raise
    except Exception as exc:
        if claimed_attachment is not None and attachment_store is not None:
            attachment_store.release(_scope(request), claimed_attachment.attachment_id)
        if manager is None:
            logger.exception("chat runtime initialization failed")
            return {
                "status": "failed",
                "reply": "对话核心尚未就绪，请检查系统能力诊断。",
                "error": "runtime_initialization_failed",
            }
        # 记录错误日志
        logger.error(
            f"Chat request failed: {type(exc).__name__}: {exc}",
            extra={
                "account_id": request.account_id,
                "conversation_id": request.conversation_id,
                "error_type": type(exc).__name__
            },
            exc_info=True
        )
        payload = {
            "status": "failed",
            "reply": f"暂时无法连接到对话核心：{type(exc).__name__}: {exc}",
            "error": str(exc),
        }
        lowered_error = str(exc).lower()
        if request.attachment_id and any(term in lowered_error for term in ("vision", "image", "multimodal", "content type")):
            payload["error_code"] = "vision_not_supported"
            payload["retryable"] = True
        return payload


@app.post("/api/conversations", status_code=201)
async def create_conversation(request: ConversationCreateRequest) -> dict[str, Any]:
    repository = _conversation_repository()
    conversation_id = f"web-{uuid.uuid4().hex}"
    scope = _web_scope_from_path(request.account_id, conversation_id)
    record = repository.create(scope, title=request.title)
    return {"conversation": record.as_dict()}


@app.get("/api/conversations")
async def list_conversations(
    account_id: str = "local-user",
    status: str = "active",
    limit: int = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    if status not in {"active", "archived"}:
        raise HTTPException(status_code=422, detail="status must be active or archived")
    _web_scope_from_path(account_id, "web")
    try:
        records, next_cursor = _conversation_repository().list(
            owner_id=account_id,
            channel="web",
            module_id="companion",
            status=status,
            limit=limit,
            cursor=cursor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"conversations": [record.as_dict() for record in records], "next_cursor": next_cursor}


def _existing_conversation(account_id: str, conversation_id: str):
    scope = _web_scope_from_path(account_id, conversation_id)
    record = _conversation_repository().get(scope)
    if record is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return scope, record


@app.post("/api/conversations/{conversation_id}/archive")
async def archive_conversation(conversation_id: str, account_id: str = "local-user") -> dict[str, Any]:
    scope, record = _existing_conversation(account_id, conversation_id)
    if _conversation_in_use(scope):
        raise HTTPException(status_code=409, detail="conversation is running, queued, or waiting for confirmation")
    if record.status != "active" or not _conversation_repository().archive(scope):
        raise HTTPException(status_code=409, detail="conversation is already archived")
    archived = _conversation_repository().get(scope)
    assert archived is not None
    return {"conversation": archived.as_dict()}


@app.post("/api/conversations/{conversation_id}/restore")
async def restore_conversation(conversation_id: str, account_id: str = "local-user") -> dict[str, Any]:
    scope, record = _existing_conversation(account_id, conversation_id)
    if record.status != "archived" or not _conversation_repository().restore(scope):
        raise HTTPException(status_code=409, detail="conversation is already active")
    restored = _conversation_repository().get(scope)
    assert restored is not None
    return {"conversation": restored.as_dict()}


@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str, account_id: str = "local-user") -> dict[str, Any]:
    scope, _ = _existing_conversation(account_id, conversation_id)
    if _conversation_in_use(scope):
        raise HTTPException(status_code=409, detail="conversation is running, queued, or waiting for confirmation")
    if not _conversation_repository().delete(scope):
        raise HTTPException(status_code=404, detail="conversation not found")
    # 库里的行没了，进程里的三份残留还得各自收：意图状态（含 SQLite 读缓存）、
    # 运行态历史缓存、以及这条会话的进程内锁。漏掉任何一个，
    # 下一次同名会话都会带着上一段的"正在追问"继续跑。
    if manager is not None:
        await manager.forget(scope)
    # 这里**不再**清位置。位置是账号 + 渠道级的（见 LocationStore._key），
    # 删掉一个会话不该把账号的位置一起删掉 —— 那会让用户在新会话里
    # 突然被要求重新授权，而他从没关过共享。
    # 真正该删位置的只有一处：用户在界面上主动关闭位置共享（DELETE /api/location）。
    return {"status": "ok"}


@app.get("/api/conversations/{conversation_id}/history")
async def history(conversation_id: str, account_id: str = "local-user") -> dict[str, Any]:
    _initialize_runtime()
    messages = manager.history(_web_scope_from_path(account_id, conversation_id))
    return {"messages": [
        {**item, "content": clean_character_reply(item.get("content", ""))}
        if item.get("role") == "assistant" else item
        for item in messages
    ]}


def _background_activity(account_id: str, conversation_id: str) -> dict[str, Any]:
    """这个对话名下还有没有活跃的后台任务（含计划）。

    拿不到就当作"没有活跃任务"：舞台宁可少显示一句"我还在处理"，
    也不能因为后台服务抖动把整个 presentation 请求打挂 —— 那样用户
    连已经读过的历史都看不到了。
    """
    host = work_host
    if host is None:
        return {"active": False, "active_count": 0}
    scope = _work_scope(account_id, conversation_id)
    try:
        items = host.list(scope, limit=50)
    except Exception:
        logger.debug("work list unavailable for presentation", exc_info=True)
        return {"active": False, "active_count": 0}
    plans: list[dict[str, Any]] = []
    try:
        plans = host.list_plans(scope, limit=50)
    except Exception:
        logger.debug("work plan list unavailable for presentation", exc_info=True)
    return background_summary(items, plans=plans)


def _presentation_snapshot(scope: Scope, conversation_id: str) -> dict[str, Any]:
    """组装一次舞台快照。GET / POST advance 共用同一口径。

    推进之后直接回一份新快照，而不是让前端再发一次 GET：两次请求之间
    若有新消息落库，会出现"确认了 A，结果又看到 A"的闪烁。
    """
    repository = _conversation_repository()
    messages = manager.history(scope) if manager is not None else repository.history(scope)
    # 与 /history 同一套清洗：舞台不该显示「（微笑）」这类舞台指示。
    messages = [
        {**item, "content": clean_character_reply(item.get("content", ""))}
        if item.get("role") == "assistant" else item
        for item in messages
    ]
    record = repository.get(scope)
    snapshot = project_presentation(
        messages,
        acknowledged_message_id=repository.presentation_state(scope),
        runtime_state=record.runtime_state if record is not None else "idle",
        background=_background_activity(scope.owner_id, conversation_id),
    )
    return {"conversation_id": conversation_id, **snapshot.as_dict()}


@app.get("/api/conversations/{conversation_id}/presentation")
async def conversation_presentation(conversation_id: str, account_id: str = "local-user") -> dict[str, Any]:
    """舞台播放快照：该显示的消息、已读游标、对话运行状态与后台任务状态。

    "会话还不存在"不是错误：新对话在发出第一条消息之前没有账本行，
    前端挂载时照样需要一份空快照来渲染问候语。
    """
    _initialize_runtime()
    scope = _web_scope_from_path(account_id, conversation_id)
    return _presentation_snapshot(scope, conversation_id)


@app.post("/api/conversations/{conversation_id}/presentation/advance")
async def advance_conversation_presentation(
    conversation_id: str, request: PresentationAdvanceRequest
) -> dict[str, Any]:
    """确认**队首**那条助手消息已播放完（第二次点击），返回推进后的新快照。

    三种结果分别由状态码表达：

    - ``200``：这条就是当前未读的那一条，游标前进；或者它已经被确认过
      （重复点击、乱序确认），幂等返回快照。
    - ``409``：想跳过中间的未读消息。不改任何状态，并把"该确认的是哪一条"
      一起返回，前端据此对齐，而不是让用户自己发现少读了一句。
    - ``422``：``message_id`` 不是正整数（请求体校验，Pydantic 直接拦下）。

    用户消息、别的会话的 id、已被裁剪的历史都走 200 幂等分支 —— 前端拿不到
    这些 id，出现它们只可能是重复帧，不该当成错误处理。
    """
    _initialize_runtime()
    scope = _web_scope_from_path(request.account_id, conversation_id)
    try:
        outcome = _conversation_repository().advance_presentation(scope, request.message_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if outcome.status == ADVANCE_BLOCKED:
        # 静默成功是更糟的选择：前端会以为自己的播放位置对了，实际上账本
        # 停在更前面 —— 那种错位要等下次刷新才暴露，用户已经漏读了一句。
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "not_head_of_queue",
                "expected_message_id": outcome.expected_message_id,
                "acknowledged_message_id": outcome.acknowledged_message_id,
            },
        )
    return _presentation_snapshot(scope, conversation_id)


def _resolve_opening_recall_store() -> OpeningRecallStore:
    """联想台账。库路径优先取对话仓储的实际路径，保证与对话**同库**。

    取仓储的 ``.path`` 而不是重读 ``AGENT_CORE_DB``：那个环境变量在
    ``_initialize_runtime`` 里已经被解析过一次，再读一遍等于给它第二个真相来源。
    """
    global _opening_recall_store
    if _opening_recall_store is None:
        path = getattr(conversation_repository, "path", None) or os.getenv("AGENT_CORE_DB", "agent_core.db")
        _opening_recall_store = OpeningRecallStore(path)
    return _opening_recall_store


def _resolve_opening_greeting_service() -> OpeningGreetingService:
    """开场白服务：与主链路共用同一个 provider 与人设，但**不共用任何写路径**。

    ``forbidden_terms`` 注入 registry 里的真实工具名 —— 模型一旦把工具名写进
    开场白（"我用 xx 帮你看看"），那是在问候语里对用户暴露内部结构，判不合格。

    台账建不起来（磁盘、权限）不让接口跟着失败：服务拿到 ``recall_store=None``
    会自动降级成"只生成普通问候"（见 ``OpeningGreetingService._recall_plan``），
    开场白仍然有，只是这次不联想。
    """
    global _opening_greeting_service
    if _opening_greeting_service is None:
        try:
            recall_store: OpeningRecallStore | None = _resolve_opening_recall_store()
        except Exception:
            logger.warning("开场白：联想台账不可用，降级为普通问候", exc_info=True)
            recall_store = None
        # 用 ``worker_provider`` 而不是 ``provider``：后者只是 _initialize_runtime 的
        # **局部变量**，模块级从来没有这个名字 —— 写成 provider 会在每次请求构造
        # 服务时抛 NameError（500），而它比"模型失败"更隐蔽：兜底句照常出现，
        # 只是永远不可能是真话。两者指向同一个对象（`worker_provider = provider`）。
        _opening_greeting_service = OpeningGreetingService(
            worker_provider,
            conversation_repository=conversation_repository,
            repositories=_repositories,
            recall_store=recall_store,
            persona=ARcueid_PERSONA,
            forbidden_terms=tuple(spec.name for spec in _tool_registry) if _tool_registry is not None else (),
            prepared_plain=prepared_greeting,
            opening_gateway=_opening_gateway,
            location_store=_location_store,
        )
    return _opening_greeting_service


def _resolve_opening_greeting_cache() -> OpeningGreetingCache:
    global _opening_greeting_cache
    if _opening_greeting_cache is None:
        _opening_greeting_cache = OpeningGreetingCache()
    return _opening_greeting_cache


@app.post("/api/conversations/{conversation_id}/opening-greeting")
async def conversation_opening_greeting(
    conversation_id: str, request: OpeningGreetingRequest
) -> dict[str, Any]:
    """进入会话时生成一句开场白。**一次进入只调用一次模型。**

    刻意不放进 ``presentation``：舞台快照会被前端轮询、被 SSE 事件反复拉动，
    模型调用放进去等于每次刷新都花一次 Token，还会把一个纯函数投影变成
    有延迟、会超时、会失败的接口。

    问候不写聊天账本、不写记忆库：它不是角色说过的话，而是"她此刻看到你进来"
    这一幕的过场。刷新页面 = 新的一次进入 = 新的一份问候。

    永远 200 + 一句能说出口的话：模型失败、超时、输出不合格都由服务层收成
    固定兜底 —— 开场白没有"加载失败"这个展示位。

    **刻意不传 ``manager.history()``**：那是内存态的会话历史，里面的消息可能
    还没落库、因而没有 ``created_at``；而线索必须有时间才能算"多久以前"、
    才能做 14 天窗口。拿它当素材来源的结果是"最新那几句话反而全部作废"——
    越新的消息越容易还在内存里，于是开场白永远只看得见旧话题。
    统一由服务层走 ``recent_user_messages()`` 从数据库限量读。
    """
    _initialize_runtime()
    scope = _web_scope_from_path(request.account_id, conversation_id)
    # 幂等键**必须**带 account_id 与 conversation_id：同一个 visit_id 落在两个
    # 账号（或两个会话）下是两份问候，少任何一个键都会串台。
    key = "|".join((scope.owner_id, scope.channel, scope.conversation_id, request.visit_id))
    service = _resolve_opening_greeting_service()
    greeting = await _resolve_opening_greeting_cache().run(
        key,
        lambda: service.generate(
            scope,
            visit_id=request.visit_id,
            absence_tier=request.absence_tier,
        ),
    )
    if greeting == DEFAULT_GREETING:
        greeting = prepared_greeting(request.visit_id)
    return {
        "conversation_id": conversation_id,
        "visit_id": request.visit_id,
        "greeting": greeting,
    }


def _public_event_data(event_type: EventType, data: dict[str, Any]) -> dict[str, Any]:
    """Remove server diagnostics before an event crosses the browser boundary."""
    public = dict(data or {})
    if event_type in {EventType.CHAT_FAILED, EventType.TURN_FAILED}:
        return {
            "turn_id": str(public.get("turn_id") or ""),
            "status": "failed",
            "error_code": "turn_failed",
            "message": "回复生成失败，请稍后重试。",
            "retryable": bool(public.get("retryable", True)),
        }
    # 注意：这里**没有** task.completed / task.failed / task.cancelled 三个分支。
    # 它们不再被 SSE 订阅（见 _task_event_stream），是纯内部业务事件：
    # data 里带 error / result 这类诊断，不该跨过浏览器边界。
    # 前端改看 task.result —— 那是回执桥写库成功后发的刷新通知，
    # 只有 task_id / run_id / status / result_code / history_written 五个字段。
    if event_type is EventType.TASK_RESULT:
        return {
            "task_id": public.get("task_id"),
            "run_id": str(public.get("run_id") or ""),
            "status": str(public.get("status") or ""),
            "result_code": str(public.get("result_code") or ""),
            "history_written": bool(public.get("history_written", False)),
        }
    if event_type in {EventType.TOOL_STARTED, EventType.TOOL_COMPLETED, EventType.TOOL_FAILED}:
        # 白名单而不是黑名单。这条以前是原样透传（落到最后的 `return public`），
        # 而模型路径的 tool.started 里**带着 arguments** —— 位置类工具的参数就是坐标。
        # 后来位置工具不吐坐标了，但"碰巧安全"不算防线。前端真正需要的只有
        # "哪个工具、开始了没有、成没成"，所以别的字段一个都不放行。
        return {
            "tool_call_id": str(public.get("tool_call_id") or ""),
            "tool_name": str(public.get("tool_name") or ""),
            "status": str(public.get("status") or ""),
            "error_kind": str(public.get("error_kind") or ""),
        }
    if event_type is EventType.STEWARD_PROGRESS:
        # 白名单而不是黑名单：播报的 data 由管家内部拼装，将来加什么字段
        # 不该由"这里有没有想到要屏蔽"来决定能不能进浏览器。
        return {
            "turn_id": str(public.get("turn_id") or ""),
            "run_id": str(public.get("run_id") or ""),
            "plan_id": str(public.get("plan_id") or ""),
            "step_id": str(public.get("step_id") or ""),
            "phase": str(public.get("phase") or ""),
            "content": str(public.get("content") or ""),
            "kind": str(public.get("kind") or ""),
            "dedupe_key": str(public.get("dedupe_key") or ""),
        }
    return public


async def _task_event_stream(account_id: str, conversation_id: str, *, heartbeat_seconds: float = 20.0):
    """Yield turn and task lifecycle events for one authoritative Web Scope."""
    if event_bus is None:
        _initialize_runtime()
    if event_bus is None:
        return
    await _start_task_services()
    queue: asyncio.Queue[Any] = asyncio.Queue()

    def enqueue(event: Any) -> None:
        queue.put_nowait(event)

    filters = {
        "owner_id": account_id,
        "channel": "web",
        "conversation_id": conversation_id,
        "module_id": "companion",
    }
    subscriptions = [
        event_bus.subscribe(event_type, enqueue, **filters)
        for event_type in (
            EventType.CHAT_STARTED,
            EventType.CHAT_DELTA,
            EventType.CHAT_COMPLETED,
            EventType.CHAT_FAILED,
            EventType.PORTRAIT_CHANGED,
            EventType.TURN_COMPLETED,
            EventType.TURN_FAILED,
            EventType.TURN_PAUSED,
            EventType.TASK_STARTED,
            # 只订阅 task.result，不订阅 task.completed/failed/cancelled：
            # 那三个是内部业务终态（带 error / result 诊断），且会被回执桥
            # 之外的逻辑各发一次；前端直接听它们会重复刷新。task.result 是
            # 回执桥**写库成功之后**发的唯一一条刷新通知，收到即拉历史。
            EventType.TASK_RESULT,
            EventType.WORK_RESULT,
            # 管家过程句：已落库，前端收到后把这句插进当前聊天。
            EventType.STEWARD_PROGRESS,
            # 工具执行状态：只用来在等待区显示"正在查询天气"这类临时提示，
            # 不落库、不进聊天记录、不进记忆库。字段已由 _public_event_data
            # 收成白名单（工具名 / 执行编号 / 状态 / 错误类型）。
            EventType.TOOL_STARTED,
            EventType.TOOL_COMPLETED,
            EventType.TOOL_FAILED,
        )
    ]
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
                continue
            event_data = _public_event_data(event.type, event.data)
            payload = {
                "version": 1,
                "event_id": uuid.uuid4().hex,
                "event_key": ":".join([
                    event.type.value,
                    str(event_data.get("turn_id") or event_data.get("run_id") or event_data.get("task_id") or ""),
                    event.timestamp.isoformat(),
                ]),
                "status": event_data.get("status", ""),
                "turn_id": event_data.get("turn_id", ""),
                "run_id": event_data.get("run_id", ""),
                "action_id": event_data.get("action_id", ""),
                "task_id": event_data.get("task_id"),
                "title": event_data.get("title", ""),
                "result": event_data.get("result"),
                "timestamp": event.timestamp.isoformat(),
            }
            payload.update({key: value for key, value in event_data.items() if key not in payload})
            yield f"event: {event.type.value}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
    finally:
        for subscription_id in subscriptions:
            event_bus.unsubscribe(subscription_id)


@app.get("/api/conversations/{conversation_id}/events")
async def conversation_events(conversation_id: str, account_id: str = "local-user"):
    return StreamingResponse(
        _task_event_stream(account_id, conversation_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.post("/api/conversations/{conversation_id}/clear")
async def clear(conversation_id: str, account_id: str = "local-user") -> dict[str, Any]:
    _initialize_runtime()
    manager.clear_history(_web_scope_from_path(account_id, conversation_id))
    return {"status": "ok"}


# ── 统一审批接口（唯一入口） ────────────────────────────────────────
#
# 以前有两套：聊天确认走 `/conversations/{cid}/confirmations` + `/decision`，
# Work 审批走 `/work/approvals/{approval_id}`，两边的词表还不一样
# （confirmed/rejected 对 approved/rejected/expired）。前端因此要懂
# "这一条该发哪个地址、字段叫什么"，而"重复点会不会跑两次""拒绝会不会执行"
# 要在两处各答一遍。
#
# 现在统一成三个按 `approval_id` 操作的接口，词表只有 confirmed / rejected。
# 两边的持久化语义一个字没改（见 `agent_core/approval_coordinator.py`）。


class ApprovalDecisionRequest(BaseModel):
    """统一审批的决策体。**只有一个字段。**

    账号与会话走查询串（与 `/api/work/*` 其余路由同一个约定），
    不放进请求体。体里只留一个决定，是为了让"想把什么跑起来"没有地方可以插嘴：
    那件事由存储里那条审批记录决定，请求体里塞得进东西就等于给了一条
    "拿审批编号配一组新参数"的旁路。
    """

    decision: str = Field(pattern="^(confirmed|rejected)$")


async def _resume_chat_confirmation(action_id: str, scope: Any, decision: str) -> dict[str, Any]:
    """聊天确认的续跑回调：把 `AgentOutcome` 翻成协调器要的映射。

    这层适配是必须显式存在的 —— 协调器不认识 `AgentOutcome`（那是 Chat 那条链
    自己的形状）。让它直接吃 `AgentOutcome`，就等于把两条链的数据结构粘在一起。
    """
    outcome = await manager.resume(action_id, scope, decision)
    return {
        "status": outcome.status,
        "reply": clean_character_reply(outcome.reply),
        "sources": list(getattr(outcome, "sources", ()) or ()),
        "answer_status": getattr(outcome, "answer_status", "answered"),
        # AgentOutcome predates the web adapter and carries run_id only.  Use
        # an explicitly supplied turn_id when available, otherwise run_id is
        # the stable identifier exposed for resumed turns.
        "turn_id": getattr(outcome, "turn_id", outcome.run_id),
        "run_id": outcome.run_id,
        "action_id": outcome.action_id,
    }


async def _approval_coordinator() -> ApprovalCoordinator:
    """每次现建一个协调器，**不缓存**。

    它手里只有两个引用（确认存储、Work Host），没有自己的状态；
    缓存它唯一的效果是多一个"持有过期存储/过期 Host"的机会 ——
    而 Work Host 是懒建的（`_ensure_work_host`），那种过期一定会发生。
    """
    _initialize_runtime()
    try:
        host = await _ensure_work_host()
    except HTTPException:
        # Work 起不来不该连带让聊天确认也办不了：两条链路的可用性互不依赖。
        logger.warning("approval coordinator: work host unavailable, work approvals will be hidden")
        host = None
    return ApprovalCoordinator(
        confirmation_store=confirmation_store,
        work_host=host,
        resume_chat=_resume_chat_confirmation,
    )


@app.get("/api/approvals")
async def approval_list(account_id: str = "local-user", conversation_id: str = "web") -> dict[str, Any]:
    coordinator = await _approval_coordinator()
    return {"approvals": coordinator.pending(_web_scope_from_path(account_id, conversation_id))}


@app.get("/api/approvals/{approval_id}")
async def approval_detail(
    approval_id: str, account_id: str = "local-user", conversation_id: str = "web"
) -> dict[str, Any]:
    coordinator = await _approval_coordinator()
    record = coordinator.get(approval_id, _web_scope_from_path(account_id, conversation_id))
    if record is None:
        # 不存在与不属于这个作用域回同一个 404：分开能让调用方枚举出
        # 别人有哪些审批在等。
        raise HTTPException(status_code=404, detail="approval not found")
    return record


@app.post("/api/approvals/{approval_id}")
async def approval_decide(
    approval_id: str,
    request: ApprovalDecisionRequest,
    account_id: str = "local-user",
    conversation_id: str = "web",
) -> dict[str, Any]:
    coordinator = await _approval_coordinator()
    scope = _web_scope_from_path(account_id, conversation_id)
    try:
        return await coordinator.decide(approval_id, scope, request.decision)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="approval not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ── 迁移期的旧接口（前端切完即删，见 docs/tool-gateway-plan.md §4） ──


@app.get("/api/conversations/{conversation_id}/confirmations", deprecated=True)
async def confirmations(conversation_id: str, account_id: str = "local-user") -> dict[str, Any]:
    """**迁移期代理**：老的聊天确认查询，转发到统一接口。

    返回项是统一条目的超集（含迁移期别名 `action_id`），所以老前端不用改也能跑。
    """
    coordinator = await _approval_coordinator()
    scope = _web_scope_from_path(account_id, conversation_id)
    return {
        "confirmations": [
            item for item in coordinator.pending(scope) if item["kind"] == APPROVAL_KIND_CHAT
        ]
    }


@app.post("/api/conversations/{conversation_id}/decision", deprecated=True)
async def decision(conversation_id: str, request: DecisionRequest) -> dict[str, Any]:
    """**迁移期代理**：老的聊天确认决策，转发到统一接口。"""
    coordinator = await _approval_coordinator()
    scope = _web_scope_from_path(request.account_id, conversation_id)
    try:
        return await coordinator.decide(request.action_id, scope, request.decision)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="no pending confirmation") from exc


@app.get("/api/metrics")
async def get_metrics() -> dict[str, Any]:
    """
    监控指标端点 - 返回所有收集的指标

    返回格式：
    {
        "requests": {...},
        "duration": {...},
        "active_requests": 2,
        "tokens_used": 12345,
        "retries_total": 10,
        "errors": {...}
    }
    """
    return metrics.get_metrics()


@app.get("/api/memory/console")
async def memory_console(account_id: str = "local-user", module_id: str = "companion") -> dict[str, Any]:
    return _memory_console_summary(account_id=account_id, module_id=module_id)


@app.get("/api/tools")
async def get_registered_tools() -> dict[str, Any]:
    """Project the canonical capability snapshot for the legacy tools UI."""
    global _module_registry, _tool_registry
    if _module_registry is None:
        _initialize_runtime()
    if _module_registry is None:
        raise HTTPException(status_code=503, detail="工具注册表尚未初始化")
    snapshot = await _capability_snapshot()
    modules: list[dict[str, Any]] = []
    for module in snapshot["modules"]:
        tools: list[dict[str, Any]] = []
        for item in module["tools"]:
            spec = _tool_registry.resolve(item["name"]) if _tool_registry is not None else None
            function = (spec.schema or {}).get("function", {}) if spec is not None else {}
            tools.append({
                "name": item["name"],
                "description": str(function.get("description") or "") if isinstance(function, dict) else "",
                "risk": spec.risk if spec is not None else "low",
                "confirmation_required": spec.requires_confirmation if spec is not None else False,
                "declared": bool(item.get("declared")),
                "registration": item["registration"],
                "advertised_to_model": item["advertised_to_model"],
                "code": item["code"],
                "reason": item["reason"],
            })
        modules.append({
            "id": module["id"],
            "description": module["description"],
            "status": module["status"],
            "diagnostic": snapshot["checks"]["modules"]["registered"].get(module["id"], {"status": module["status"], "code": "", "reason": ""}),
            "tools": tools,
        })
    return {
        "modules": modules,
        "diagnostics": snapshot["checks"]["modules"]["registered"],
        "registration_failures": snapshot["checks"]["modules"]["failures"],
    }


@app.get("/metrics")
async def prometheus_metrics() -> str:
    """
    Prometheus 格式的指标端点

    返回 Prometheus 文本格式，可被 Prometheus 抓取

    Example:
        curl http://localhost:8765/metrics
    """
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(
        content=metrics.export_prometheus(),
        media_type="text/plain; version=0.0.4"
    )


#: 判定"这个配置项是密钥"的判据。只看变量名，不看值 ——
#: 值长得像不像密钥是猜的，名字里写着 KEY/SECRET 是确定的。
#:
#: 注意不能只认 "API_KEY"：`AMAP_WEB_SERVICE_KEY` 里就没有 API_KEY 三个字，
#: 但它就是高德的密钥。按"以 KEY 结尾"或名字里含这些词来判，才盖得住。
_SECRET_KEY_MARKERS = ("API_KEY", "SECRET", "TOKEN", "PASSWORD", "_KEY", "KEY_")

#: 遮蔽后固定回这个字面量。长度与内容都不反映原值：
#: 连"密钥有多长"都不该从这个接口看出来。
_SECRET_PLACEHOLDER = "***已配置***"


def is_secret_setting_key(key: str) -> bool:
    """这个配置项是不是密钥。"""
    name = str(key or "").upper()
    return any(marker in name for marker in _SECRET_KEY_MARKERS)


def _mask_setting_value(key: str, value: str) -> str:
    """设置接口对外显示的值。密钥**任何长度**都不返回原文。

    以前这里写的是 `len(value) > 12` 才遮蔽，于是短密钥（测试密钥、
    随手起的占位串）会**明文返回**。这正是"看起来做了遮蔽"的典型形态：
    长密钥是遮蔽的，短密钥漏了，而短密钥同样是密钥。

    未配置返回空串而不是占位符：调用方（前端能力健康面板）靠"空不空"
    判断这个能力有没有配好，给个占位符会让它误认为已配置。
    """
    if not str(value or ""):
        return ""
    if is_secret_setting_key(key):
        return _SECRET_PLACEHOLDER
    return str(value)


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    """
    读取当前 .env 配置

    返回格式：
    {
      "settings": {
        "GEMINI_API_KEY": "***已配置***" (密钥一律不返回原文),
        "GOOGLE_GEMINI_BASE_URL": "https://...",
        "GEMINI_MODEL": "gemini-3.1-flash-lite",
        ...
      }
    }
    """
    # 重新加载 .env 确保获取最新值
    load_dotenv(dotenv_path=PROJECT_ENV_PATH, override=True)

    # 定义要暴露的配置项
    config_keys = [
        # LLM
        "GEMINI_API_KEY",
        "GOOGLE_GEMINI_BASE_URL",
        "GEMINI_MODEL",
        # 情绪标注 LLM
        # 嵌入
        "ZHIPUAI_API_KEY",
        "ZHIPUAI_EMBEDDING_MODEL",
        "ZHIPUAI_EMBEDDING_BASE_URL",
        "ZHIPUAI_EMBEDDING_DIMENSIONS",
        # TTS
        "GPT_SOVITS_URL",
        "OPENAI_API_KEY",
        "OPENAI_API_BASE",
        "OPENAI_API_MODEL",
        "TTS_REF_AUDIO",
        "TTS_REF_TEXT",
        # 搜索
        "BOCHA_API_KEY", "AMAP_WEB_SERVICE_KEY",
        "BOCHA_BASE_URL",
    ]

    settings = {}
    for key in config_keys:
        value = os.getenv(key, "")
        settings[key] = _mask_setting_value(key, value)

    return {"settings": settings}


@app.post("/api/settings")
async def update_settings(request: SettingsUpdateRequest) -> dict[str, Any]:
    """
    更新 .env 配置

    请求格式：
    {
      "settings": {
        "GEMINI_MODEL": "gemini-3.1-flash",
        "GEMINI_API_KEY": "sk-new-key-here"
      }
    }

    返回：{"status": "ok", "updated_count": 2, "note": "..."}
    """
    try:
        # 读取现有 .env 内容
        if not PROJECT_ENV_PATH.exists():
            PROJECT_ENV_PATH.write_text("", encoding="utf-8")

        lines = PROJECT_ENV_PATH.read_text(encoding="utf-8").splitlines()

        # 解析现有配置（简单 key=value 解析，忽略注释行）
        env_dict = {}
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                if "=" in stripped:
                    key, _, value = stripped.partition("=")
                    env_dict[key.strip()] = value.strip()

        # 合并新配置（前端传来的值如果是遮蔽的 "sk-***xyz"，则跳过不覆盖）
        updated_count = 0
        for key, value in request.settings.items():
            # 跳过遮蔽值（前端未修改的 secret 字段）
            if "***" in value:
                continue
            # 更新或新增
            if env_dict.get(key) != value:
                env_dict[key] = value
                updated_count += 1

        # 重新生成 .env 内容（保留注释行）
        new_lines = []
        processed_keys = set()
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                # 保留注释和空行
                new_lines.append(line)
            elif "=" in stripped:
                key = stripped.split("=")[0].strip()
                if key in env_dict:
                    new_lines.append(f"{key}={env_dict[key]}")
                    processed_keys.add(key)
                else:
                    new_lines.append(line)

        # 追加新增的配置项（不在原文件中的）
        for key, value in env_dict.items():
            if key not in processed_keys:
                new_lines.append(f"{key}={value}")

        # 写回文件
        PROJECT_ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

        # Refresh environment for subsequent requests.
        load_dotenv(dotenv_path=PROJECT_ENV_PATH, override=True)

        logger.info(f"Settings updated: {updated_count} keys changed")

        return {
            "status": "ok",
            "updated_count": updated_count,
            "note": "Configuration saved. Restart the server for runtime changes."
        }

    except Exception as exc:
        logger.error(f"Failed to update settings: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to save settings: {exc}")


# ── 账号级权限模式 API ────────────────────────────────────────────────
#
# 这两条接口的形态刻意与 /api/settings 不同：**账号在前，环境在后**。
# settings 写的是 .env（进程级、要重启才生效），permissions 写的是
# agent_core.db（账号级、下一次授权立即生效）。把它们混在一起会让用户
# 以为"切完全访问要重启"，而实际上不需要 —— 策略每次授权都重新读库。


def _permissions_layer() -> PermissionLayer:
    """权限层按需初始化。

    ``/api/permissions`` 有可能在 ``_initialize_runtime`` 之前被调用
    （例如服务刚起来、前端先拉了权限）。这时现建一份：``AccountPermissionStore``
    是幂等的（建表用 IF NOT EXISTS），只有 controller 的 web_host 需要正确 ——
    而它来自 WEB_HOST，与运行时那份同源，两份读到的 effective 完全一致。
    """
    global permission_layer
    if permission_layer is None:
        permission_layer = PermissionLayer(
            db_path=os.getenv("AGENT_CORE_DB", "agent_core.db"),
            web_host=os.getenv("WEB_HOST", "127.0.0.1"),
        )
    return permission_layer


@app.get("/api/permissions")
async def get_permissions(account_id: str = "local-user") -> dict[str, Any]:
    """读某个账号的权限模式（账号级，与会话无关）。

    返回 ``configured_mode`` 与 ``effective_mode`` 两个字段：
    前者是用户的选择（诚实记录），后者是本机环境允许的实际效果。
    非回环监听时 effective 会低于 configured，``reason`` 给出稳定错误码。
    """
    layer = _permissions_layer()
    snapshot = layer.controller.snapshot(account_id)
    snapshot["supports"] = sorted(PERMISSION_MODES)
    return snapshot


@app.put("/api/permissions")
async def update_permissions(request: PermissionUpdateRequest) -> dict[str, Any]:
    """切换账号的权限模式。

    开启完全访问时，同一动作里清掉该账号**所有会话**的旧审批状态：
    聊天侧 pending confirmation、会话锁、Host 侧 pending approval、
    以及停在等待中的计划步骤。旧动作一个都不执行 —— 用户点"开启"的
    心智是"从现在起不用点了"，不是"把攒下的一起跑一遍"。

    清理结果是"尽力而为 + 有账可查"：失败项进 ``failed`` 列表返回给前端，
    不静默吞掉（否则用户看到"已开启"却发现旧确认框还在）。
    """
    layer = _permissions_layer()
    try:
        result = layer.controller.set_mode(request.account_id, request.mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # 只在**真的生效**为完全访问时才清理：非回环监听下 effective 仍是 standard，
    # 此时该账号的旧审批依然有效，清掉等于替用户拒了一堆还有效的操作。
    cleanup: dict[str, Any] | None = None
    if result["effective_mode"] == MODE_FULL_ACCESS:
        cleanup = layer.cleanup.run(request.account_id)
        if cleanup.get("failed"):
            logger.warning(
                "permission cleanup reported failures account=%s failed=%s",
                request.account_id,
                cleanup["failed"],
            )

    result["cleanup"] = cleanup
    result["rejected_confirmations"] = (cleanup or {}).get("rejected_confirmations", 0)
    result["rejected_work_approvals"] = (cleanup or {}).get("rejected_work_approvals", 0)
    logger.info(
        "permission mode set account=%s configured=%s effective=%s",
        request.account_id,
        result["configured_mode"],
        result["effective_mode"],
    )
    return result


# ── Tasks Dashboard API ──────────────────────────────────────────────


def _task_service() -> Any | None:
    """Get the first registered TaskService instance."""
    for svc in _task_services:
        if hasattr(svc, "store") and hasattr(svc, "run_now"):
            return svc
    return None


def _condition_task_service() -> Any | None:
    """Get the first registered ConditionTaskService instance."""
    for svc in _task_services:
        if hasattr(svc, "store") and hasattr(svc, "create_weather_task"):
            return svc
    return None


def _schedule_display(schedule: dict[str, Any]) -> str:
    """Human-readable schedule description."""
    kind = schedule.get("kind", "")
    if kind == "run_at":
        run_at = schedule.get("run_at", "")
        return f"一次性 · {run_at}"
    if kind == "interval":
        seconds = int(schedule.get("interval_seconds", 0))
        if seconds >= 3600:
            return f"每 {seconds // 3600} 小时"
        return f"每 {seconds // 60} 分钟"
    if kind == "cron":
        return f"Cron: {schedule.get('cron', '')}"
    return str(schedule)


def _condition_display(task: Any) -> str:
    """Human-readable condition description."""
    city = getattr(task, "city", "")
    condition = getattr(task, "condition", "")
    condition_map = {"weather_clear": "晴天"}
    return f"{city} · {condition_map.get(condition, condition)}"


def _public_task_error(task: Any) -> dict[str, Any]:
    """任务行里"上次出过什么事"的公开投影。

    以前这里直接吐 ``last_error``：那是 TaskStore 里存的内部诊断，
    可能是 ``RuntimeError: [Errno 2] No such file: C:\\Users\\...`` ——
    异常类名、本机绝对路径、Provider 名会一起越过 HTTP 边界。
    现在只回一个稳定错误码 + 一句固定中文；原文只留在 TaskStore 与日志里。
    """
    raw = str(getattr(task, "last_error", "") or "")
    if not raw:
        return {"result_code": "", "last_result": ""}
    code = public_error_code(raw)
    return {
        "result_code": code,
        "last_result": public_task_result("failed", getattr(task, "title", ""), code),
    }


@app.get("/api/tasks")
async def list_all_tasks(account_id: str = "local-user") -> dict[str, Any]:
    """List all tasks (scheduled + condition) for the account."""
    _initialize_runtime()
    await _start_task_services()
    scope = AccountTaskScope(account_id.strip(), "companion")
    result: list[dict[str, Any]] = []

    # Scheduled tasks
    svc = _task_service()
    if svc is not None:
        try:
            # 同步 SQLite 读 → 工作线程。这个接口前端进任务页就会拉，
            # 而它读两张表、每张最多 200 行，直接在 loop 上跑会连带卡住聊天流。
            tasks = await asyncio.to_thread(svc.store.list_tasks, scope, limit=200)
            for t in tasks:
                result.append({
                    "task_id": t.task_id,
                    "title": t.title,
                    "type": "scheduled",
                    "delivery_mode": t.delivery_mode,
                    "status": t.status,
                    "schedule_display": _schedule_display(t.schedule),
                    "next_run_at": t.next_run_at,
                    "created_at": t.created_at,
                    "updated_at": t.updated_at,
                    **_public_task_error(t),
                })
        except Exception:
            pass

    # Condition tasks
    cond_svc = _condition_task_service()
    if cond_svc is not None:
        try:
            conditions = await asyncio.to_thread(cond_svc.store.list, scope)
            for t in conditions:
                result.append({
                    "task_id": t.task_id,
                    "title": t.title,
                    "type": "condition",
                    "delivery_mode": "execute",
                    "status": t.status,
                    "schedule_display": f"条件 · {_condition_display(t)}",
                    "next_run_at": t.next_check_at,
                    "created_at": t.created_at,
                    "updated_at": t.updated_at,
                    "city": t.city,
                    "condition": t.condition,
                    **_public_task_error(t),
                })
        except Exception:
            pass

    # Sort by created_at descending
    result.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return {"tasks": result}


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task_api(task_id: str, account_id: str = "local-user") -> dict[str, Any]:
    """Cancel a task by ID (scheduled or condition)."""
    _initialize_runtime()
    await _start_task_services()
    scope = AccountTaskScope(account_id.strip(), "companion")
    cancelled = False

    svc = _task_service()
    if svc is not None:
        try:
            cancelled = await svc.cancel_task(task_id, scope)
        except Exception:
            pass

    if not cancelled:
        cond_svc = _condition_task_service()
        if cond_svc is not None:
            try:
                # 条件任务的取消是纯同步存储调用，走工作线程（同类接口保持一致）。
                cancelled = await asyncio.to_thread(cond_svc.store.cancel, task_id, scope)
            except Exception:
                pass

    return {"cancelled": cancelled, "task_id": task_id}


@app.post("/api/tasks/{task_id}/run-now")
async def run_task_now_api(task_id: str, account_id: str = "local-user") -> dict[str, Any]:
    """Trigger immediate execution of a task."""
    _initialize_runtime()
    await _start_task_services()
    scope = AccountTaskScope(account_id.strip(), "companion")
    queued = False

    svc = _task_service()
    if svc is not None:
        try:
            queued = await svc.run_now(task_id, scope)
        except Exception:
            pass

    if not queued:
        cond_svc = _condition_task_service()
        if cond_svc is not None:
            try:
                # run_now 是 async（内部会 tick），它自己的存储调用已在服务层挪出 loop。
                queued = await cond_svc.run_now(task_id, scope)
            except Exception:
                pass

    return {"queued": queued, "task_id": task_id}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web_api:app", host=os.getenv("WEB_HOST", "127.0.0.1"), port=int(os.getenv("WEB_PORT", "8765")), reload=False)
