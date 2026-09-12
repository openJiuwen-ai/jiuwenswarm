# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read-only, transport-neutral catalog for configured custom Agents."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


class AgentCatalogError(ValueError):
    """Stable domain failure with no wire or transport representation."""

    def __init__(self, message: str, *, code: str = "BAD_REQUEST") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class AgentCatalogInput:
    """Caller scope for one Agent catalog query."""

    channel_id: str
    session_id: str | None = None
    project_dir: str = ""
    trusted_dirs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentCatalogScope:
    """Resolved project authority for one catalog inspection."""

    project_dir: Path


@dataclass(frozen=True, slots=True)
class AgentDescriptor:
    """Safe Agent metadata; prompts and backing paths are intentionally absent."""

    name: str
    description: str
    source: str
    model: str | None
    tools: tuple[str, ...]
    disallowed_tools: tuple[str, ...]
    color: str | None
    permission_mode: str | None
    memory_scope: str | None
    shadowed_by: str | None
    enabled: bool | None
    when_to_use: str | None
    max_iterations: int | None
    skills: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("tools", "disallowed_tools", "skills"):
            payload[key] = list(payload[key])
        return payload


@dataclass(frozen=True, slots=True)
class AgentCatalogResult:
    agents: tuple[AgentDescriptor, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"agents": [agent.to_dict() for agent in self.agents]}


@dataclass(frozen=True, slots=True)
class AgentToolDescriptor:
    name: str
    internal_name: str
    description: str
    group: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AgentToolsResult:
    tools: tuple[AgentToolDescriptor, ...]
    groups: tuple[str, ...]
    disallowed_for_subagents: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "tools": [tool.to_dict() for tool in self.tools],
            "groups": list(self.groups),
            "disallowed_for_subagents": list(self.disallowed_for_subagents),
        }


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def resolve_agent_catalog_scope(
    project_dir: str,
    *,
    trusted_dirs: tuple[str, ...],
) -> AgentCatalogScope:
    """Resolve an existing project below at least one existing trusted root."""
    try:
        project = Path(project_dir).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AgentCatalogError(
            "project directory does not exist", code="NOT_FOUND"
        ) from exc
    if not project.is_dir():
        raise AgentCatalogError("project directory must be a directory")

    roots: list[Path] = []
    for value in trusted_dirs:
        if not str(value or "").strip():
            continue
        try:
            root = Path(value).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if root.is_dir():
            roots.append(root)
    if not roots or not any(_is_relative_to(project, root) for root in roots):
        raise AgentCatalogError("project directory is not trusted", code="FORBIDDEN")
    return AgentCatalogScope(project_dir=project)


def _safe_service(scope: AgentCatalogScope):
    # AgentConfigService remains the source of truth for builtin definitions,
    # source precedence and enabled-state semantics.  Only filesystem discovery
    # is narrowed so a linked Markdown file cannot escape its source directory.
    from jiuwenswarm.server.runtime.agent_config_service import (
        AgentConfigService,
        _parse_agent_file,
    )

    class SafeAgentConfigService(AgentConfigService):
        @staticmethod
        def _load_from_dir(dir_path: Path, source: str) -> list[Any]:
            try:
                root = dir_path.resolve(strict=True)
            except (OSError, RuntimeError):
                return []
            if not root.is_dir():
                return []
            agents: list[Any] = []
            try:
                candidates = sorted(root.glob("*.md"))
            except OSError:
                return []
            for candidate in candidates:
                try:
                    resolved = candidate.resolve(strict=True)
                    if resolved.is_file() and _is_relative_to(resolved, root):
                        agent = _parse_agent_file(resolved, source)
                        if agent is not None:
                            agents.append(agent)
                except Exception:
                    logger.warning(
                        "Failed to parse agent file: %s",
                        candidate,
                        exc_info=True,
                    )
            return agents

    return SafeAgentConfigService(scope.project_dir)


def _descriptor(agent: Any) -> AgentDescriptor:
    return AgentDescriptor(
        name=str(agent.name),
        description=str(agent.description or ""),
        source=str(agent.source),
        model=str(agent.model) if agent.model is not None else None,
        tools=tuple(str(item) for item in (agent.tools or ())),
        disallowed_tools=tuple(str(item) for item in (agent.disallowed_tools or ())),
        color=str(agent.color) if agent.color is not None else None,
        permission_mode=(
            str(agent.permission_mode) if agent.permission_mode is not None else None
        ),
        memory_scope=(
            str(agent.memory_scope) if agent.memory_scope is not None else None
        ),
        shadowed_by=(str(agent.shadowed_by) if agent.shadowed_by is not None else None),
        enabled=agent.enabled if isinstance(agent.enabled, bool) else None,
        when_to_use=(str(agent.when_to_use) if agent.when_to_use is not None else None),
        max_iterations=(
            agent.max_iterations if isinstance(agent.max_iterations, int) else None
        ),
        skills=tuple(str(item) for item in (agent.skills or ())),
    )


def list_agents(scope: AgentCatalogScope) -> AgentCatalogResult:
    """List safe metadata using AgentConfigService precedence semantics."""
    return AgentCatalogResult(
        agents=tuple(_descriptor(agent) for agent in _safe_service(scope).list_agents())
    )


def get_agent(scope: AgentCatalogScope, name: str) -> AgentDescriptor:
    """Return the active definition without exposing its system prompt."""
    normalized = str(name or "").strip()
    if not normalized:
        raise AgentCatalogError("agent name is required")
    agent = _safe_service(scope).get_agent(normalized)
    if agent is None:
        raise AgentCatalogError("agent not found", code="NOT_FOUND")
    return _descriptor(agent)


def list_agent_tools(scope: AgentCatalogScope) -> AgentToolsResult:
    """Return the existing static tool catalog without initializing an Agent."""
    raw = _safe_service(scope).list_available_tools()
    tools: list[AgentToolDescriptor] = []
    for item in raw.get("tools", ()):
        if not isinstance(item, dict):
            continue
        tools.append(
            AgentToolDescriptor(
                name=str(item.get("name") or ""),
                internal_name=str(item.get("internal_name") or ""),
                description=str(item.get("description") or ""),
                group=str(item.get("group") or ""),
            )
        )
    return AgentToolsResult(
        tools=tuple(tools),
        groups=tuple(str(item) for item in raw.get("groups", ())),
        disallowed_for_subagents=tuple(
            sorted(str(item) for item in raw.get("disallowed_for_subagents", ()))
        ),
    )


__all__ = [
    "AgentCatalogError",
    "AgentCatalogInput",
    "AgentCatalogResult",
    "AgentCatalogScope",
    "AgentDescriptor",
    "AgentToolDescriptor",
    "AgentToolsResult",
    "get_agent",
    "list_agent_tools",
    "list_agents",
    "resolve_agent_catalog_scope",
]
