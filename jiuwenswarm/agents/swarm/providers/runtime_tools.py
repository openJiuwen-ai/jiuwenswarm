# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime tool providers for swarm provider-based team assembly.

Ports the ``register_member_runtime_tools`` logic of the legacy ``team_manager``
into config-sourced tool providers. Each factory returns a flat list of tool
*instances*; openjiuwen's ``create_deep_agent`` performs the actual resource/
ability registration, so these providers never touch ``Runner.resource_mgr`` or
``agent.ability_manager`` (that imperative wiring is the customizer's job, not a
provider's).

Covered runtime tools:

* ``cron_tools`` — the per-member cron toolkit built by ``CronRuntimeBridge``,
  scoped to ``team_member_<member_card_id>``.
* ``send_file`` — the ``send_file_to_user`` toolkit, gated by the channel's
  ``send_file_allowed`` config (web defaults to enabled, others disabled) and by
  the presence of a request id / channel id.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

from openjiuwen.agent_teams.harness.manifest import (
    ConstructionInput,
    context_field,
    ElementKind,
    harness_element,
    param_field,
)

from jiuwenswarm.agents.harness.common.tools.cron.cron_runtime import CronRuntimeBridge
from jiuwenswarm.agents.harness.common.tools.send_file_to_user import SendFileToolkit
from jiuwenswarm.agents.swarm.context import SwarmBuildContext

logger = logging.getLogger(__name__)

# Provider name constants; namespaced under the shared "swarm." prefix.
CRON_TOOLS = "swarm.cron_tools"
SEND_FILE = "swarm.send_file"


class CronToolsInput(ConstructionInput):
    """Construction inputs for the per-member cron toolkit."""

    member_card_id: str | None = context_field(
        attr="member_card_id",
        description="Member card id used as the cron tool scope / agent id.",
    )
    channel_id: str | None = context_field(
        attr="channel_id", description="Raw channel id."
    )
    session_id: str | None = context_field(
        attr="session_id", description="Active session id."
    )
    request_metadata: dict[str, Any] | None = context_field(
        attr="request_metadata",
        description="Request metadata mapping.",
    )
    language: str = context_field(
        attr="language", default="cn", description="Member language code."
    )


@harness_element(
    kind=ElementKind.TOOL,
    name=CRON_TOOLS,
    description="Per-member cron toolkit (create/list/manage scheduled tasks), "
    "scoped to team_member_<member_card_id>.",
    input_model=CronToolsInput,
)
def build_cron_tools(params: dict[str, Any], ctx: SwarmBuildContext) -> list[Any]:
    """Build the per-member cron toolkit from the config source.

    Mirrors the cron branch of ``register_member_runtime_tools``: builds a
    ``team``-mode cron context scoped to this member and delegates to
    ``CronRuntimeBridge.build_tools``.

    Args:
        params: Provider params (unused; kept for the provider contract).
        ctx: The per-member build context.

    Returns:
        The cron tool instances, or an empty list when construction fails.
    """
    inp = CronToolsInput.resolve(params, ctx)
    agent_id = inp.member_card_id
    cron_context = SimpleNamespace(
        tool_scope=f"team_member_{agent_id or 'unknown'}",
        channel_id=inp.channel_id or "web",
        session_id=inp.session_id,
        metadata=inp.request_metadata,
        mode="team",
    )
    try:
        cron_tools = CronRuntimeBridge().build_tools(
            context=cron_context,
            agent_id=agent_id,
            language=inp.language,
        )
        logger.info(
            "[swarm.cron_tools] built %d cron tools for agent_id=%s",
            len(cron_tools),
            agent_id,
        )
        return list(cron_tools)
    except Exception as exc:
        logger.warning(
            "[swarm.cron_tools] cron tool construction failed for agent_id=%s: %s",
            agent_id,
            exc,
        )
        return []


def _is_send_file_enabled(config: dict[str, Any] | None, channel_id: str) -> bool:
    """Resolve whether file sending is allowed for *channel_id*.

    Reads ``channels.<channel_id>.send_file_allowed``; when unset, the ``web``
    channel defaults to enabled and all other channels default to disabled.

    Args:
        config: The resolved ``config.yaml`` mapping.
        channel_id: The channel id to resolve the switch for.

    Returns:
        ``True`` when file sending is allowed for the channel.
    """
    send_file_allowed = None
    if isinstance(config, dict):
        send_file_allowed = (
            config.get("channels", {}).get(str(channel_id), {}).get("send_file_allowed")
        )
    if send_file_allowed is None:
        return channel_id == "web"
    return bool(send_file_allowed)


class SendFileInput(ConstructionInput):
    """Construction inputs for the send_file_to_user toolkit."""

    channels_config: dict[str, Any] = param_field(
        default_factory=dict,
        description="Per-channel config (the send_file_allowed switch lives here).",
    )
    request_id: str | None = context_field(
        attr="request_id",
        description="Originating request id (required; skipped when absent).",
    )
    channel_id: str | None = context_field(
        attr="channel_id",
        description="Raw channel id (required; skipped when absent).",
    )
    session_id: str | None = context_field(
        attr="session_id", description="Active session id."
    )
    request_metadata: dict[str, Any] | None = context_field(
        attr="request_metadata",
        description="Request metadata mapping.",
    )
    project_dir: str | None = context_field(
        attr="project_dir",
        description="Active user project directory.",
    )
    team_workspace_root: str | None = context_field(
        attr="team_ws_root",
        description="Internal team collaboration workspace root.",
    )


@harness_element(
    kind=ElementKind.TOOL,
    name=SEND_FILE,
    description="The send_file_to_user toolkit, gated by the channel's "
    "send_file_allowed config and the presence of a request/channel id.",
    input_model=SendFileInput,
)
def build_send_file_tools(params: dict[str, Any], ctx: SwarmBuildContext) -> list[Any]:
    """Build the ``send_file_to_user`` toolkit from the config source.

    Mirrors the send-file branch of ``register_member_runtime_tools``: requires a
    request id and channel id, and is gated by the channel's ``send_file_allowed``
    config switch.

    Args:
        params: Provider params (unused; kept for the provider contract).
        ctx: The per-member build context.

    Returns:
        The send-file tool instances, or an empty list when the capability is
        skipped (missing ids / disabled by config) or construction fails.
    """
    inp = SendFileInput.resolve(params, ctx)
    if not inp.request_id or not inp.channel_id:
        logger.info("[swarm.send_file] skipped: missing request_id or channel_id")
        return []

    if not _is_send_file_enabled({"channels": inp.channels_config}, inp.channel_id):
        logger.info(
            "[swarm.send_file] skipped: send_file_allowed=False for channel=%s",
            inp.channel_id,
        )
        return []

    try:
        toolkit = SendFileToolkit(
            request_id=inp.request_id,
            session_id=inp.session_id,
            channel_id=inp.channel_id,
            metadata=inp.request_metadata,
            project_dir=inp.project_dir,
            team_workspace_root=inp.team_workspace_root,
        )
        tools = list(toolkit.get_tools())
        logger.info(
            "[swarm.send_file] built %d send-file tools for channel=%s",
            len(tools),
            inp.channel_id,
        )
        return tools
    except Exception as exc:
        logger.warning("[swarm.send_file] send-file tool construction failed: %s", exc)
        return []


__all__ = [
    "CRON_TOOLS",
    "SEND_FILE",
    "build_cron_tools",
    "build_send_file_tools",
]
