"""Route durable Work attempts to providers without mixing their state machines."""

from __future__ import annotations

from dataclasses import replace as _replace
from typing import Any, Iterable, Mapping

from .work_provider import (
    TaskTypeCapabilities,
    WorkProvider,
    WorkProviderCapabilities,
    WorkRequest,
    WorkResult,
    capabilities_for_task_type,
)


class CompositeWorkProvider:
    """Dispatch each task type to its owning provider.

    Two selection rules, in this order:

    1. **显式标识**：``request.provider_selector`` 命中某个已注册 provider 就用它。
       命中不了就是失败，**不回落**到默认 owner —— 静默回落会让"选了 A 环境、
       跑在 B 环境"这种事只体现在结果里，用户看不到。
    2. **默认 owner**：没给标识时按 task_type 找默认 provider。默认 owner 是
       第一个"非 selectable"的注册者；如果该 task_type 下全是 selectable，
       则取第一个注册者。这条规则保证老调用（不带标识）行为不变。
    """

    def __init__(self, providers: Iterable[WorkProvider]) -> None:
        self._providers = tuple(providers)
        self._by_task_type: dict[str, WorkProvider] = {}
        self._by_provider_id: dict[str, WorkProvider] = {}
        self._attempt_owners: dict[str, WorkProvider] = {}
        for provider in self._providers:
            provider_id = str(provider.capabilities.provider_id)
            if provider_id in self._by_provider_id:
                raise ValueError(f"duplicate provider id: {provider_id}")
            self._by_provider_id[provider_id] = provider
            for task_type in provider.capabilities.task_types:
                owner = self._by_task_type.get(task_type)
                if owner is None:
                    self._by_task_type[task_type] = provider
                    continue
                if not provider.capabilities.selectable and not owner.capabilities.selectable:
                    raise ValueError(f"duplicate provider for task type: {task_type}")
                # 非 selectable 的一方是默认 owner；两者都 selectable 时保持先注册的。
                if owner.capabilities.selectable and not provider.capabilities.selectable:
                    self._by_task_type[task_type] = provider
        self.capabilities = WorkProviderCapabilities(
            provider_id="composite-worker",
            task_types=tuple(sorted(self._by_task_type)),
            # 组合层**不声称任何执行能力**：取消/恢复这类能力一律按 task_type
            # 解析到具体执行者（见 ``task_capabilities``）。以前这里用 any() 把
            # "某个 provider 能取消"汇总成"组合能取消"，于是 A 的能力被冒充到
            # B 的任务上——正是能力误报的来源。
            supports_cancel=False,
            supports_resume=False,
        )

    @property
    def providers(self) -> tuple[WorkProvider, ...]:
        return self._providers

    def provider_for(self, task_type: str) -> WorkProvider | None:
        return self._by_task_type.get(task_type)

    def provider_by_id(self, provider_id: str) -> WorkProvider | None:
        return self._by_provider_id.get(str(provider_id or "").strip())

    def task_capabilities(self, task_type: str) -> TaskTypeCapabilities:
        """按 task_type 解析出的**真实执行者**能力报告。

        报告来自默认 owner（显式 provider 标识的选择在 execute/resume 时已由
        ``resolve`` 保证与账本一致，能力面只按归属展示）。类型没人认领时返回
        ``can_execute=False`` 的报告，让匹配检查给出 rejected 而不是含糊其辞。
        """
        provider = self.provider_for(task_type)
        if provider is None:
            return TaskTypeCapabilities(
                task_type=task_type,
                can_execute=False,
                notes=(f"no registered provider owns task_type {task_type}",),
            )
        report = capabilities_for_task_type(provider, task_type)
        if report.can_execute:
            return report
        # 认领该类型的 provider 自己报告"不能执行"（例如 Harness 配置不可用）：
        # 保留它的理由，不让组合层把归属说回成"可以"。
        return _replace(
            report,
            notes=(*report.notes, f"resolved owner {provider.capabilities.provider_id} cannot execute"),
        )

    def resolve(self, task_type: str, provider_id: str = "") -> WorkProvider | None:
        """Explicit id first, default owner second; unknown id returns None."""
        selector = str(provider_id or "").strip()
        if selector:
            return self.provider_by_id(selector)
        return self.provider_for(task_type)

    def describe(self) -> list[dict[str, Any]]:
        """Registration table for the capabilities endpoint.

        ``default`` tells the UI which provider runs when no id is sent;
        ``selectable`` tells it which providers may be picked explicitly.
        ``capabilities_by_task_type`` is the per-task capability report the match
        check consumes — the same numbers, resolved per task type instead of
        summed across providers.
        """
        described: list[dict[str, Any]] = []
        for provider in self._providers:
            capabilities = provider.capabilities
            described.append(
                {
                    "provider_id": capabilities.provider_id,
                    "task_types": list(capabilities.task_types),
                    "supports_cancel": bool(capabilities.supports_cancel),
                    "supports_resume": bool(capabilities.supports_resume),
                    "selectable": bool(capabilities.selectable),
                    "default": any(
                        self._by_task_type.get(task_type) is provider
                        for task_type in capabilities.task_types
                    ),
                    "capabilities_by_task_type": {
                        task_type: capabilities_for_task_type(provider, task_type).as_dict()
                        for task_type in capabilities.task_types
                    },
                }
            )
        return described

    async def execute(self, request: WorkRequest, emit: Any) -> WorkResult:
        selector = request.provider_selector
        provider = self.resolve(request.task_type, selector)
        if provider is None:
            if selector:
                return WorkResult("failed", error=f"unknown work provider: {selector}")
            return WorkResult("failed", error=f"unsupported task_type: {request.task_type}")
        self._attempt_owners[request.attempt_id] = provider
        return await provider.execute(request, emit)

    async def cancel(self, attempt_id: str, provider_id: str = "") -> bool:
        provider = self._attempt_owners.get(attempt_id)
        if provider is None and str(provider_id or "").strip():
            provider = self.provider_by_id(provider_id)
        if provider is not None:
            if not provider.capabilities.supports_cancel:
                return False
            return bool(await provider.cancel(attempt_id))
        for candidate in self._providers:
            if candidate.capabilities.supports_cancel and await candidate.cancel(attempt_id):
                self._attempt_owners[attempt_id] = candidate
                return True
        return False

    def cancel_status(self, attempt_id: str) -> str | None:
        """Forward the optional 'how did the cancel end' report to the Host.

        Synchronous on purpose: it reads a flag the provider already set while
        handling ``cancel``. Making it async would invite a second round trip to
        the external process just to answer "did it stop".
        """
        provider = self._attempt_owners.get(attempt_id)
        candidates = (provider,) if provider is not None else self._providers
        for candidate in candidates:
            if candidate is None:
                continue
            reader = getattr(candidate, "cancel_status", None)
            if not callable(reader):
                continue
            value = reader(attempt_id)
            if value:
                return str(value)
        return None

    async def resume(
        self,
        request: WorkRequest,
        checkpoint: Mapping[str, Any],
        decision: str,
        emit: Any,
    ) -> WorkResult:
        selector = request.provider_selector
        provider = self.resolve(request.task_type, selector)
        if provider is None:
            if selector:
                return WorkResult("failed", error=f"unknown work provider: {selector}")
            return WorkResult("failed", error=f"unsupported task_type: {request.task_type}")
        if not provider.capabilities.supports_resume:
            return WorkResult(
                "interrupted",
                error=f"provider does not support resume: {provider.capabilities.provider_id}",
            )
        self._attempt_owners[request.attempt_id] = provider
        return await provider.resume(request, checkpoint, decision, emit)
