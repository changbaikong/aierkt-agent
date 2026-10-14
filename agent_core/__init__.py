"""Business-neutral primitives for the companion Agent.

注意：`ToolExecutor` **不在**这里导出。它已降级为 `agent_core.tools._ToolExecutor`
（网关的内部实现），公开导出会让新代码误以为它是正经入口。
执行动作请走 `agent_core.actions.ActionGateway`。
"""

from .contracts import (
    AccountTaskScope,
    AgentOutcome,
    AgentRequest,
    ConfirmationRequest,
    ProviderResponse,
    Scope,
    ToolCall,
    ToolResult,
    ToolSpec,
    IngressContext,
)
from .loop import AgentLoop
from .memory_agent import MemoryAgentSession
from .modules import ModuleRegistry, ModuleSpec
from .prompts import PersonaComposer, PromptComposer, PromptContext, PromptParts, ToolPromptComposer
from .conversation_manager import ConversationManager, TurnHandle
from .runner import AgentRunner, RecoveryNotice
from .tools import RiskPolicy, ToolRegistry
from .work_provider import (
    WorkEvent,
    WorkProvider,
    WorkProviderCapabilities,
    WorkRequest,
    WorkResult,
)
from .work_ledger import (
    ApprovalRecord,
    AttemptRecord,
    EventRecord,
    WorkItemRecord,
    WorkLedger,
)
from .file_work import FileWorkService
from .local_worker_provider import LocalWorkerProvider
from .provider_activity import ProviderActivity
from .work_host import WorkHost

__all__ = [
    "AgentLoop",
    "AgentRunner",
    "AccountTaskScope",
    "ConversationManager",
    "MemoryAgentSession",
    "ModuleRegistry",
    "ModuleSpec",
    "PromptComposer",
    "PromptContext",
    "PromptParts",
    "PersonaComposer",
    "ToolPromptComposer",
    "AgentOutcome",
    "AgentRequest",
    "ConfirmationRequest",
    "IngressContext",
    "ProviderResponse",
    "RiskPolicy",
    "RecoveryNotice",
    "Scope",
    "ToolCall",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "TurnHandle",
    "WorkEvent",
    "WorkProvider",
    "WorkProviderCapabilities",
    "WorkRequest",
    "WorkResult",
    "ApprovalRecord",
    "AttemptRecord",
    "EventRecord",
    "WorkItemRecord",
    "WorkLedger",
    "FileWorkService",
    "LocalWorkerProvider",
    "ProviderActivity",
    "WorkHost",
]
