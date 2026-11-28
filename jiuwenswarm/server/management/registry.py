# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Explicit lazy provider composition for one execution resource owner."""

from __future__ import annotations

from importlib import import_module
from typing import Any

PROVIDER_TYPES = {
    "trajectory": ("jiuwenswarm.server.management.trajectory", "TrajectoryManagement"),
    "recommendations": ("jiuwenswarm.server.management.recommendations", "RecommendationsManagement"),
    "sessions": ("jiuwenswarm.server.management.sessions", "SessionsManagement"),
    "teams": ("jiuwenswarm.server.management.teams", "TeamsManagement"),
    "rsi": ("jiuwenswarm.server.management.rsi", "RsiManagement"),
    "authorization": (
        "jiuwenswarm.server.management.authorization",
        "AuthorizationManagement",
    ),
    "history": ("jiuwenswarm.server.management.history", "HistoryManagement"),
    "proactive": ("jiuwenswarm.server.management.proactive", "ProactiveManagement"),
    "workflows": ("jiuwenswarm.server.management.workflows", "WorkflowsManagement"),
    "workflow_control": (
        "jiuwenswarm.server.management.workflow_control",
        "WorkflowControlManagement",
    ),
    "commands": ("jiuwenswarm.server.management.commands", "CommandsManagement"),
    "mcp": ("jiuwenswarm.server.management.mcp", "McpManagement"),
    "assets": ("jiuwenswarm.server.management.assets", "AssetsManagement"),
    "sandbox": ("jiuwenswarm.server.management.sandbox", "SandboxManagement"),
    "agents": ("jiuwenswarm.server.management.agents", "AgentsManagement"),
    "extensions": ("jiuwenswarm.server.management.extensions", "ExtensionsManagement"),
    "packages": ("jiuwenswarm.server.management.packages", "PackagesManagement"),
    "schedule": ("jiuwenswarm.server.management.schedule", "ScheduleManagement"),
}


class ManagementServices:
    """Construct only the requested domain, preserving one Runtime owner."""

    def __init__(self, host: Any) -> None:
        self.host = host
        self._providers: dict[str, Any] = {}

    def provider(self, domain: str) -> Any:
        if domain not in self._providers:
            module, name = PROVIDER_TYPES[domain]
            self._providers[domain] = getattr(import_module(module), name)(self.host)
        return self._providers[domain]
