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
* ``clouddoc_tools`` — the co-scribe toolkit, gated by ``clouddoc.enabled``, by a
  configured connection, and by the turn being attended. See the factory for why the
  last of those is a refusal rather than a filter.
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
from jiuwenswarm.agents.harness.common.tools.file_delivery_policy import is_send_file_enabled
from jiuwenswarm.agents.swarm.context import SwarmBuildContext

logger = logging.getLogger(__name__)

# Provider name constants; namespaced under the shared "swarm." prefix.
CRON_TOOLS = "swarm.cron_tools"
SEND_FILE = "swarm.send_file"
CLOUDDOC_TOOLS = "swarm.clouddoc_tools"


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
    user_id: str | None = context_field(
        attr="user_id", description="Authenticated request owner for routed tools."
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
    # Team members receive the original request metadata, which intentionally
    # does not duplicate every session field.  Cron project binding must still
    # use the caller's project (especially code-mode projects), rather than
    # silently falling back to the work default project.
    metadata = dict(inp.request_metadata or {})
    if isinstance(inp.session_id, str) and inp.session_id.strip():
        try:
            from jiuwenswarm.server.runtime.session.session_metadata import (
                get_session_metadata,
            )

            session_metadata = get_session_metadata(
                inp.session_id.strip(), cache_bust=True, enable_writeback=False
            )
            if isinstance(session_metadata, dict):
                for key in ("project_id", "project_dir", "work_mode", "model_name"):
                    if not str(metadata.get(key) or "").strip():
                        value = session_metadata.get(key)
                        if isinstance(value, str) and value.strip():
                            metadata[key] = value.strip()
                if not str(metadata.get("model_name") or "").strip():
                    model = session_metadata.get("model")
                    if isinstance(model, str) and model.strip():
                        metadata["model_name"] = model.strip()
        except Exception as exc:  # noqa: BLE001 - cron retains legacy fallback
            logger.debug(
                "[swarm.cron_tools] failed to load session project binding: %s", exc
            )
    cron_context = SimpleNamespace(
        tool_scope=f"team_member_{agent_id or 'unknown'}",
        channel_id=inp.channel_id or "web",
        session_id=inp.session_id,
        metadata=metadata,
        user_id=inp.user_id,
        # Preserve code.team/team.work variants so a cron execution retains
        # the same runtime mode as the originating team conversation.
        mode=str(getattr(ctx, "mode", "") or "team"),
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

    Reads ``channels.<channel_id>.send_file_allowed``. The internal full-duplex
    delegate inherits Web policy when no channel-specific switch is configured.

    Args:
        config: The resolved ``config.yaml`` mapping.
        channel_id: The channel id to resolve the switch for.

    Returns:
        ``True`` when file sending is allowed for the channel.
    """
    return is_send_file_enabled(config, channel_id)


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
    user_id: str | None = context_field(
        attr="user_id", description="Authenticated request owner for routed downloads."
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
            user_id=inp.user_id,
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


class CloudDocToolsInput(ConstructionInput):
    """Construction inputs for the co-scribe toolkit."""

    clouddoc_config: dict[str, Any] = param_field(
        default_factory=dict,
        description="The clouddoc config section: enable switch, connections, "
        "approve/keep words, working-style file.",
    )
    session_id: str | None = context_field(
        attr="session_id",
        description="Active session id; the ambiguity rail reads this session's own "
        "user text as its only evidence.",
    )
    channel_id: str | None = context_field(
        attr="channel_id",
        description="Raw channel id. The co-scribe channel marks an unattended turn, "
        "which this path refuses rather than serves.",
    )


@harness_element(
    kind=ElementKind.TOOL,
    name=CLOUDDOC_TOOLS,
    description="The co-scribe cloud-document toolkit, gated by clouddoc.enabled, "
    "a configured connection, and the turn being attended.",
    input_model=CloudDocToolsInput,
)
def build_clouddoc_tools(params: dict[str, Any], ctx: SwarmBuildContext) -> list[Any]:
    """Build the co-scribe toolkit for a team member.

    **An unattended turn gets nothing, and that is the important line here.**

    On the single-agent path these tools come with a second thing attached: the session's
    ability set is stripped to a closed allowlist, so an unattended turn -- one a comment
    triggered, with nobody watching -- cannot reach bash or anything else outside a short
    list. That stripping is imperative, it acts on a live ``ability_manager``, and there
    is no equivalent on this path: a provider returns elements and never touches the
    agent's abilities.

    Today the question is moot, because the watcher dispatches to a single-agent session
    (§4.3.6) and a team turn is always someone typing. But "moot today" is how the
    review's worst findings started: a rule holding because no caller reaches it is not
    a rule, and the day someone points the watcher at a team, this factory would hand a
    closed-set turn an open tool set with nothing raising.

    So it refuses instead. If the closed set cannot be enforced here, the tools that need
    it are not built here.

    Args:
        params: Provider params carrying the ``clouddoc`` config section.
        ctx: The per-member build context.

    Returns:
        The co-scribe tool instances, or an empty list when the capability is skipped.
    """
    inp = CloudDocToolsInput.resolve(params, ctx)
    cfg = inp.clouddoc_config or {}
    if not cfg.get("enabled"):
        return []

    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import (
        CLOUDDOC_CHANNEL_ID,
    )

    # Decided by channel id, the same rule the single-agent path uses, and read off the
    # build context rather than a contextvar: the context is what this path is given,
    # and the alternative predicate lives in the server runtime, which nothing under
    # ``agents/swarm`` should have to import.
    if inp.channel_id == CLOUDDOC_CHANNEL_ID:
        logger.warning(
            "[swarm.clouddoc] 无人值守回合走到了 team 装配路径：该路径无法收窄能力集，"
            "因此拒绝提供 co-scribe 工具。无人值守回合应由 watcher 派发到单 agent 会话。"
        )
        return []

    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import (
        read_connection_specs,
    )

    specs = read_connection_specs(cfg)
    if not specs:
        logger.info("[swarm.clouddoc] skipped: no configured connection")
        return []

    try:
        from jiuwenswarm.extensions.co_scribe.backend.toolkit.clouddoc_tools import (
            CloudDocToolkit,
        )
        from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.factory import (
            build_provider,
        )
    except ImportError:
        # The clouddoc extras are absent. A missing dependency must not stop a team
        # member from being built.
        logger.warning("[swarm.clouddoc] extras 未安装，跳过工具装配")
        return []

    def _live_specs() -> list[dict]:
        """Re-read the connections on every call.

        Closing over ``specs`` looks equivalent and is not: the panel adopts documents
        at any moment and writes them into the config, and a snapshot taken at member
        build time would leave a document invisible to the agent while the panel lists
        it on screen.
        """
        from jiuwenswarm.common.config import get_config

        return read_connection_specs(get_config().get("clouddoc") or {})

    # A team turn is a person talking, so it reaches every connection's documents,
    # routed by adoption -- the same helper the chat path uses, so the two attended
    # hosts cannot drift apart again.
    try:
        from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.routing import (
            all_adopted_documents,
            build_routed_provider,
        )

        provider, _ = build_routed_provider(
            specs, build=build_provider, live_specs=_live_specs, log=logger,
            agent_roster=tuple(str(x) for x in (cfg.get("agent_roster") or [])),
        )
    except Exception as exc:  # noqa: BLE001 - a corrupt key must not end member setup
        logger.warning("[swarm.clouddoc] provider 初始化失败：%s", exc)
        return []
    try:
        from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.kinds import prime_provider_kinds

        prime_provider_kinds(provider, [d for sp in specs for d in (sp.get("documents") or [])])
    except Exception:  # noqa: BLE001 - priming must not stop member setup
        pass

    try:
        from jiuwenswarm.extensions.co_scribe.backend.toolkit.receipts import ReceiptStore

        # D21: direct mode carries no receipts by design; the other modes do.
        if str(cfg.get("mode") or "mandate").strip().lower() != "direct":
            provider.receipt_sink = ReceiptStore()
    except Exception as exc:  # noqa: BLE001
        # Attended and ask-gated here, so a missing sink is tolerated; the unattended
        # direct-apply path refuses without one (IC-2) and never reaches this factory.
        logger.warning("[swarm.clouddoc] receipt sink 不可用：%s", exc)

    try:
        harness_mode = str(cfg.get("mode") or "mandate").strip().lower()
        if harness_mode not in ("mandate", "recorded", "direct"):
            harness_mode = "mandate"
        toolkit = CloudDocToolkit(
            provider,
            harness_mode=harness_mode,
            rail_overrides=cfg.get("rail"),
            # No turn document and no turn comment: this path is only ever a person
            # talking, which is the chat shape. The tools ask the user which document
            # they mean, using the watched list below.
            watched_docs=lambda: all_adopted_documents(_live_specs),
            # Routed across every connection, so the "partial list" note never applies.
            connection_count=lambda: 1,
            workmode_file=str(cfg.get("workmode_file") or ""),
        )
        from jiuwenswarm.extensions.co_scribe.backend.clouddoc_bridge import (
            to_openjiuwen,
        )

        tools = to_openjiuwen(list(toolkit.get_tools()))
        logger.info("[swarm.clouddoc] built %d co-scribe tools", len(tools))
        return tools
    except Exception as exc:  # noqa: BLE001
        logger.warning("[swarm.clouddoc] toolkit construction failed: %s", exc)
        return []


__all__ = [
    "CLOUDDOC_TOOLS",
    "CRON_TOOLS",
    "SEND_FILE",
    "build_clouddoc_tools",
    "build_cron_tools",
    "build_send_file_tools",
]
