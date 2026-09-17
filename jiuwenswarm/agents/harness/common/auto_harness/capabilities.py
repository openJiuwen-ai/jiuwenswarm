# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Capability registry for auto-harness scenario extensions."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from openjiuwen.core.foundation.llm import Model

from .task_store import TaskStore


class AutoHarnessCapability(Protocol):
    """Scenario extension interface for auto-harness capabilities."""

    async def handle(
        self,
        action: str,
        params: dict[str, Any],
        model: Optional[Model] = None,
    ) -> dict[str, Any]:
        ...


class AutoHarnessCapabilityRegistry:
    """Registry that keeps scenario capabilities out of the core service."""

    def __init__(self) -> None:
        self._capabilities: dict[str, AutoHarnessCapability] = {}

    def register(self, name: str, capability: AutoHarnessCapability) -> None:
        self._capabilities[name] = capability

    async def handle(
        self,
        name: str,
        action: str,
        params: dict[str, Any],
        model: Optional[Model] = None,
    ) -> dict[str, Any]:
        capability = self._capabilities.get(name)
        if capability is None:
            return {"error": f"未知 auto-harness 能力: {name}"}
        return await capability.handle(action, params, model)


def create_default_capability_registry(
    *,
    data_dir: Path,
    task_store: TaskStore,
    harness_service: Any,
    base_config_getter: Callable[[], Any],
    default_repo_url: str,
) -> AutoHarnessCapabilityRegistry:
    """Create built-in auto-harness capabilities."""
    from .issue_fix.issue_matrix_store import IssueMatrixStore
    from .issue_fix.issue_state_store import IssueStateStore
    from .issue_fix.run_progress import (
        enrich_issue_fix_progress,
        infer_issue_fix_skipped_stages,
    )
    from .issue_fix.service import IssueFixService

    registry = AutoHarnessCapabilityRegistry()
    issue_state_store = IssueStateStore(data_dir)
    issue_matrix_store = IssueMatrixStore(data_dir)
    task_store.register_run_log_status_extension(
        skipped_stage_inferer=infer_issue_fix_skipped_stages,
        progress_enricher=enrich_issue_fix_progress,
    )
    registry.register(
        "issue",
        IssueFixService(
            task_store=task_store,
            issue_state_store=issue_state_store,
            issue_matrix_store=issue_matrix_store,
            harness_service=harness_service,
            base_config_getter=base_config_getter,
            default_repo_url=default_repo_url,
        ),
    )
    return registry
