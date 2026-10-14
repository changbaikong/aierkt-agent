"""Subagent module builder: the single registration source for dispatch.

Why this file exists (2026-09-22): before it, `web_api.py` and `chat_cli.py`
each called `register_subagent_tool(...)` on their own registry. That is exactly
the shape work had before 2026-09-14, with the same consequences:

- the Web call sat inside `try/except` and only logged a warning, so "the Web
  entry has no sub-agent" was a **silent** degradation that never showed up on
  the capability snapshot;
- the two calls passed different arguments (Web forwarded its own
  `run_store`/`confirmation_store`, CLI did not and fell back to the default
  `agent_core.db`), so changing `AGENT_CORE_DB` made the two entries talk to
  different databases.

Now the capability catalog owns it: one declaration, one build function, both
entries get the same thing.
"""

from __future__ import annotations

from typing import Any

from agent_core.contracts import ToolSpec
from agent_core.modules import ModuleSpec


def build_subagent_module(
    *,
    provider: Any = None,
    registry_provider: Any = None,
    event_bus: Any = None,
    permission_layer: Any = None,
    core_db_path: str | None = None,
    prompt_composer: Any = None,
    output_mode: str = "text",
    **kwargs: Any,
) -> ModuleSpec:
    """Build the sub-agent module and its single tool.

    Args:
        provider: the shared LLM provider (delegation runs the same model).
        registry_provider: callable returning the **current** tool registry.
            The dispatch handler uses it to build the child's own registry
            (see `child_tool_registry`); it must stay a callable because the
            real registry does not exist yet while modules are being built.
        permission_layer: account-aware permission layer. Its policy is handed
            to the child loop **as is** — delegation grants no extra authority,
            and using the account-aware policy (not `DefaultRiskPolicy`) keeps
            完全访问 working inside the sub-agent too.
    """
    from agent_core.subagent import make_subagent_tool

    def build_tools() -> list[ToolSpec]:
        return [
            make_subagent_tool(
                provider,
                registry_provider=registry_provider or (lambda: None),
                event_bus=event_bus,
                child_risk_policy=getattr(permission_layer, "policy", None),
                prompt_composer=prompt_composer,
                output_mode=output_mode,
                **kwargs,
            )
        ]

    return ModuleSpec(
        "subagent",
        build_tools,
        memory_policy="private_state",
        description="独立预算的子任务委派",
        declared_tools=("dispatch",),
    )
