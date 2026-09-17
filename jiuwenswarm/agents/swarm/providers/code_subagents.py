# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Config-sourced code and browser sub-agent providers for swarm team assembly.

Declares the swarm-specific ``code`` and ``browser`` sub-agents as ``swarm.*``
sub-agent providers resolved via ``SubAgentSpec.factory_name`` during
``spec.build()``. The explore / plan sub-agents are provided by openjiuwen;
``code_agent`` and ``browser_agent`` stay swarm-side.

``code_agent`` reuses the swarm ``CodingMemoryRail`` instance.

``browser_agent`` (``swarm.browser_agent``) gives each swarm member its own
isolated browser by passing a unique ``browser_key`` (derived from the session
id + member name) to ``build_browser_agent_config``. agent-core turns that key
into a per-member ``BrowserInstanceConfig``: a suffixed MCP ``server_id`` (own
``@playwright/mcp`` subprocess), an auto-allocated debug port, and an own
``.browser-profiles/<key>`` user-data-dir under managed mode. Without a key,
every member shares ``Runner.resource_mgr``'s single ``playwright_official_stdio``
connection — one shared browser. We only need a swarm-side provider (rather than
the generic ``core.browser_agent``) because the key must be read from the
per-member build context, not a static param.

The parent member model is read from ``ctx.extras["_parent_model"]``
(published by ``DeepAgentSpec.build``). Both providers skip when no model
is present.
"""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.agent_teams.harness.manifest import (
    ConstructionInput,
    context_field,
    ElementKind,
    harness_element,
    param_field,
)
from openjiuwen.harness.schema.build_context import parent_sys_operation
from openjiuwen.harness.subagents.browser_agent import build_browser_agent_config
from openjiuwen.harness.subagents.code_agent import build_code_agent_config

from jiuwenswarm.agents.harness.common.browser_defaults import (
    DEFAULT_BROWSER_AGENT_MAX_ITERATIONS,
)
from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.agents.swarm.providers.code_rails import (
    code_runtime_language,
    CODING_MEMORY_EXTRAS_KEY,
)
from jiuwenswarm.server.runtime.agent_adapter.statusline_setup_agent import (
    DEFAULT_STATUSLINE_SETUP_MAX_ITERATIONS,
    build_statusline_setup_agent_config,
)

logger = logging.getLogger(__name__)

CODE_AGENT = "swarm.code_agent"
SWARM_BROWSER_AGENT = "swarm.browser_agent"
STATUSLINE_SETUP_AGENT = "swarm.statusline_setup_agent"

# Key under ``ctx.extras`` where ``DeepAgentSpec.build`` publishes the resolved
# parent member model for sub-agent providers to reuse.
_PARENT_MODEL_EXTRAS_KEY = "_parent_model"
_DEFAULT_MAX_ITERATIONS = 15


def _workspace_root(ctx: SwarmBuildContext) -> str | None:
    """Resolve the member workspace root path."""
    return getattr(ctx.workspace, "root_path", None) if ctx.workspace else None


class CodeAgentInput(ConstructionInput):
    """Construction inputs for the swarm code sub-agent."""

    max_iterations: int = param_field(
        default=_DEFAULT_MAX_ITERATIONS,
        description="Maximum task-loop iterations for the sub-agent.",
    )
    workspace_root: str | None = context_field(
        resolver=_workspace_root,
        description="Member workspace root (defaults to ./ when absent).",
    )
    language: str = context_field(
        resolver=code_runtime_language,
        default="en",
        description="Code runtime language for the sub-agent.",
    )


@harness_element(
    kind=ElementKind.SUBAGENT,
    name=CODE_AGENT,
    description="Code execution sub-agent reusing the main agent's CodingMemoryRail; "
    "skipped when no parent model is available.",
    input_model=CodeAgentInput,
)
def build_code_agent(factory_kwargs: dict[str, Any], ctx: SwarmBuildContext) -> Any:
    """Build the code sub-agent, reusing the main agent's CodingMemoryRail.

    ``build_code_agent_config`` requires a model; returns None (skipped) when no
    parent model is available on the context.
    """
    inp = CodeAgentInput.resolve(factory_kwargs, ctx)
    model = ctx.extras.get(_PARENT_MODEL_EXTRAS_KEY)
    if model is None:
        logger.warning("[swarm.code_agent] skipped: no parent model on build context")
        return None
    rails = None
    coding_memory_rail = ctx.extras.get(CODING_MEMORY_EXTRAS_KEY)
    if coding_memory_rail is not None:
        # SysOperationRail is code_agent's default rail; passing rails overrides
        # the defaults, so it must be included explicitly alongside the shared
        # CodingMemoryRail.
        from openjiuwen.harness.rails import SysOperationRail

        rails = [SysOperationRail(), coding_memory_rail]
    spec = build_code_agent_config(
        model,
        rails=rails,
        workspace=str(inp.workspace_root or "./"),
        language=inp.language,
        max_iterations=inp.max_iterations,
    )
    spec.factory_kwargs = {"auto_create_workspace": False}
    return spec


@harness_element(
    kind=ElementKind.SUBAGENT,
    name=STATUSLINE_SETUP_AGENT,
    description="Built-in JiuwenSwarm TUI status-line setup subagent.",
    input_model=CodeAgentInput,
)
def build_statusline_setup_agent(
    factory_kwargs: dict[str, Any],
    ctx: SwarmBuildContext,
) -> Any:
    """Build the status-line setup subagent for a team member."""

    inp = CodeAgentInput.resolve(factory_kwargs, ctx)
    model = ctx.extras.get(_PARENT_MODEL_EXTRAS_KEY)
    if model is None:
        logger.warning("[swarm.statusline_setup_agent] skipped: no parent model")
        return None
    return build_statusline_setup_agent_config(
        model,
        workspace=str(inp.workspace_root or "./"),
        sys_operation=parent_sys_operation(ctx),
        language=inp.language,
        max_iterations=inp.max_iterations,
    )


class BrowserAgentInput(ConstructionInput):
    """Construction inputs for the swarm browser sub-agent."""

    max_iterations: int = param_field(
        default=DEFAULT_BROWSER_AGENT_MAX_ITERATIONS,
        description="Maximum task-loop iterations for the sub-agent.",
    )
    workspace_root: str | None = context_field(
        resolver=_workspace_root,
        description="Member workspace root (defaults to ./ when absent).",
    )
    language: str = context_field(
        resolver=code_runtime_language,
        default="en",
        description="Code runtime language for the sub-agent.",
    )
    session_id: str = context_field(
        attr="session_id",
        default="",
        description="Active session id — combined with role to derive a unique browser server_id.",
    )
    role: str = context_field(
        attr="role",
        default="",
        description="Member role ('leader'/'teammate') — only a coarse fallback discriminator.",
    )
    member_name: str = context_field(
        attr="member_name",
        default="",
        description="Unique member name — the real per-member browser discriminator. "
        "Every teammate shares role='teammate', so role alone collides; member_name "
        "(e.g. 'browser-usd-sgd') is distinct per spawned member.",
    )


def _browser_key(session_id: str, member_name: str, role: str) -> str:
    """Return the per-member ``browser_key`` for ``build_browser_agent_config``.

    ``role`` is only ever 'leader'/'teammate', so it collides across teammates;
    ``member_name`` is unique per member (the leader/template specs carry only a
    role, so fall back to role when member_name is absent). The session id is
    folded in so members of different concurrent sessions that happen to share a
    member_name never collide onto one browser. agent-core sanitizes the key to
    id-safe chars; an empty key preserves legacy shared-browser behavior.
    """
    disc = (member_name or "").strip() or (role or "").strip()
    if not disc:
        return ""
    return f"{session_id}-{disc}" if session_id else disc


@harness_element(
    kind=ElementKind.SUBAGENT,
    name=SWARM_BROWSER_AGENT,
    description="Browser sub-agent with per-member browser isolation: each member passes a "
    "unique browser_key, so agent-core allocates a separate @playwright/mcp subprocess, debug "
    "port and user-data-dir (managed mode) per member.",
    input_model=BrowserAgentInput,
)
def build_swarm_browser_agent(factory_kwargs: dict[str, Any], ctx: SwarmBuildContext) -> Any:
    """Build the browser sub-agent config with a per-member ``browser_key``."""
    inp = BrowserAgentInput.resolve(factory_kwargs, ctx)
    model = ctx.extras.get(_PARENT_MODEL_EXTRAS_KEY)
    if model is None:
        logger.warning("[swarm.browser_agent] skipped: no parent model on build context")
        return None

    browser_key = _browser_key(inp.session_id, inp.member_name, inp.role)
    spec = build_browser_agent_config(
        model,
        workspace=str(inp.workspace_root or "./"),
        language=inp.language,
        max_iterations=inp.max_iterations,
        browser_key=browser_key,
    )
    # build_browser_agent_config bakes the resolved RuntimeSettings (carrying the
    # per-key BrowserInstanceConfig) into spec.factory_kwargs; preserve it and
    # only add the workspace flag.
    spec.factory_kwargs = {**(spec.factory_kwargs or {}), "auto_create_workspace": False}
    logger.info(
        "[swarm.browser_agent] member_name=%r role=%r browser_key=%r",
        inp.member_name, inp.role, browser_key,
    )
    return spec


__all__ = [
    "CODE_AGENT",
    "DEFAULT_STATUSLINE_SETUP_MAX_ITERATIONS",
    "STATUSLINE_SETUP_AGENT",
    "SWARM_BROWSER_AGENT",
]
