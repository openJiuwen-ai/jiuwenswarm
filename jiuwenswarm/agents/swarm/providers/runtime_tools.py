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
from jiuwenswarm.agents.harness.common.tools.session_messaging_toolkit import (
    SessionMessagingToolkit,
)
from jiuwenswarm.agents.harness.common.tools.file_delivery_policy import is_send_file_enabled
from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.common.cron_session import is_cron_execution_context

logger = logging.getLogger(__name__)

# Provider name constants; namespaced under the shared "swarm." prefix.
CRON_TOOLS = "swarm.cron_tools"
SEND_FILE = "swarm.send_file"
SESSION_MESSAGING_TOOLS = "swarm.session_messaging_tools"
ORGANIZATION_MODEL_TOOLS = "swarm.organization_model_tools"
ORGANIZATION_OWNER_INBOX_TOOLS = "swarm.organization_owner_inbox_tools"


@harness_element(kind=ElementKind.TOOL, name=ORGANIZATION_OWNER_INBOX_TOOLS,
                 description="Owner-only durable result acknowledgement.")
def build_organization_owner_inbox_tools(params: dict[str, Any], ctx: SwarmBuildContext) -> list[Any]:
    from jiuwenswarm.agents.harness.team.expert_org.inbox_tool import OrgAckOwnerResultTool

    if str(ctx.role) != "leader":
        return []
    return [OrgAckOwnerResultTool(ctx.team_id, ctx.session_id)]


@harness_element(kind=ElementKind.TOOL, name=ORGANIZATION_MODEL_TOOLS,
                 description="Leader-only persistent expert-Team model control.")
def build_organization_model_tools(params: dict[str, Any], ctx: SwarmBuildContext) -> list[Any]:
    from jiuwenswarm.agents.harness.team.expert_org.model_tool import OrgSetTeamModelTool

    if str(ctx.role) != "leader":
        return []
    return [OrgSetTeamModelTool(ctx.team_id, ctx.session_id)]


@harness_element(
    kind=ElementKind.TOOL,
    name=SESSION_MESSAGING_TOOLS,
    description="Leader-only durable cross-Session messaging toolkit.",
)
def build_session_messaging_tools(
    params: dict[str, Any],
    ctx: SwarmBuildContext,
) -> list[Any]:
    """Build Session messaging tools only for the Team leader."""
    _ = params
    if str(getattr(ctx, "role", "") or "") != "leader":
        return []
    return list(SessionMessagingToolkit().get_tools())


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
    session_metadata: dict[str, Any] | None = None
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
            # The same read feeds the cron gate; on failure the gate falls back
            # to request signals and may re-expose cron tools, so log it.
            logger.warning(
                "[swarm.cron_tools] session metadata lookup failed; cron gate "
                "falls back to request signals session_id=%s: %s",
                inp.session_id,
                exc,
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
    # cron 执行会话全面禁止操作 cron：成员工具集整体不下发（连 list/get
    # 等只读管理也不暴露，与单 Agent 链路 _ensure_cron_tools_registered 的
    # 全面下架对齐），心跳工具同样不挂（见 member_rails 的 swarm.heartbeat）。
    #
    # 判定必须包含会话里持久化的 ``cron_id``：Web 定时任务的执行会话由
    # ``_allocate_execution_session`` 以 web 渠道创建，会话 ID 是普通的
    # ``web_<ts>_<rand>``，既没有 ``cron_`` 前缀也不是 ``__cron__`` 渠道；
    # 调度器的 ``request_metadata["cron"]`` 只存在于触发那一轮请求上。用户
    # 进入该执行会话继续对话、会话运行时被回收后重建时，请求上没有任何
    # cron 标记，若再只看请求信号，成员会重新拿到完整的 cron 工具集，
    # ``cron_create_job`` 就能在定时任务会话里再建任务（issue #6188）。
    # 单 Agent 链路一直通过 ``_is_cron_execution_session`` 读这份元数据，
    # team 链路这里必须对齐，见 ``common/cron_session.py``。
    #
    # 远端成员可能读不到该会话的元数据，leader 会在 bootstrap 时把归属作为
    # ``cron_session`` 转发过来；它与 ``cron`` 分开，避免把"本轮由调度器
    # 触发"的语义带给审批/A4P 等消费者，见 remote_member_bootstrap。
    cron_execution_session = is_cron_execution_context(
        inp.session_id,
        channel_id=inp.channel_id,
        request_metadata=inp.request_metadata,
        session_metadata=session_metadata,
    )
    if cron_execution_session:
        logger.info(
            "[swarm.cron_tools] skip cron tools for cron execution session "
            "agent_id=%s",
            agent_id,
        )
        return []
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


__all__ = [
    "CRON_TOOLS",
    "SEND_FILE",
    "SESSION_MESSAGING_TOOLS",
    "ORGANIZATION_MODEL_TOOLS",
    "build_organization_model_tools",
    "build_cron_tools",
    "build_send_file_tools",
    "build_session_messaging_tools",
]
