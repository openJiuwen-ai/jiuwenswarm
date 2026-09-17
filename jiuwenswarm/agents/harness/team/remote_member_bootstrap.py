# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Wrap team spawn_teammate so remote blank claws receive a bootstrap envelope.

The leader LLM calls ``spawn_teammate``. For member names listed under
``team.metadata.jiuwen_remote_member_names`` (or all members when
``jiuwen_remote_all_spawn_members`` is true), after a successful DB insert we
deliver a direct bootstrap envelope so a teammate process registered in A2X can
apply runtime hints (transport topology, leader id, etc.).

Security: payload intentionally avoids DB credentials; it only mirrors
messager-facing fields already shared for pyzmq coordination.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shutil
import socket
import types
import uuid
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)

# team.metadata.jiuwen_remote_member_names: str | list[str]
_METADATA_REMOTE_NAMES_KEY = "jiuwen_remote_member_names"
_WRAPPED_ATTR = "_jiuwen_spawn_teammate_remote_bootstrap_wrapped"
_WRAPPED_TEAM_AGENT_ATTR = "_jiuwen_spawn_teammate_remote_bootstrap_team_agent"
_WRAPPED_SESSION_ID_ATTR = "_jiuwen_spawn_teammate_remote_bootstrap_session_id"
_WRAPPED_CHANNEL_ID_ATTR = "_jiuwen_spawn_teammate_remote_bootstrap_channel_id"
_WRAPPED_REMOTE_NAMES_ATTR = "_jiuwen_spawn_teammate_remote_bootstrap_remote_names"
_WRAPPED_REMOTE_ALL_ATTR = "_jiuwen_spawn_teammate_remote_bootstrap_remote_all"
_LOCAL_SPAWN_GUARD_ATTR = "_jiuwen_distributed_local_spawn_guard_attached"
_SEND_MESSAGE_GUARDED_ATTR = "_jiuwen_distributed_send_message_guarded"
_ACK_LISTENER_ATTR = "_jiuwen_remote_bootstrap_ack_listener_attached"
_SHUTDOWN_CLEANUP_WRAPPED_ATTR = "_jiuwen_shutdown_member_remote_cleanup_wrapped"
_SHUTDOWN_CLEANUP_SESSION_ID_ATTR = "_jiuwen_shutdown_member_remote_cleanup_session_id"
_SHUTDOWN_CLEANUP_CHANNEL_ID_ATTR = "_jiuwen_shutdown_member_remote_cleanup_channel_id"
_CLEAN_TEAM_TEARDOWN_WRAPPED_ATTR = "_jiuwen_clean_team_distributed_teardown_wrapped"
_CLEAN_TEAM_TEARDOWN_SESSION_ID_ATTR = "_jiuwen_clean_team_distributed_teardown_session_id"
_CLEAN_TEAM_TEARDOWN_CHANNEL_ID_ATTR = "_jiuwen_clean_team_distributed_teardown_channel_id"
_CLEAN_TEAM_TEARDOWN_TEAM_AGENT_ATTR = "_jiuwen_clean_team_distributed_teardown_team_agent"
_BUILD_TEAM_POST_HOOK_ATTR = "_jiuwen_build_team_post_tool_registration_hook_attached"
_METADATA_REMOTE_ALL_KEY = "jiuwen_remote_all_spawn_members"
_A2X_RESERVATIONS_BY_SESSION: dict[str, list[tuple[str, Any, dict[str, Any]]]] = {}
_SHUTDOWN_CLEANUP_TASKS: dict[str, asyncio.Task] = {}
_REMOTE_SHUTDOWN_TIMEOUT_SEC = 20.0
_REMOTE_SHUTDOWN_POLL_SEC = 0.25

# Remote claw → leader: JSON body on a normal team P2P message (DB + MESSAGE topic).
REMOTE_BOOTSTRAP_ACK_TYPE = "jiuwen.remote_bootstrap_ack"
REMOTE_BOOTSTRAP_DIRECT_EVENT_TYPE = "jiuwen.remote_teammate_bootstrap.direct"
REMOTE_TEAM_DESTROY_DIRECT_EVENT_TYPE = "jiuwen.remote_team_destroy.direct"
REMOTE_MEMBER_SHUTDOWN_DIRECT_EVENT_TYPE = "jiuwen.remote_member_shutdown.direct"
_TRANSPORT_BOOTSTRAP_DIRECT_ADDR_KEY = "bootstrap_direct_addr"
_TRANSPORT_BOOTSTRAP_KNOWN_PEERS_KEY = "bootstrap_known_peers"

_DYNAMIC_MEMBER_AGENTS: dict[tuple[str, str], Any] = {}
_DYNAMIC_MEMBER_INVOKE_TASKS: dict[tuple[str, str], asyncio.Task[Any]] = {}


class RemoteSpawnPrecheck(NamedTuple):
    """Result for remote spawn fail-fast validation."""

    error: str | None = None
    registry_reservation: Any | None = None


def remote_member_names(config_base: dict[str, Any] | None = None) -> set[str]:
    """Member slugs treated as externally-hosted teammates (post-spawn bootstrap)."""
    if config_base is None:
        from jiuwenswarm.common.config import get_config as _get_config

        config_base = _get_config()
    team = config_base.get("team") if isinstance(config_base.get("team"), dict) else {}
    meta = team.get("metadata") if isinstance(team.get("metadata"), dict) else {}
    raw = meta.get(_METADATA_REMOTE_NAMES_KEY)
    if raw is None:
        return set()
    if isinstance(raw, str) and raw.strip():
        return {raw.strip()}
    if isinstance(raw, list):
        return {str(x).strip() for x in raw if str(x).strip()}
    return set()


def remote_all_spawn_members(config_base: dict[str, Any] | None = None) -> bool:
    """Whether distributed leader treats every ``spawn_teammate`` as remote."""
    if config_base is None:
        from jiuwenswarm.common.config import get_config as _get_config

        config_base = _get_config()
    team = config_base.get("team") if isinstance(config_base.get("team"), dict) else {}
    runtime = team.get("runtime") if isinstance(team.get("runtime"), dict) else {}
    runtime_mode = str(runtime.get("mode", "")).strip().lower()
    if runtime_mode == "distributed":
        # In distributed mode, prefer remote takeover by default.
        raw = team.get("metadata") if isinstance(team.get("metadata"), dict) else {}
        if isinstance(raw, dict) and _METADATA_REMOTE_ALL_KEY in raw:
            return bool(raw.get(_METADATA_REMOTE_ALL_KEY))
        return True
    return False


def _is_distributed_leader_runtime(config_base: dict[str, Any]) -> bool:
    team = config_base.get("team") if isinstance(config_base.get("team"), dict) else {}
    runtime = team.get("runtime") if isinstance(team.get("runtime"), dict) else {}
    mode = str(runtime.get("mode", "")).strip().lower()
    role = str(runtime.get("role", "")).strip().lower()
    return mode == "distributed" and role == "leader"


def resolve_team_lifecycle(team_agent: Any) -> str:
    """Return team lifecycle from TeamAgent spec (temporary / persistent)."""
    spec = getattr(team_agent, "spec", None)
    if spec is not None:
        lifecycle = str(getattr(spec, "lifecycle", "") or "").strip().lower()
        if lifecycle:
            return lifecycle
    lifecycle = str(getattr(team_agent, "lifecycle", "") or "").strip().lower()
    if lifecycle:
        return lifecycle
    return "persistent"


def _team_agent_deep_agent(team_agent: Any) -> Any | None:
    """Return the underlying DeepAgent across openjiuwen TeamAgent variants."""
    deep_agent = getattr(team_agent, "deep_agent", None)
    if deep_agent is not None:
        return deep_agent

    harness = getattr(team_agent, "harness", None)
    if harness is None:
        return None

    deep_agent = getattr(harness, "inner_agent", None)
    if deep_agent is not None:
        return deep_agent

    return getattr(harness, "_deep_agent", None)


def _team_agent_messager(team_agent: Any | None) -> Any | None:
    """Return a TeamAgent messager across pre/post agent-core refactors."""
    if team_agent is None:
        return None
    for attr in ("_messager", "mailbox_transport"):
        messager = getattr(team_agent, attr, None)
        if messager is not None:
            return messager
    infra = getattr(team_agent, "infra", None)
    messager = getattr(infra, "messager", None) if infra is not None else None
    if messager is not None:
        return messager
    configurator = getattr(team_agent, "_configurator", None)
    return getattr(configurator, "messager", None) if configurator is not None else None


def _team_tool_card_matches(cid: str, name: str, tool_name: str, *, expected: str) -> bool:
    """Return True when an ability card id/name matches a team tool lookup."""
    if name == tool_name:
        return True
    if cid == tool_name or cid.startswith(f"{tool_name}_"):
        return True
    return cid == expected or cid.startswith(f"{expected}.")


def _team_tool_id(leader_deep_agent: Any, tool_name: str) -> str:
    """Resolve a registered team tool id by public tool name."""
    expected = f"team.{tool_name}"
    try:
        for card in leader_deep_agent.ability_manager.list() or []:
            cid = getattr(card, "id", "") or ""
            name = getattr(card, "name", "") or ""
            if _team_tool_card_matches(cid, name, tool_name, expected=expected):
                return cid or expected
    except Exception as exc:
        logger.warning("[RemoteMemberBootstrap] %s tool id resolve failed: %s", tool_name, exc)
    return expected


def _messager_bootstrap_dict(team_agent: Any) -> dict[str, Any]:
    ctx = team_agent.runtime_context
    if ctx is None or ctx.messager_config is None:
        return {}
    try:
        return ctx.messager_config.model_dump(mode="json")
    except Exception as exc:
        logger.warning("[RemoteMemberBootstrap] messager_config dump failed: %s", exc)
        return {}


def _transport_params_from_config(config_base: dict[str, Any] | None = None) -> dict[str, Any]:
    if config_base is None:
        from jiuwenswarm.common.config import get_config as _get_config

        config_base = _get_config()
    team = config_base.get("team") if isinstance(config_base.get("team"), dict) else {}
    transport = team.get("transport") if isinstance(team.get("transport"), dict) else {}
    params = transport.get("params") if isinstance(transport.get("params"), dict) else {}
    return params if isinstance(params, dict) else {}


def _resolve_bootstrap_peer_for_member(member_name: str, config_base: dict[str, Any] | None = None) -> tuple[str, str]:
    """Resolve (agent_id, addr) for bootstrap control-plane message."""
    params = _transport_params_from_config(config_base)
    requested = str(member_name or "").strip()

    def _iter_peers(key: str) -> list[dict[str, Any]]:
        raw = params.get(key)
        if isinstance(raw, list):
            return [x for x in raw if isinstance(x, dict)]
        return []

    def _pick(peers: list[dict[str, Any]]) -> tuple[str, str]:
        if not peers:
            return "", ""
        for peer in peers:
            alias = str(peer.get("member_name", "")).strip()
            if requested and alias and alias == requested:
                agent_id = str(peer.get("agent_id", "")).strip()
                addrs = peer.get("addrs") if isinstance(peer.get("addrs"), list) else []
                addr = _normalize_leader_direct_addr(addrs[0]) if addrs else ""
                if agent_id and addr:
                    return agent_id, addr
        for peer in peers:
            agent_id = str(peer.get("agent_id", "")).strip()
            addrs = peer.get("addrs") if isinstance(peer.get("addrs"), list) else []
            addr = _normalize_leader_direct_addr(addrs[0]) if addrs else ""
            if requested and requested == agent_id and addr:
                return agent_id, addr
        first = peers[0]
        agent_id = str(first.get("agent_id", "")).strip()
        addrs = first.get("addrs") if isinstance(first.get("addrs"), list) else []
        addr = _normalize_leader_direct_addr(addrs[0]) if addrs else ""
        return (agent_id, addr) if agent_id and addr else ("", "")

    agent_id, addr = _pick(_iter_peers(_TRANSPORT_BOOTSTRAP_KNOWN_PEERS_KEY))
    if agent_id and addr:
        return agent_id, addr
    return _pick(_iter_peers("known_peers"))


def _team_name_for_agent(team_agent: Any) -> str:
    team_name_fn = getattr(team_agent, "_team_name", None)
    if callable(team_name_fn):
        try:
            name = team_name_fn()
            if isinstance(name, str) and name.strip():
                return name.strip()
        except Exception as exc:
            logger.debug("[RemoteMemberBootstrap] _team_name lookup failed: %s", exc)
    tb = getattr(team_agent, "team_backend", None)
    name = getattr(tb, "team_name", "") if tb is not None else ""
    return str(name or "").strip()


async def _existing_team_member(team_agent: Any | None, member_name: str) -> Any | None:
    if team_agent is None:
        return None
    tb = getattr(team_agent, "team_backend", None)
    if tb is None:
        return None
    get_member = getattr(tb, "get_member", None)
    if not callable(get_member):
        return None
    try:
        return await get_member(member_name)
    except TypeError:
        team_name = _team_name_for_agent(team_agent)
        if not team_name:
            return None
        return await get_member(member_name, team_name)


async def _team_agent_for_session(
    session_id: str,
    *,
    channel_id: str = "default",
) -> Any | None:
    """Resolve the auxiliary team agent used on a teammate process for ``session_id``."""
    sid = str(session_id or "").strip()
    if not sid:
        return None
    try:
        from openjiuwen.agent_teams.context import reset_session_id, set_session_id

        from jiuwenswarm.agents.harness.team.team_manager import get_team_manager
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.get_instance()
        agent_manager = server.get_agent_manager()
        host = agent_manager.get_agent_nowait(channel_id) or await agent_manager.get_agent(
            channel_id,
            "agent",
        )
        if host is None:
            return None
        deep_agent = await host.ensure_instance()
        if deep_agent is None:
            return None
        token = set_session_id(sid)
        try:
            return await get_team_manager(channel_id).get_or_create_team(
                sid,
                deep_agent,
                channel_id=channel_id,
            )
        finally:
            reset_session_id(token)
    except Exception as exc:
        logger.debug(
            "[RemoteMemberBootstrap] team agent lookup failed session_id=%s channel=%s: %s",
            sid,
            channel_id,
            exc,
        )
        return None


async def _member_status_for_session(
    session_id: str,
    member_name: str,
    *,
    channel_id: str = "default",
) -> str | None:
    """Return member status from shared DB, or None when lookup is unavailable."""
    try:
        team_agent = await _team_agent_for_session(session_id, channel_id=channel_id)
        if team_agent is None:
            return None
        existing = await _existing_team_member(team_agent, member_name)
        if existing is None:
            return None
        return str(getattr(existing, "status", "") or "").strip().lower()
    except Exception as exc:
        logger.debug(
            "[RemoteMemberBootstrap] member status lookup unavailable session_id=%s member=%s: %s",
            session_id,
            member_name,
            exc,
        )
        return None


def _resolve_member_status_updater(db: Any) -> Any | None:
    """Return ``TeamDatabase.member.update_member_status`` only."""
    if db is None:
        return None
    member_dao = getattr(db, "member", None)
    if member_dao is not None:
        update = getattr(member_dao, "update_member_status", None)
        if callable(update):
            return update
    return None


async def _update_member_status_for_session(
    session_id: str,
    member_name: str,
    status: str,
    *,
    channel_id: str = "default",
) -> bool:
    team_agent = await _team_agent_for_session(session_id, channel_id=channel_id)
    if team_agent is None:
        return False
    tb = getattr(team_agent, "team_backend", None)
    db = getattr(tb, "db", None) if tb is not None else None
    update = _resolve_member_status_updater(db)
    if not callable(update):
        logger.warning(
            "[RemoteMemberBootstrap] member status updater unavailable session_id=%s member=%s",
            session_id,
            member_name,
        )
        return False
    team_name = _team_name_for_agent(team_agent)
    if not team_name:
        return False
    try:
        return bool(await update(member_name, team_name, status))
    except Exception as exc:
        logger.warning(
            "[RemoteMemberBootstrap] member status update failed session_id=%s member=%s status=%s: %s",
            session_id,
            member_name,
            status,
            exc,
        )
        return False


async def _cancel_remote_bootstrap_kickoff_tasks(
    session_id: str,
    member_name: str,
    kickoff_tasks: set[asyncio.Task[Any]] | None,
    *,
    loop_kicked_members: set[tuple[str, str]] | None = None,
) -> None:
    sid = str(session_id or "").strip()
    member = str(member_name or "").strip()
    if loop_kicked_members is not None and sid and member:
        loop_kicked_members.discard((sid, member))
    if not kickoff_tasks:
        return
    task_prefix = f"remote-bootstrap-kickoff:{sid}:"
    for task in list(kickoff_tasks):
        task_name = task.get_name()
        if not task_name.startswith(task_prefix):
            continue
        if not task_name.endswith(f":{member}"):
            continue
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def _validate_remote_spawn_inputs(inputs: dict[str, Any] | None) -> str | None:
    data = inputs or {}
    member_name = data.get("member_name")
    if not isinstance(member_name, str) or not member_name.strip():
        return "member_name is required for remote spawn"
    display_name = data.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip():
        return "display_name is required for remote spawn"
    desc = data.get("desc")
    if desc is None or not str(desc).strip():
        return "desc is required for remote spawn"
    return None


async def _release_registry_reservation(
    reservation: Any | None,
    *,
    member_name: str,
    reason: str,
) -> None:
    if reservation is None:
        return
    logger.warning(
        "[RemoteMemberBootstrap] releasing A2X reservation member=%s service_id=%s endpoint=%s reason=%s",
        member_name,
        getattr(reservation, "service_id", ""),
        getattr(reservation, "endpoint", ""),
        reason,
    )
    for method_name in ("release", "close"):
        method = getattr(reservation, method_name, None)
        if not callable(method):
            continue
        try:
            result = method()
            if hasattr(result, "__await__"):
                await result
        except Exception as exc:
            logger.warning(
                "[RemoteMemberBootstrap] A2X reservation %s failed member=%s: %s",
                method_name,
                member_name,
                exc,
            )


async def precheck_and_reserve_remote_spawn(
    member_name: str,
    config_base: dict[str, Any] | None = None,
    *,
    team_agent: Any | None = None,
) -> RemoteSpawnPrecheck:
    """Reserve a blank remote teammate before spawn_teammate mutates the team DB."""
    key = str(member_name or "").strip()
    if not key:
        return RemoteSpawnPrecheck(error="Remote member name is required.")

    existing = await _existing_team_member(team_agent, key)
    if existing is not None:
        team_name = _team_name_for_agent(team_agent) if team_agent is not None else ""
        suffix = f" in team {team_name}" if team_name else ""
        return RemoteSpawnPrecheck(error=f"Remote member {key} already exists{suffix}.")

    if config_base is None:
        from jiuwenswarm.common.config import get_config as _get_config

        config_base = _get_config()

    try:
        from jiuwenswarm.agents.harness.team.a2x.a2x_registry_runtime import reserve_blank_teammate_agent

        reservation = await reserve_blank_teammate_agent(
            config_base,
            source="leader-spawn-precheck",
        )
    except Exception as exc:
        logger.warning(
            "[RemoteMemberBootstrap] A2X blank teammate precheck failed member=%s: %s",
            key,
            exc,
        )
        return RemoteSpawnPrecheck(
            error=f"Failed to reserve blank teammate for remote member {key}: {exc}"
        )

    if reservation is None:
        return RemoteSpawnPrecheck(
            error=f"No blank teammate available to host remote member {key}."
        )
    return RemoteSpawnPrecheck(registry_reservation=reservation)


def _normalize_leader_agent_id(raw: Any, *, team_name: str, leader_member_name: str) -> str:
    """Return a non-empty, stable leader peer id for teammate route registration."""
    value = str(raw or "").strip()
    if value and value.lower() not in {"none", "null"}:
        return value
    tname = str(team_name or "").strip() or "jiuwen_team"
    lname = str(leader_member_name or "").strip() or "team_leader"
    return f"{tname}_{lname}"


def _normalize_leader_direct_addr(raw: Any) -> str:
    """Normalize leader direct addr to a connectable host for remote teammate."""
    value = str(raw or "").strip()
    if not value:
        return ""
    # direct_addr is often configured as bind addr 0.0.0.0; peers must dial a real host.
    return re.sub(r"^tcp://0\.0\.0\.0(?=[:/]|$)", "tcp://127.0.0.1", value)


def build_bootstrap_ack_envelope(
    *,
    member_name: str,
    team_name: str | None = None,
    leader_agent_id: str | None = None,
    leader_direct_addr: str | None = None,
    handshake_applied: bool | None = None,
    version: int = 1,
) -> dict[str, Any]:
    """Payload for a teammate→leader message after remote bootstrap is applied (optional team_name)."""
    body: dict[str, Any] = {
        "type": REMOTE_BOOTSTRAP_ACK_TYPE,
        "version": version,
        "member_name": member_name.strip(),
    }
    if team_name:
        body["team_name"] = team_name
    if leader_agent_id:
        body["leader_agent_id"] = leader_agent_id
    if leader_direct_addr:
        body["leader_direct_addr"] = leader_direct_addr
    if isinstance(handshake_applied, bool):
        body["handshake_applied"] = handshake_applied
    return body


def parse_remote_bootstrap_ack_json(content: str) -> dict[str, Any] | None:
    """If ``content`` is a valid ACK JSON envelope, return the dict; else None."""
    if not isinstance(content, str) or not content.strip():
        return None
    try:
        data = json.loads(content.strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("type") != REMOTE_BOOTSTRAP_ACK_TYPE:
        return None
    if int(data.get("version", 1)) != 1:
        return None
    mn = data.get("member_name")
    if not isinstance(mn, str) or not mn.strip():
        return None
    return data


def _swarm_assembly_hint(team_agent: Any) -> dict[str, str]:
    """Extract provider-assembly hints (mode / project_dir) from a leader spec.

    Reads the serializable ``build_context_seed`` the swarm enrichment leaves on
    the spec, falling back to the live ``build_context``. Returns an empty dict
    when the team was built via the legacy customizer path, so the teammate
    keeps the legacy rebuild.
    """
    spec = getattr(team_agent, "spec", None)
    if spec is None:
        return {}
    seed = getattr(spec, "build_context_seed", None)
    if isinstance(seed, dict) and seed:
        mode = str(seed.get("mode") or "").strip()
        project_dir = str(seed.get("project_dir") or "").strip()
    else:
        build_context = getattr(spec, "build_context", None)
        mode = str(getattr(build_context, "mode", "") or "").strip()
        project_dir = str(getattr(build_context, "project_dir", "") or "").strip()
    hint: dict[str, str] = {}
    if mode:
        hint["mode"] = mode
    if project_dir:
        hint["project_dir"] = project_dir
    return hint


def build_bootstrap_envelope(
    team_agent: Any,
    *,
    session_id: str,
    member_name: str,
    prompt: str | None,
) -> dict[str, Any]:
    spec = team_agent.spec
    ctx = team_agent.runtime_context
    team_spec = ctx.team_spec if ctx else None
    team_name = (team_spec.team_name if team_spec else None) or (spec.team_name if spec else "")
    leader_member_name = (team_spec.leader_member_name if team_spec else None) or (
        spec.leader.member_name if spec and spec.leader else None
    )
    messager = _messager_bootstrap_dict(team_agent)
    leader_agent_id = _normalize_leader_agent_id(
        messager.get("node_id"),
        team_name=team_name,
        leader_member_name=str(leader_member_name or ""),
    )
    leader_direct_addr = _normalize_leader_direct_addr(messager.get("direct_addr"))
    envelope = {
        "type": "jiuwen.remote_teammate_bootstrap",
        "version": 1,
        "bootstrap_id": str(uuid.uuid4()),
        "team_name": team_name,
        "session_id": str(session_id or "").strip(),
        "member_name": member_name,
        "leader_member_name": leader_member_name,
        "leader_agent_id": leader_agent_id,
        "leader_direct_addr": leader_direct_addr,
        "messager": messager,
        "prompt": prompt or "",
    }
    # Carry provider-assembly hints so the remote teammate rebuilds with the
    # same request mode / project dir (empty dict for the legacy path).
    envelope.update(_swarm_assembly_hint(team_agent))
    return envelope


def build_team_destroy_envelope(
    team_agent: Any,
    *,
    session_id: str,
    member_name: str,
    reservation: Any | None = None,
) -> dict[str, Any]:
    """Payload sent from leader to a remote teammate before team teardown."""
    spec = getattr(team_agent, "spec", None)
    ctx = getattr(team_agent, "runtime_context", None)
    team_spec = getattr(ctx, "team_spec", None) if ctx else None
    team_name = (getattr(team_spec, "team_name", None) if team_spec else None) or (
        getattr(spec, "team_name", "") if spec else ""
    )
    leader_member_name = (getattr(team_spec, "leader_member_name", None) if team_spec else None) or (
        spec.leader.member_name if spec and getattr(spec, "leader", None) else None
    )
    messager = _messager_bootstrap_dict(team_agent)
    leader_agent_id = _normalize_leader_agent_id(
        messager.get("node_id"),
        team_name=team_name,
        leader_member_name=str(leader_member_name or ""),
    )
    body = {
        "type": "jiuwen.remote_team_destroy",
        "version": 1,
        "destroy_id": str(uuid.uuid4()),
        "team_name": team_name,
        "session_id": str(session_id or "").strip(),
        "member_name": str(member_name or "").strip(),
        "leader_member_name": leader_member_name,
        "leader_agent_id": leader_agent_id,
    }
    if reservation is not None:
        body["registry"] = {
            "dataset": str(getattr(reservation, "dataset", "") or "").strip(),
            "service_id": str(getattr(reservation, "service_id", "") or "").strip(),
            "endpoint": _normalize_leader_direct_addr(getattr(reservation, "endpoint", "")),
        }
    return body


def _apply_leader_route_from_envelope(team_agent: Any, envelope: dict[str, Any]) -> bool:
    """Best-effort dynamic route registration so blank teammate can reply to leader."""
    leader_agent_id = str(envelope.get("leader_agent_id", "")).strip()
    leader_direct_addr = _normalize_leader_direct_addr(envelope.get("leader_direct_addr"))
    if (
        not leader_agent_id
        or leader_agent_id.lower() in {"none", "null"}
        or not leader_direct_addr
    ):
        logger.warning(
            "[RemoteMemberBootstrap] teammate leader route skipped: missing route fields "
            "leader_agent_id=%s raw_leader_direct_addr=%s normalized_leader_direct_addr=%s",
            leader_agent_id,
            envelope.get("leader_direct_addr"),
            leader_direct_addr,
        )
        return False
    messager = _team_agent_messager(team_agent)
    register = getattr(messager, "register_peer", None)
    if not callable(register):
        logger.warning(
            "[RemoteMemberBootstrap] teammate leader route skipped: register_peer unavailable "
            "leader_agent_id=%s leader_direct_addr=%s has_messager=%s messager_type=%s",
            leader_agent_id,
            leader_direct_addr,
            messager is not None,
            type(messager).__name__ if messager is not None else None,
        )
        return False
    try:
        from openjiuwen.agent_teams.messager.base import MessagerPeerConfig

        register(MessagerPeerConfig(agent_id=leader_agent_id, addrs=[leader_direct_addr]))
        return True
    except Exception as exc:
        logger.warning(
            "[RemoteMemberBootstrap] teammate failed to apply leader route agent_id=%s addr=%s: %s",
            leader_agent_id,
            leader_direct_addr,
            exc,
        )
        return False


async def _send_bootstrap_via_raw_zmq(
    *,
    peer_addr: str,
    envelope: dict[str, Any],
    member_name: str,
    peer_agent_id: str,
    timeout_s: float = 20.0,
    event_type: str = REMOTE_BOOTSTRAP_DIRECT_EVENT_TYPE,
    log_label: str = "bootstrap",
    id_field: str = "bootstrap_id",
) -> bool:
    """Send a control-plane event directly to a teammate bootstrap ROUTER endpoint."""
    try:
        import zmq
        import zmq.asyncio
    except Exception as exc:
        logger.warning("[RemoteMemberBootstrap] raw ZMQ %s unavailable: %s", log_label, exc)
        return False

    addr = _normalize_leader_direct_addr(peer_addr)
    if not addr:
        return False

    ctx = zmq.asyncio.Context.instance()
    sock = ctx.socket(zmq.DEALER)
    sock.setsockopt(zmq.LINGER, 0)
    try:
        sock.connect(addr)
        payload = {
            "event_type": event_type,
            "payload": {"envelope": envelope},
            "sender_id": str(envelope.get("leader_member_name") or ""),
        }
        await sock.send_multipart([json.dumps(payload).encode("utf-8")])
        frames = await asyncio.wait_for(sock.recv_multipart(), timeout=max(0.2, timeout_s))
        if not any(frame == b"ok" for frame in frames):
            logger.warning(
                "[RemoteMemberBootstrap] raw ZMQ %s unexpected ACK member=%s "
                "peer_agent_id=%s peer_addr=%s frames=%s",
                log_label,
                member_name,
                peer_agent_id,
                addr,
                frames,
            )
            return False
        return True
    except asyncio.TimeoutError:
        logger.warning(
            "[RemoteMemberBootstrap] raw ZMQ %s timed out member=%s "
            "peer_agent_id=%s peer_addr=%s timeout=%.1fs",
            log_label,
            member_name,
            peer_agent_id,
            addr,
            timeout_s,
        )
        return False
    except Exception as exc:
        logger.warning(
            "[RemoteMemberBootstrap] raw ZMQ %s failed member=%s peer_agent_id=%s "
            "peer_addr=%s: %s",
            log_label,
            member_name,
            peer_agent_id,
            addr,
            exc,
        )
        return False
    finally:
        sock.close(linger=0)


async def _send_bootstrap_message(
    team_agent: Any,
    session_id: str,
    member_name: str,
    prompt: str | None,
    *,
    registry_reservation: Any | None = None,
) -> bool:
    messager = _team_agent_messager(team_agent)
    envelope = build_bootstrap_envelope(
        team_agent,
        session_id=session_id,
        member_name=member_name,
        prompt=prompt,
    )
    peer_agent_id = ""
    peer_addr = ""
    delivery_path = ""
    if registry_reservation is not None:
        peer_agent_id = registry_reservation.service_id
        peer_addr = _normalize_leader_direct_addr(registry_reservation.endpoint)
        envelope["a2x_dataset"] = getattr(registry_reservation, "dataset", "")
        envelope["a2x_service_id"] = getattr(registry_reservation, "service_id", "")
    else:
        try:
            from jiuwenswarm.common.config import get_config as _get_config
            from jiuwenswarm.agents.harness.team.a2x.a2x_registry_runtime import reserve_blank_teammate_agent

            registry_reservation = await reserve_blank_teammate_agent(
                _get_config(),
                source="leader-spawn-member",
            )
            if registry_reservation is not None:
                peer_agent_id = registry_reservation.service_id
                peer_addr = _normalize_leader_direct_addr(registry_reservation.endpoint)
                envelope["a2x_dataset"] = getattr(registry_reservation, "dataset", "")
                envelope["a2x_service_id"] = getattr(registry_reservation, "service_id", "")
        except Exception as exc:
            logger.warning("[RemoteMemberBootstrap] A2X blank teammate reservation failed: %s", exc)

    if not peer_agent_id or not peer_addr:
        peer_agent_id, peer_addr = _resolve_bootstrap_peer_for_member(member_name)

    direct_sent = False
    direct_send_error = None
    if messager is not None and peer_agent_id and peer_addr:
        try:
            from openjiuwen.agent_teams.messager.base import MessagerPeerConfig
            from openjiuwen.agent_teams.schema.events import EventMessage

            register = getattr(messager, "register_peer", None)
            send = getattr(messager, "send", None)
            if callable(register) and callable(send):
                register(MessagerPeerConfig(agent_id=peer_agent_id, addrs=[peer_addr]))
                control_event = EventMessage(
                    event_type=REMOTE_BOOTSTRAP_DIRECT_EVENT_TYPE,
                    payload={"envelope": envelope},
                    sender_id=str(envelope.get("leader_member_name") or ""),
                )
                await send(peer_agent_id, control_event)
                direct_sent = True
                delivery_path = "messager"
        except Exception as exc:
            direct_send_error = exc
            logger.warning(
                "[RemoteMemberBootstrap] direct bootstrap send failed member=%s peer_agent_id=%s peer_addr=%s: %s",
                member_name,
                peer_agent_id,
                peer_addr,
                exc,
            )
    if (not direct_sent) and peer_agent_id and peer_addr:
        try:
            direct_sent = await _send_bootstrap_via_raw_zmq(
                peer_addr=peer_addr,
                envelope=envelope,
                member_name=member_name,
                peer_agent_id=peer_agent_id,
            )
            if direct_sent:
                delivery_path = "raw_zmq"
        except Exception as exc:
            logger.warning(
                "[RemoteMemberBootstrap] raw ZMQ bootstrap send failed member=%s "
                "peer_agent_id=%s peer_addr=%s: %s",
                member_name,
                peer_agent_id,
                peer_addr,
                exc,
            )
    if direct_sent:
        if registry_reservation is not None:
            remember_a2x_reservation(
                team_agent,
                session_id,
                member_name,
                registry_reservation,
            )
        logger.info(
            "[RemoteMemberBootstrap] remote bootstrap delivered member=%s session_id=%s "
            "peer_agent_id=%s peer_addr=%s path=%s bootstrap_id=%s",
            member_name,
            session_id,
            peer_agent_id,
            peer_addr,
            delivery_path or "unknown",
            envelope.get("bootstrap_id"),
        )
        return True

    if registry_reservation is not None:
        logger.warning(
            "[RemoteMemberBootstrap] releasing A2X reservation after bootstrap delivery failure "
            "member=%s service_id=%s endpoint=%s",
            member_name,
            registry_reservation.service_id,
            registry_reservation.endpoint,
        )
        await registry_reservation.release()
        await registry_reservation.close()

    logger.warning(
        "[RemoteMemberBootstrap] direct bootstrap not delivered; DB fallback disabled "
        "member=%s peer_agent_id=%s peer_addr=%s has_messager=%s send_error=%s",
        member_name,
        peer_agent_id,
        peer_addr,
        bool(messager is not None),
        direct_send_error,
    )
    return False


async def send_bootstrap_message(
    team_agent: Any,
    session_id: str,
    member_name: str,
    prompt: str | None,
    **kwargs: Any,
) -> bool:
    return await _send_bootstrap_message(
        team_agent,
        session_id,
        member_name,
        prompt,
        registry_reservation=kwargs.get("registry_reservation"),
    )


def remember_a2x_reservation(
    team_agent: Any,
    session_id: str,
    member_name: str,
    reservation: Any,
) -> None:
    """Track an A2X registry reservation for later :func:`release_a2x_reservations_for_session`."""
    key = str(session_id or "").strip()
    if not key:
        logger.warning(
            "[RemoteMemberBootstrap] skip A2X reservation tracking: missing session_id member=%s",
            member_name,
        )
        return
    destroy_envelope = build_team_destroy_envelope(
        team_agent,
        session_id=key,
        member_name=member_name,
        reservation=reservation,
    )
    reservations = _A2X_RESERVATIONS_BY_SESSION.setdefault(key, [])
    reservations.append((member_name, reservation, destroy_envelope))
    logger.info(
        "[RemoteMemberBootstrap] tracked A2X reservation session_id=%s member=%s service_id=%s endpoint=%s",
        key,
        member_name,
        getattr(reservation, "service_id", ""),
        getattr(reservation, "endpoint", ""),
    )


async def _notify_reserved_teammate_control_plane(
    *,
    team_agent: Any | None,
    member_name: str,
    reservation: Any,
    envelope: dict[str, Any],
    event_type: str,
    log_label: str,
    id_field: str,
    success_message: str,
    failure_message: str,
) -> bool:
    messager = _team_agent_messager(team_agent)
    peer_agent_id = str(getattr(reservation, "service_id", "") or "").strip()
    peer_addr = _normalize_leader_direct_addr(getattr(reservation, "endpoint", ""))
    if not peer_agent_id or not peer_addr:
        logger.debug(
            "[RemoteMemberBootstrap] skip %s notify member=%s peer_agent_id=%s peer_addr=%s",
            log_label,
            member_name,
            peer_agent_id,
            peer_addr,
        )
        return False
    delivered = False
    try:
        from openjiuwen.agent_teams.messager.base import MessagerPeerConfig
        from openjiuwen.agent_teams.schema.events import EventMessage

        register = getattr(messager, "register_peer", None)
        send = getattr(messager, "send", None)
        if messager is not None and callable(register) and callable(send):
            register(MessagerPeerConfig(agent_id=peer_agent_id, addrs=[peer_addr]))
            control_event = EventMessage(
                event_type=event_type,
                payload={"envelope": envelope},
                sender_id=str(envelope.get("leader_member_name") or ""),
            )
            await send(peer_agent_id, control_event)
            delivered = True
    except Exception as exc:
        logger.warning(failure_message, member_name, peer_agent_id, peer_addr, exc)
    if not delivered:
        delivered = await _send_bootstrap_via_raw_zmq(
            peer_addr=peer_addr,
            envelope=envelope,
            member_name=member_name,
            peer_agent_id=peer_agent_id,
            event_type=event_type,
            log_label=log_label,
            id_field=id_field,
        )
    if delivered:
        logger.info(
            success_message,
            member_name,
            peer_agent_id,
            peer_addr,
            envelope.get(id_field),
        )
    return bool(delivered)


async def notify_remote_member_shutdown_finalize(
    team_agent: Any,
    session_id: str,
    member_name: str,
    *,
    force: bool = False,
) -> bool:
    """Ask a reserved remote teammate to cancel bootstrap kickoff and mark SHUTDOWN."""
    key = str(session_id or "").strip()
    member = str(member_name or "").strip()
    if not key or not member:
        return False
    spec = getattr(team_agent, "spec", None)
    ctx = getattr(team_agent, "runtime_context", None)
    team_spec = getattr(ctx, "team_spec", None) if ctx else None
    team_name = (getattr(team_spec, "team_name", None) if team_spec else None) or (
        getattr(spec, "team_name", "") if spec else ""
    )
    leader_member_name = (getattr(team_spec, "leader_member_name", None) if team_spec else None) or (
        spec.leader.member_name if spec and getattr(spec, "leader", None) else None
    )
    messager = _messager_bootstrap_dict(team_agent)
    leader_agent_id = _normalize_leader_agent_id(
        messager.get("node_id"),
        team_name=team_name,
        leader_member_name=str(leader_member_name or ""),
    )
    envelope = {
        "type": "jiuwen.remote_member_shutdown",
        "version": 1,
        "shutdown_id": str(uuid.uuid4()),
        "team_name": team_name,
        "session_id": key,
        "member_name": member,
        "leader_member_name": leader_member_name,
        "leader_agent_id": leader_agent_id,
        "force": bool(force),
    }
    for name, reservation, _ in _A2X_RESERVATIONS_BY_SESSION.get(key, []):
        if name != member:
            continue
        return await _notify_reserved_teammate_control_plane(
            team_agent=team_agent,
            member_name=member,
            reservation=reservation,
            envelope=envelope,
            event_type=REMOTE_MEMBER_SHUTDOWN_DIRECT_EVENT_TYPE,
            log_label="member shutdown",
            id_field="shutdown_id",
            success_message=(
                "[RemoteMemberBootstrap] notified remote teammate to finalize shutdown "
                "member=%s service_id=%s endpoint=%s shutdown_id=%s"
            ),
            failure_message=(
                "[RemoteMemberBootstrap] remote teammate shutdown notify failed "
                "member=%s service_id=%s endpoint=%s: %s"
            ),
        )
    logger.debug(
        "[RemoteMemberBootstrap] no A2X reservation for shutdown finalize session_id=%s member=%s",
        key,
        member,
    )
    return False


async def release_a2x_reservations_for_session(
    session_id: str,
    *,
    team_agent: Any | None = None,
) -> None:
    """Notify reserved teammates for a distributed session teardown and close registry clients."""
    from jiuwenswarm.common.config import get_config as _get_config

    if not _is_distributed_leader_runtime(_get_config()):
        logger.debug(
            "[RemoteMemberBootstrap] non-distributed leader runtime; skip A2X reservation release "
            "session_id=%s",
            session_id,
        )
        return

    key = str(session_id or "").strip()
    reservations = _A2X_RESERVATIONS_BY_SESSION.pop(key, [])
    if not reservations:
        logger.debug("[RemoteMemberBootstrap] no A2X reservations to release session_id=%s", key)
        return

    for member_name, reservation, destroy_envelope in reservations:
        await _notify_reserved_teammate_control_plane(
            team_agent=team_agent,
            member_name=member_name,
            reservation=reservation,
            envelope=destroy_envelope,
            event_type=REMOTE_TEAM_DESTROY_DIRECT_EVENT_TYPE,
            log_label="team destroy",
            id_field="destroy_id",
            success_message=(
                "[RemoteMemberBootstrap] notified remote teammate to restore blank state before team destroy "
                "member=%s service_id=%s endpoint=%s destroy_id=%s"
            ),
            failure_message=(
                "[RemoteMemberBootstrap] remote teammate team destroy notify failed "
                "member=%s service_id=%s endpoint=%s: %s"
            ),
        )
        close = getattr(reservation, "close", None)
        if callable(close):
            await close()
    logger.info(
        "[RemoteMemberBootstrap] released A2X reservations session_id=%s count=%s",
        key,
        len(reservations),
    )


def attach_build_team_post_tool_registration_hook(
    team_agent: Any,
    *,
    session_id: str,
    channel_id: str | None,
) -> None:
    """Defer spawn_teammate wrapper until leader tools exist after build_team.

    Runner-owned leader tools (spawn_teammate, send_message, etc.) are registered
    only once ``TeamBackend.build_team`` completes. Hooking at runtime_ready is too
    early; this wrapper attaches the remote bootstrap patch immediately after a
    successful build_team call.
    """
    from jiuwenswarm.common.config import get_config as _get_config

    config_base = _get_config()
    if not _is_distributed_leader_runtime(config_base):
        logger.debug(
            "[RemoteMemberBootstrap] non-distributed leader runtime; skip build_team post hook"
        )
        return

    from openjiuwen.agent_teams.schema.team import TeamRole

    if getattr(team_agent, "role", None) != TeamRole.LEADER:
        return

    team_backend = getattr(team_agent, "team_backend", None)
    if team_backend is None:
        logger.warning(
            "[RemoteMemberBootstrap] skip build_team post hook: missing team_backend "
            "session_id=%s channel=%s",
            session_id,
            channel_id,
        )
        return
    if getattr(team_backend, _BUILD_TEAM_POST_HOOK_ATTR, False):
        return

    orig_build_team = getattr(team_backend, "build_team", None)
    if not callable(orig_build_team):
        logger.warning(
            "[RemoteMemberBootstrap] skip build_team post hook: team_backend.build_team not callable "
            "session_id=%s channel=%s",
            session_id,
            channel_id,
        )
        return

    async def wrapped_build_team(*args: Any, **kwargs: Any) -> Any:
        result = await orig_build_team(*args, **kwargs)
        attach_spawn_teammate_remote_bootstrap_wrapper(
            team_agent,
            session_id=session_id,
            channel_id=channel_id,
        )
        attach_shutdown_member_remote_cleanup_wrapper(
            team_agent,
            session_id=session_id,
            channel_id=channel_id,
        )
        attach_clean_team_distributed_teardown_wrapper(
            team_agent,
            session_id=session_id,
            channel_id=channel_id,
        )
        return result

    team_backend.build_team = wrapped_build_team
    setattr(team_backend, _BUILD_TEAM_POST_HOOK_ATTR, True)


def attach_spawn_teammate_remote_bootstrap_wrapper(
    team_agent: Any,
    *,
    session_id: str,
    channel_id: str | None,
) -> None:
    """Monkey-patch SpawnTeammateTool.invoke on the leader's registered tool instance."""
    from jiuwenswarm.common.config import get_config as _get_config

    config_base = _get_config()
    if not _is_distributed_leader_runtime(config_base):
        logger.debug("[RemoteMemberBootstrap] non-distributed leader runtime; skip spawn_teammate wrapper")
        return

    from openjiuwen.agent_teams.schema.team import TeamRole
    from openjiuwen.core.runner import Runner

    if getattr(team_agent, "role", None) != TeamRole.LEADER:
        return
    leader = _team_agent_deep_agent(team_agent)
    if leader is None:
        logger.debug("[RemoteMemberBootstrap] skip spawn_teammate wrapper: missing leader DeepAgent")
        return

    tool_id = _team_tool_id(leader, "spawn_teammate")
    tag = getattr(getattr(leader, "card", None), "id", None)
    tool = Runner.resource_mgr.get_tool(tool_id, tag=tag) if tag else None
    if tool is None:
        tool = Runner.resource_mgr.get_tool(tool_id)
    if tool is None:
        logger.warning(
            "[RemoteMemberBootstrap] tool %s not in Runner.resource_mgr (session_id=%s channel=%s)",
            tool_id,
            session_id,
            channel_id,
        )
        return
    remote_names = remote_member_names(config_base)
    remote_all = remote_all_spawn_members(config_base)
    if not remote_names and not remote_all:
        logger.debug("[RemoteMemberBootstrap] no jiuwen_remote_member_names; skip wrapper")
        return

    setattr(tool, _WRAPPED_TEAM_AGENT_ATTR, team_agent)
    setattr(tool, _WRAPPED_SESSION_ID_ATTR, session_id)
    setattr(tool, _WRAPPED_CHANNEL_ID_ATTR, channel_id)
    setattr(tool, _WRAPPED_REMOTE_NAMES_ATTR, set(remote_names))
    setattr(tool, _WRAPPED_REMOTE_ALL_ATTR, remote_all)
    if getattr(tool, _WRAPPED_ATTR, False):
        return

    orig_invoke = tool.invoke

    async def wrapped_invoke(self: Any, inputs: dict[str, Any], **kwargs: Any) -> Any:
        from openjiuwen.harness.tools.base_tool import ToolOutput

        registry_reservation = None
        key = ""
        active_team_agent = getattr(self, _WRAPPED_TEAM_AGENT_ATTR, team_agent)
        active_session_id = str(
            getattr(self, _WRAPPED_SESSION_ID_ATTR, session_id) or session_id
        ).strip() or session_id
        try:
            mname = (inputs or {}).get("member_name")
            if not isinstance(mname, str):
                return await orig_invoke(inputs, **kwargs)

            key = mname.strip()
            active_remote_names = getattr(self, _WRAPPED_REMOTE_NAMES_ATTR, remote_names)
            if not isinstance(active_remote_names, set):
                active_remote_names = set(active_remote_names or [])
            active_remote_all = bool(getattr(self, _WRAPPED_REMOTE_ALL_ATTR, remote_all))
            if (not active_remote_all) and key not in active_remote_names:
                return await orig_invoke(inputs, **kwargs)

            precheck = await precheck_and_reserve_remote_spawn(
                key,
                config_base,
                team_agent=active_team_agent,
            )
            registry_reservation = precheck.registry_reservation
            if precheck.error:
                return ToolOutput(success=False, error=precheck.error)
            validation_error = _validate_remote_spawn_inputs(inputs)
            if validation_error:
                await _release_registry_reservation(
                    registry_reservation,
                    member_name=key,
                    reason="invalid remote spawn inputs",
                )
                registry_reservation = None
                return ToolOutput(success=False, error=validation_error)

            result = await orig_invoke(inputs, **kwargs)
            ok = bool(getattr(result, "success", False))
            if not ok:
                await _release_registry_reservation(
                    registry_reservation,
                    member_name=key,
                    reason="spawn_teammate failed",
                )
                registry_reservation = None
                return result

            # SpawnTeammateTool already inserted the roster row via team_backend.spawn_member.
            # openjiuwen native spawn path may mark member as READY immediately.
            # For remote teammates, force it back to UNSTARTED and wait for ACK to set READY.
            try:
                from openjiuwen.agent_teams.schema.status import MemberStatus

                tb = getattr(active_team_agent, "team_backend", None)
                db = getattr(tb, "db", None) if tb is not None else None
                team_name = _team_name_for_agent(active_team_agent)
                update = _resolve_member_status_updater(db)
                if callable(update) and isinstance(team_name, str) and team_name.strip():
                    await update(key, team_name, MemberStatus.UNSTARTED.value)
            except Exception as exc:
                logger.warning(
                    "[RemoteMemberBootstrap] failed to force UNSTARTED before bootstrap member=%s: %s",
                    key,
                    exc,
                )

            try:
                delivered = await send_bootstrap_message(
                    active_team_agent,
                    active_session_id,
                    key,
                    (inputs or {}).get("prompt"),
                    registry_reservation=registry_reservation,
                )
                delivery_error = None
            except Exception as exc:
                logger.warning(
                    "[RemoteMemberBootstrap] bootstrap delivery raised member=%s: %s",
                    key,
                    exc,
                )
                delivered = False
                delivery_error = exc

            if delivered:
                registry_reservation = None
                return result

            try:
                from openjiuwen.agent_teams.schema.status import MemberStatus

                tb = getattr(active_team_agent, "team_backend", None)
                db = getattr(tb, "db", None) if tb is not None else None
                team_name = _team_name_for_agent(active_team_agent)
                update = _resolve_member_status_updater(db)
                if callable(update) and isinstance(team_name, str) and team_name.strip():
                    await update(key, team_name, MemberStatus.ERROR.value)
            except Exception as exc:
                logger.warning(
                    "[RemoteMemberBootstrap] failed to mark remote member ERROR after bootstrap failure "
                    "member=%s: %s",
                    key,
                    exc,
                )
            await _release_registry_reservation(
                registry_reservation,
                member_name=key,
                reason="bootstrap not delivered",
            )
            registry_reservation = None
            detail = f": {delivery_error}" if delivery_error is not None else ""
            return ToolOutput(
                success=False,
                error=f"Remote bootstrap not delivered for member {key}; member marked error{detail}.",
            )
        except Exception as exc:
            logger.warning("[RemoteMemberBootstrap] post-spawn hook failed: %s", exc)
            if key:
                try:
                    from openjiuwen.agent_teams.schema.status import MemberStatus

                    tb = getattr(active_team_agent, "team_backend", None)
                    db = getattr(tb, "db", None) if tb is not None else None
                    team_name = _team_name_for_agent(active_team_agent)
                    update = _resolve_member_status_updater(db)
                    if callable(update) and isinstance(team_name, str) and team_name.strip():
                        await update(key, team_name, MemberStatus.ERROR.value)
                except Exception as mark_exc:
                    logger.warning(
                        "[RemoteMemberBootstrap] failed to mark remote member ERROR after hook exception "
                        "member=%s: %s",
                        key,
                        mark_exc,
                    )
            await _release_registry_reservation(
                registry_reservation,
                member_name=key,
                reason="post-spawn hook exception",
            )
            return ToolOutput(
                success=False,
                error=f"Remote bootstrap failed for member {key or '<unknown>'}: {exc}",
            )

    tool.invoke = types.MethodType(wrapped_invoke, tool)
    setattr(tool, _WRAPPED_ATTR, True)


async def _ensure_remote_teammates_shutdown_before_clean_team(
    team_agent: Any,
    session_id: str,
    *,
    timeout: float = _REMOTE_SHUTDOWN_TIMEOUT_SEC,
) -> None:
    """Best-effort: remote teammates must reach SHUTDOWN before openjiuwen clean_team."""
    from openjiuwen.agent_teams.schema.status import MemberStatus

    import time

    sid = str(session_id or "").strip()
    if not sid:
        return
    tb = getattr(team_agent, "team_backend", None)
    list_members = getattr(tb, "list_members", None) if tb is not None else None
    if not callable(list_members):
        return

    spec = getattr(team_agent, "spec", None)
    leader_name = str(getattr(spec, "leader_member_name", "") or "").strip()
    if not leader_name and spec is not None:
        leader_spec = getattr(spec, "leader", None)
        leader_name = str(getattr(leader_spec, "member_name", "") or "").strip()

    async def _notify_stuck() -> None:
        members = await list_members()
        for member in members:
            name = str(getattr(member, "member_name", "") or "").strip()
            if not name or name == leader_name:
                continue
            status = str(getattr(member, "status", "") or "").strip().lower()
            if status == MemberStatus.SHUTDOWN.value:
                continue
            if status == MemberStatus.SHUTDOWN_REQUESTED.value:
                await notify_remote_member_shutdown_finalize(
                    team_agent,
                    sid,
                    name,
                )

    await _notify_stuck()
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        members = await list_members()
        stuck: list[Any] = []
        for member in members:
            name = str(getattr(member, "member_name", "") or "").strip()
            if not name or name == leader_name:
                continue
            status = str(getattr(member, "status", "") or "").strip().lower()
            if status != MemberStatus.SHUTDOWN.value:
                stuck.append(member)
        if not stuck:
            return
        await _notify_stuck()
        await asyncio.sleep(_REMOTE_SHUTDOWN_POLL_SEC)

    team_name = _team_name_for_agent(team_agent)
    db = getattr(tb, "db", None) if tb is not None else None
    update = _resolve_member_status_updater(db)
    if not callable(update) or not team_name:
        return
    for member in stuck:
        name = str(getattr(member, "member_name", "") or "").strip()
        status = str(getattr(member, "status", "") or "").strip().lower()
        if status != MemberStatus.SHUTDOWN_REQUESTED.value:
            continue
        logger.warning(
            "[RemoteMemberBootstrap] promoting stuck remote member to SHUTDOWN before clean_team "
            "session_id=%s member=%s",
            sid,
            name,
        )
        with contextlib.suppress(Exception):
            await update(name, team_name, MemberStatus.SHUTDOWN.value)


async def _all_teammates_shutdown_requested_or_done(team_agent: Any) -> bool:
    tb = getattr(team_agent, "team_backend", None)
    list_members = getattr(tb, "list_members", None) if tb is not None else None
    if not callable(list_members):
        logger.debug("[RemoteMemberBootstrap] shutdown cleanup skipped: missing public list_members")
        return False

    from openjiuwen.agent_teams.schema.status import MemberStatus

    closed_statuses = {
        MemberStatus.SHUTDOWN_REQUESTED.value,
        MemberStatus.SHUTDOWN.value,
    }

    members = await list_members()
    if not members:
        return False

    for member in members:
        status = str(getattr(member, "status", "") or "").strip().lower()
        if status not in closed_statuses:
            logger.debug(
                "[RemoteMemberBootstrap] shutdown cleanup waiting for member=%s status=%s",
                getattr(member, "member_name", None),
                status,
            )
            return False
    return True


def _schedule_shutdown_cleanup(session_id: str, channel_id: str | None) -> None:
    sid = str(session_id or "").strip()
    if not sid:
        return
    existing_task = _SHUTDOWN_CLEANUP_TASKS.get(sid)
    if existing_task is None or existing_task.done():
        _SHUTDOWN_CLEANUP_TASKS[sid] = asyncio.create_task(
            _delayed_shutdown_cleanup(sid, channel_id)
        )


async def _delayed_shutdown_cleanup(session_id: str, channel_id: str | None) -> bool:
    try:
        # Give the leader response a short chance to flush before stopping the team stream.
        await asyncio.sleep(2.0)
        return await run_pending_shutdown_cleanup_for_session(
            session_id,
            channel_id=channel_id,
        )
    finally:
        current_task = asyncio.current_task()
        if _SHUTDOWN_CLEANUP_TASKS.get(session_id) is current_task:
            _SHUTDOWN_CLEANUP_TASKS.pop(session_id, None)


async def wait_for_pending_shutdown_cleanup_for_session(
    session_id: str,
    *,
    timeout: float = _REMOTE_SHUTDOWN_TIMEOUT_SEC,
) -> bool:
    sid = str(session_id or "").strip()
    task = _SHUTDOWN_CLEANUP_TASKS.get(sid)
    if task is None:
        return False
    try:
        return bool(await asyncio.wait_for(asyncio.shield(task), timeout=timeout))
    except asyncio.TimeoutError:
        logger.warning(
            "[RemoteMemberBootstrap] timed out waiting for shutdown cleanup session_id=%s",
            sid,
        )
        return False


async def _push_shutdown_cleanup_notice(
    *,
    session_id: str,
    channel_id: str | None,
    deleted: bool,
) -> None:
    try:
        from jiuwenswarm.server.gateway_push import WebSocketGatewayPushTransport
        from jiuwenswarm.server.runtime.session.session_metadata import build_server_push_message

        request_id = f"team_shutdown_cleanup_{session_id}"
        content = (
            "团队已清空并解散，远端成员已恢复为空闲状态。"
            if deleted
            else "团队解散清理未完成，请查看后端日志确认原因。"
        )
        await WebSocketGatewayPushTransport().send_push(
            build_server_push_message(
                session_id=session_id,
                request_id=request_id,
                fallback_channel_id=channel_id,
                payload={
                    "event_type": "chat.final",
                    "request_id": request_id,
                    "session_id": session_id,
                    "content": content,
                },
            )
        )
    except Exception as exc:
        logger.warning(
            "[RemoteMemberBootstrap] shutdown cleanup notice push failed "
            "session_id=%s error=%s",
            session_id,
            exc,
        )


async def run_pending_shutdown_cleanup_for_session(
    session_id: str,
    channel_id: str | None = None,
) -> bool:
    """Delete a distributed team session after all teammates have been asked to shut down."""
    sid = str(session_id or "").strip()
    if not sid:
        return False
    active_channel_id = channel_id
    try:
        from jiuwenswarm.agents.harness.team import get_team_manager

        manager = get_team_manager(active_channel_id)
        deleted = await manager.delete_session_runtime(sid, reason="team.shutdown_all_members: ")
        logger.info(
            "[RemoteMemberBootstrap] post-stream shutdown cleanup finished "
            "session_id=%s deleted=%s",
            sid,
            deleted,
        )
        await _push_shutdown_cleanup_notice(
            session_id=sid,
            channel_id=active_channel_id,
            deleted=bool(deleted),
        )
        return bool(deleted)
    except Exception as exc:
        logger.warning(
            "[RemoteMemberBootstrap] post-stream shutdown cleanup failed session_id=%s error=%s",
            sid,
            exc,
        )
        await _push_shutdown_cleanup_notice(
            session_id=sid,
            channel_id=active_channel_id,
            deleted=False,
        )
        return False


def attach_shutdown_member_remote_cleanup_wrapper(
    team_agent: Any,
    *,
    session_id: str,
    channel_id: str | None,
) -> None:
    """Delete a distributed team session after every teammate has been shut down."""
    from jiuwenswarm.common.config import get_config as _get_config

    config_base = _get_config()
    if not _is_distributed_leader_runtime(config_base):
        logger.debug("[RemoteMemberBootstrap] non-distributed leader runtime; skip shutdown cleanup wrapper")
        return

    from openjiuwen.agent_teams.schema.team import TeamRole
    from openjiuwen.core.runner import Runner

    if getattr(team_agent, "role", None) != TeamRole.LEADER:
        return
    leader = _team_agent_deep_agent(team_agent)
    if leader is None:
        logger.debug("[RemoteMemberBootstrap] skip shutdown cleanup wrapper: missing leader DeepAgent")
        return

    tool_id = _team_tool_id(leader, "shutdown_member")
    tag = getattr(getattr(leader, "card", None), "id", None)
    tool = Runner.resource_mgr.get_tool(tool_id, tag=tag) if tag else None
    if tool is None:
        tool = Runner.resource_mgr.get_tool(tool_id)
    if tool is None:
        logger.debug(
            "[RemoteMemberBootstrap] tool %s not in Runner.resource_mgr; skip shutdown cleanup wrapper "
            "session_id=%s channel=%s",
            tool_id,
            session_id,
            channel_id,
        )
        return

    setattr(tool, _SHUTDOWN_CLEANUP_SESSION_ID_ATTR, session_id)
    setattr(tool, _SHUTDOWN_CLEANUP_CHANNEL_ID_ATTR, channel_id)
    setattr(tool, _WRAPPED_TEAM_AGENT_ATTR, team_agent)
    if getattr(tool, _SHUTDOWN_CLEANUP_WRAPPED_ATTR, False):
        return

    orig_invoke = tool.invoke

    async def wrapped_invoke(self: Any, inputs: dict[str, Any], **kwargs: Any) -> Any:
        result = await orig_invoke(inputs, **kwargs)
        try:
            if not bool(getattr(result, "success", False)):
                return result
            active_team_agent = getattr(self, _WRAPPED_TEAM_AGENT_ATTR, team_agent)
            active_session_id = str(
                getattr(self, _SHUTDOWN_CLEANUP_SESSION_ID_ATTR, session_id) or session_id
            ).strip() or session_id
            active_channel_id = getattr(self, _SHUTDOWN_CLEANUP_CHANNEL_ID_ATTR, channel_id)
            shutdown_inputs = inputs if isinstance(inputs, dict) else {}
            shutdown_member_name = str(shutdown_inputs.get("member_name", "")).strip()
            shutdown_force = bool(shutdown_inputs.get("force", False))
            lifecycle = resolve_team_lifecycle(active_team_agent)
            if shutdown_member_name and lifecycle == "temporary":
                notified = await notify_remote_member_shutdown_finalize(
                    active_team_agent,
                    active_session_id,
                    shutdown_member_name,
                    force=shutdown_force,
                )
                if notified:
                    import time
                    from openjiuwen.agent_teams.schema.status import MemberStatus

                    deadline = time.monotonic() + _REMOTE_SHUTDOWN_TIMEOUT_SEC
                    while time.monotonic() < deadline:
                        existing = await _existing_team_member(
                            active_team_agent,
                            shutdown_member_name,
                        )
                        if existing is not None and str(
                            getattr(existing, "status", "") or ""
                        ).strip().lower() == MemberStatus.SHUTDOWN.value:
                            break
                        await asyncio.sleep(_REMOTE_SHUTDOWN_POLL_SEC)
            if not await _all_teammates_shutdown_requested_or_done(active_team_agent):
                return result
            if lifecycle == "temporary":
                return result
            _schedule_shutdown_cleanup(active_session_id, active_channel_id)
        except Exception as exc:
            logger.warning("[RemoteMemberBootstrap] shutdown cleanup hook failed: %s", exc)
        return result

    tool.invoke = types.MethodType(wrapped_invoke, tool)
    setattr(tool, _SHUTDOWN_CLEANUP_WRAPPED_ATTR, True)


def attach_clean_team_distributed_teardown_wrapper(
    team_agent: Any,
    *,
    session_id: str,
    channel_id: str | None,
) -> None:
    """After leader clean_team succeeds, notify reserved remote teammates via control plane.

    Aligns distributed temporary teams with local inprocess flow: openjiuwen
    clean_team owns DB/filesystem teardown; jiuwenswarm releases A2X reservations
    so teammate processes drop dynamic runtimes and restore blank agent cards.
    """
    from jiuwenswarm.common.config import get_config as _get_config

    config_base = _get_config()
    if not _is_distributed_leader_runtime(config_base):
        logger.debug(
            "[RemoteMemberBootstrap] non-distributed leader runtime; skip clean_team teardown wrapper"
        )
        return

    from openjiuwen.agent_teams.schema.team import TeamRole
    from openjiuwen.core.runner import Runner

    if getattr(team_agent, "role", None) != TeamRole.LEADER:
        return
    if resolve_team_lifecycle(team_agent) != "temporary":
        logger.debug(
            "[RemoteMemberBootstrap] skip clean_team teardown wrapper: lifecycle=%s",
            resolve_team_lifecycle(team_agent),
        )
        return

    leader = _team_agent_deep_agent(team_agent)
    if leader is None:
        logger.debug("[RemoteMemberBootstrap] skip clean_team teardown wrapper: missing leader DeepAgent")
        return

    tool_id = _team_tool_id(leader, "clean_team")
    tag = getattr(getattr(leader, "card", None), "id", None)
    tool = Runner.resource_mgr.get_tool(tool_id, tag=tag) if tag else None
    if tool is None:
        tool = Runner.resource_mgr.get_tool(tool_id)
    if tool is None:
        logger.debug(
            "[RemoteMemberBootstrap] tool %s not in Runner.resource_mgr; skip clean_team teardown wrapper "
            "session_id=%s channel=%s",
            tool_id,
            session_id,
            channel_id,
        )
        return

    setattr(tool, _CLEAN_TEAM_TEARDOWN_SESSION_ID_ATTR, session_id)
    setattr(tool, _CLEAN_TEAM_TEARDOWN_CHANNEL_ID_ATTR, channel_id)
    setattr(tool, _CLEAN_TEAM_TEARDOWN_TEAM_AGENT_ATTR, team_agent)
    if getattr(tool, _CLEAN_TEAM_TEARDOWN_WRAPPED_ATTR, False):
        return

    orig_invoke = tool.invoke

    async def wrapped_invoke(self: Any, inputs: dict[str, Any], **kwargs: Any) -> Any:
        active_team_agent = getattr(self, _CLEAN_TEAM_TEARDOWN_TEAM_AGENT_ATTR, team_agent)
        active_session_id = str(
            getattr(self, _CLEAN_TEAM_TEARDOWN_SESSION_ID_ATTR, session_id) or session_id
        ).strip() or session_id
        try:
            await _ensure_remote_teammates_shutdown_before_clean_team(
                active_team_agent,
                active_session_id,
            )
        except Exception as exc:
            logger.warning(
                "[RemoteMemberBootstrap] pre-clean_team remote shutdown prepare failed: %s",
                exc,
            )
        result = await orig_invoke(inputs, **kwargs)
        try:
            if not bool(getattr(result, "success", False)):
                return result
            await release_a2x_reservations_for_session(
                active_session_id,
                team_agent=active_team_agent,
            )
            logger.info(
                "[RemoteMemberBootstrap] clean_team succeeded; released A2X reservations "
                "session_id=%s channel=%s",
                active_session_id,
                getattr(self, _CLEAN_TEAM_TEARDOWN_CHANNEL_ID_ATTR, channel_id),
            )
        except Exception as exc:
            logger.warning("[RemoteMemberBootstrap] clean_team teardown hook failed: %s", exc)
        return result

    tool.invoke = types.MethodType(wrapped_invoke, tool)
    setattr(tool, _CLEAN_TEAM_TEARDOWN_WRAPPED_ATTR, True)


def attach_distributed_local_spawn_guard(
    team_agent: Any,
    *,
    session_id: str,
    channel_id: str | None,
) -> None:
    """Disable leader-side teammate startup when teammates are remote-managed.

    Some agent-core versions accept ``spawn_mode=distributed`` in config but still
    wire ``send_message`` auto-start to local ``spawn_teammate``. In distributed
    leader mode, jiuwenswarm owns remote bootstrap, so local teammate creation must
    be suppressed at the adapter layer.
    """
    from jiuwenswarm.common.config import get_config as _get_config

    config_base = _get_config()
    if not _is_distributed_leader_runtime(config_base):
        logger.debug("[RemoteMemberBootstrap] non-distributed leader runtime; skip local spawn guard")
        return

    from openjiuwen.agent_teams.schema.team import TeamRole
    from openjiuwen.core.runner import Runner

    if getattr(team_agent, "role", None) != TeamRole.LEADER:
        return
    if getattr(team_agent, _LOCAL_SPAWN_GUARD_ATTR, False):
        return

    leader = _team_agent_deep_agent(team_agent)
    if leader is None:
        logger.debug("[RemoteMemberBootstrap] skip local spawn guard: missing leader DeepAgent")
        return

    tool_id = _team_tool_id(leader, "send_message")
    tag = getattr(getattr(leader, "card", None), "id", None)
    tool = Runner.resource_mgr.get_tool(tool_id, tag=tag) if tag else None
    if tool is None:
        tool = Runner.resource_mgr.get_tool(tool_id)
    if tool is None:
        logger.warning(
            "[RemoteMemberBootstrap] distributed local spawn guard could not find send_message tool "
            "tool_id=%s session_id=%s channel=%s",
            tool_id,
            session_id,
            channel_id,
        )
    elif not getattr(tool, _SEND_MESSAGE_GUARDED_ATTR, False):
        if hasattr(tool, "_on_teammate_created"):
            setattr(tool, "_on_teammate_created", None)
            setattr(tool, _SEND_MESSAGE_GUARDED_ATTR, True)
        else:
            logger.warning(
                "[RemoteMemberBootstrap] send_message tool has no _on_teammate_created field "
                "tool_id=%s type=%s",
                tool_id,
                type(tool).__name__,
            )

    original_spawn_teammate = getattr(team_agent, "spawn_teammate", None)
    if callable(original_spawn_teammate):

        async def _skip_local_spawn_teammate(self: Any, ctx: Any, *args: Any, **kwargs: Any) -> None:
            return None

        setattr(team_agent, "_jiuwen_original_spawn_teammate", original_spawn_teammate)
        team_agent.spawn_teammate = types.MethodType(_skip_local_spawn_teammate, team_agent)
    else:
        logger.warning("[RemoteMemberBootstrap] team_agent has no callable spawn_teammate to guard")

    setattr(team_agent, _LOCAL_SPAWN_GUARD_ATTR, True)


def attach_remote_bootstrap_ack_listener(
    team_agent: Any,
    *,
    session_id: str,
    channel_id: str | None = None,
) -> None:
    """Leader: on MESSAGE transport events, detect ACK JSON and set member UNSTARTED→READY in DB.

    The published :class:`MessageEvent` has no body; we load content via ``db.get_message``,
    then ``mark_message_read`` so the leader LLM is not fed the control payload.
    """
    from jiuwenswarm.common.config import get_config as _get_config

    config_base = _get_config()
    if not _is_distributed_leader_runtime(config_base):
        logger.debug("[RemoteMemberBootstrap] non-distributed leader runtime; skip ACK listener")
        return

    from openjiuwen.agent_teams.schema.events import TeamEvent
    from openjiuwen.agent_teams.schema.status import MemberStatus
    from openjiuwen.agent_teams.schema.team import TeamRole

    if getattr(team_agent, "role", None) != TeamRole.LEADER:
        return
    if getattr(team_agent, _ACK_LISTENER_ATTR, False):
        return
    tb = getattr(team_agent, "team_backend", None)
    mm = getattr(team_agent, "message_manager", None)
    if tb is None or mm is None or getattr(tb, "db", None) is None:
        logger.debug(
            "[RemoteMemberBootstrap] skip ACK listener: missing team_backend.db or message_manager",
        )
        return
    remote_names = remote_member_names(config_base)
    remote_all = remote_all_spawn_members(config_base)
    if not remote_names and not remote_all:
        logger.debug("[RemoteMemberBootstrap] no jiuwen_remote_member_names; skip ACK listener")
        return

    processed_message_ids: set[str] = set()

    async def on_event(event: Any) -> None:
        if getattr(event, "event_type", None) != TeamEvent.MESSAGE:
            return
        payload = getattr(event, "payload", None) or {}
        if not isinstance(payload, dict):
            return
        to_name = payload.get("to_member_name")
        from_name = payload.get("from_member_name")
        message_id = payload.get("message_id")
        _mn = getattr(team_agent, "_member_name", None)
        leader_name = _mn() if callable(_mn) else None
        if not leader_name or to_name != leader_name:
            return
        if not isinstance(from_name, str):
            return
        sender = from_name.strip()
        if not sender:
            return
        if (not remote_all) and sender not in remote_names:
            return
        if not isinstance(message_id, str) or not message_id:
            return
        if message_id in processed_message_ids:
            return

        row = await tb.db.get_message(message_id)
        if row is None:
            logger.debug("[RemoteMemberBootstrap] ACK: no row for message_id=%s", message_id)
            return
        if getattr(row, "from_member_name", None) != from_name or getattr(row, "to_member_name", None) != leader_name:
            logger.warning(
                "[RemoteMemberBootstrap] ACK: DB row sender/recipient mismatch id=%s",
                message_id,
            )
            return

        ack = parse_remote_bootstrap_ack_json(getattr(row, "content", "") or "")
        if ack is None:
            return
        ack_member = str(ack.get("member_name", "")).strip()
        if ack_member != sender:
            logger.warning(
                "[RemoteMemberBootstrap] ACK: member_name != sender for id=%s",
                message_id,
            )
            return
        _tn = getattr(team_agent, "_team_name", None)
        team_name = _tn() if callable(_tn) else None
        if not team_name:
            logger.warning("[RemoteMemberBootstrap] ACK: leader has no team_name")
            return
        ack_team = ack.get("team_name")
        if ack_team and str(ack_team) != str(team_name):
            logger.warning(
                "[RemoteMemberBootstrap] ACK: team_name mismatch db=%s ack=%s",
                team_name,
                ack_team,
            )
            return
        ack_applied = ack.get("handshake_applied")
        if isinstance(ack_applied, bool) and not ack_applied:
            logger.warning(
                "[RemoteMemberBootstrap] ACK: teammate reports bootstrap not fully applied member=%s id=%s",
                ack_member,
                message_id,
            )
            return
        existing_member = await _existing_team_member(team_agent, ack_member)
        if existing_member is None:
            try:
                await mm.mark_message_read(message_id, leader_name)
            except Exception as exc:
                logger.warning("[RemoteMemberBootstrap] ACK: mark_message_read failed: %s", exc)
            logger.warning(
                "[RemoteMemberBootstrap] ACK: no roster row for member=%s team=%s; skip READY update",
                ack_member,
                team_name,
            )
            return

        update = _resolve_member_status_updater(getattr(tb, "db", None))
        ok = (
            bool(await update(ack_member, team_name, MemberStatus.READY.value))
            if callable(update)
            else False
        )
        if not ok:
            logger.warning(
                "[RemoteMemberBootstrap] ACK: update_member_status failed member=%s team=%s",
                ack_member,
                team_name,
            )
            return
        try:
            await mm.mark_message_read(message_id, leader_name)
        except Exception as exc:
            logger.warning("[RemoteMemberBootstrap] ACK: mark_message_read failed: %s", exc)
        processed_message_ids.add(message_id)

    team_agent.add_event_listener(on_event)
    setattr(team_agent, _ACK_LISTENER_ATTR, True)


def _is_distributed_teammate_runtime(config_base: dict[str, Any]) -> bool:
    team = config_base.get("team") if isinstance(config_base.get("team"), dict) else {}
    runtime = team.get("runtime") if isinstance(team.get("runtime"), dict) else {}
    mode = str(runtime.get("mode", "")).strip().lower()
    role = str(runtime.get("role", "")).strip().lower()
    return mode == "distributed" and role == "teammate"


async def _send_bootstrap_ack_from_teammate(
    team_agent: Any,
    *,
    session_id: str,
    member_name: str,
    team_name: str,
    leader_member_name: str,
    leader_agent_id: str,
    leader_direct_addr: str,
    handshake_applied: bool,
) -> bool:
    """Send the remote-bootstrap ACK once teammate routing is configured."""
    mm = getattr(team_agent, "message_manager", None)
    send_message = getattr(mm, "send_message", None)
    if not callable(send_message):
        logger.warning(
            "[RemoteMemberBootstrap] teammate ACK skipped: message_manager unavailable "
            "session_id=%s member=%s",
            session_id,
            member_name,
        )
        return False
    ack = build_bootstrap_ack_envelope(
        member_name=member_name,
        team_name=team_name or _team_name_for_agent(team_agent),
        leader_agent_id=leader_agent_id,
        leader_direct_addr=leader_direct_addr,
        handshake_applied=handshake_applied,
    )
    from openjiuwen.agent_teams.context import reset_session_id, set_session_id

    token = set_session_id(session_id)
    try:
        await send_message(
            content=json.dumps(ack, ensure_ascii=False),
            to_member_name=leader_member_name,
        )
    finally:
        reset_session_id(token)
    logger.info(
        "[RemoteMemberBootstrap] teammate sent bootstrap ACK session_id=%s member=%s "
        "leader=%s handshake_applied=%s",
        session_id,
        member_name,
        leader_member_name,
        handshake_applied,
    )
    return True


async def _stop_team_agent_runtime(
    agent: Any,
    *,
    session_id: str,
    member_name: str,
    source: str,
) -> bool:
    """Stop a TeamAgent-like runtime without deleting team database state."""
    stopped = False
    messager = _team_agent_messager(agent)
    stop_coordination = getattr(agent, "_stop_coordination", None)
    if callable(stop_coordination):
        with contextlib.suppress(Exception):
            await stop_coordination()
            stopped = True
    stop_messager = getattr(messager, "stop", None)
    if callable(stop_messager):
        with contextlib.suppress(Exception):
            await stop_messager()
            stopped = True
    return stopped


async def _discard_auxiliary_team_agent(
    team_manager: Any,
    session_id: str,
    team_agent: Any,
) -> None:
    """Remove the bootstrap helper TeamAgent from TeamManager without cleaning DB rows."""
    agents = getattr(team_manager, "_team_agents", None)
    if isinstance(agents, dict) and agents.get(session_id) is team_agent:
        agents.pop(session_id, None)
    await _stop_team_agent_runtime(
        team_agent,
        session_id=session_id,
        member_name=str(getattr(team_agent, "member_name", None) or "bootstrap-helper"),
        source="bootstrap-helper",
    )


def _cleanup_auxiliary_leader_workspace(team_name: str, leader_member_name: str) -> None:
    """Remove the helper leader member workspace created in the teammate process."""
    team = str(team_name or "").strip()
    leader = str(leader_member_name or "").strip()
    if not team or not leader:
        return
    try:
        from openjiuwen.agent_teams.paths import team_home

        helper_workspace = team_home(team) / "workspaces" / f"{leader}_workspace"
        if not helper_workspace.exists():
            return
        shutil.rmtree(helper_workspace)
    except Exception as exc:
        logger.warning(
            "[RemoteMemberBootstrap] auxiliary leader workspace cleanup failed "
            "team=%s leader=%s error=%s",
            team,
            leader,
            exc,
        )


def _allocate_loopback_direct_addr() -> str:
    """Reserve a currently free loopback TCP port for a dynamic member ROUTER."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        _, port = sock.getsockname()
    return f"tcp://127.0.0.1:{port}"


def _retarget_teammate_direct_addr(ctx: Any, *, session_id: str, member_name: str) -> Any:
    """Avoid agent-core's default inprocess member direct_addr (usually 16000)."""
    cfg = getattr(ctx, "messager_config", None)
    if cfg is None:
        return ctx
    new_addr = _allocate_loopback_direct_addr()
    new_cfg = cfg.model_copy(update={"direct_addr": new_addr})
    new_ctx = ctx.model_copy(update={"messager_config": new_cfg})
    return new_ctx


async def _stop_dynamic_member_agent(session_id: str, member_name: str) -> bool:
    """Stop and forget a dynamically created remote teammate member runtime."""
    sid = str(session_id or "").strip()
    member = str(member_name or "").strip()
    if not sid or not member:
        return False
    key = (sid, member)
    task = _DYNAMIC_MEMBER_INVOKE_TASKS.pop(key, None)
    current_task = asyncio.current_task()
    if task is not None and task is not current_task and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    agent = _DYNAMIC_MEMBER_AGENTS.pop(key, None)
    if agent is None:
        return task is not None
    await _stop_team_agent_runtime(
        agent,
        session_id=sid,
        member_name=member,
        source="dynamic-member",
    )
    return True


async def _stop_dynamic_member_agents_for_session(session_id: str, member_name: str | None = None) -> int:
    """Stop dynamic member runtimes for a session, optionally narrowed to one member."""
    sid = str(session_id or "").strip()
    member = str(member_name or "").strip() if member_name else ""
    if not sid:
        return 0
    keys = [
        key
        for key in set(_DYNAMIC_MEMBER_AGENTS) | set(_DYNAMIC_MEMBER_INVOKE_TASKS)
        if key[0] == sid and (not member or key[1] == member)
    ]
    stopped_count = 0
    for key_sid, key_member in keys:
        if await _stop_dynamic_member_agent(key_sid, key_member):
            stopped_count += 1
    return stopped_count


async def finalize_remote_member_shutdown_on_teammate(
    session_id: str,
    member_name: str,
    *,
    channel_id: str = "default",
    force: bool = False,
    kickoff_tasks: set[asyncio.Task[Any]] | None = None,
    loop_kicked_members: set[tuple[str, str]] | None = None,
) -> bool:
    """Cancel delayed bootstrap kickoff, stop dynamic runtime, and mark member SHUTDOWN."""
    from openjiuwen.agent_teams.schema.status import MemberStatus

    sid = str(session_id or "").strip()
    member = str(member_name or "").strip()
    if not sid or not member:
        return False

    await _cancel_remote_bootstrap_kickoff_tasks(
        sid,
        member,
        kickoff_tasks,
        loop_kicked_members=loop_kicked_members,
    )
    await _stop_dynamic_member_agent(sid, member)

    current = await _member_status_for_session(sid, member, channel_id=channel_id)
    if current == MemberStatus.SHUTDOWN.value:
        return True
    if current == MemberStatus.SHUTDOWN_REQUESTED.value or force or current is None:
        updated = await _update_member_status_for_session(
            sid,
            member,
            MemberStatus.SHUTDOWN.value,
            channel_id=channel_id,
        )
        if updated:
            logger.info(
                "[RemoteMemberBootstrap] finalized remote member shutdown session_id=%s member=%s force=%s",
                sid,
                member,
                force,
            )
        return updated
    logger.debug(
        "[RemoteMemberBootstrap] skip finalize shutdown: unexpected status session_id=%s "
        "member=%s status=%s",
        sid,
        member,
        current,
    )
    return False


async def _initialize_team_agent_db(
    team_agent: Any,
    *,
    session_id: str,
    member: str,
    source: str,
) -> bool:
    """Ensure per-team DB tables (e.g. team_message_*) exist before message I/O."""
    backend = getattr(team_agent, "team_backend", None)
    db = getattr(backend, "db", None) if backend is not None else None
    initialize_db = getattr(db, "initialize", None)
    if not callable(initialize_db):
        logger.debug(
            "[RemoteMemberBootstrap] team db initialize skipped: no initialize() "
            "source=%s session_id=%s member=%s",
            source,
            session_id,
            member,
        )
        return False
    try:
        await initialize_db()
        return True
    except Exception as exc:
        logger.warning(
            "[RemoteMemberBootstrap] team db initialize failed source=%s session_id=%s member=%s: %s",
            source,
            session_id,
            member,
            exc,
        )
        return False


async def _ensure_dynamic_member_execution_loop(
    *,
    session_id: str,
    target_member: str,
    channel_id: str = "default",
    leader_agent_id: str = "",
    leader_direct_addr: str = "",
    card_replaced: bool = False,
    assembly_mode: str = "",
    assembly_project_dir: str = "",
) -> tuple[bool, bool]:
    """Best-effort bootstrap for teammate runtime loop after dynamic member takeover.

    When ``assembly_mode`` is set (provider-based assembly), the auxiliary leader
    is rebuilt provider-style so the serialized spec carries provider declarations
    plus the ``build_context_seed`` that the teammate uses to reconstruct its
    build context — no customizer closure crosses the process boundary.
    """
    sid = str(session_id or "").strip()
    member = str(target_member or "").strip()
    if not sid or not member:
        return False, False
    try:
        from openjiuwen.agent_teams.agent.team_agent import TeamAgent
        from openjiuwen.agent_teams.context import reset_session_id, set_session_id
        from openjiuwen.agent_teams.schema.status import MemberStatus

        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
        from jiuwenswarm.agents.harness.team.team_manager import get_team_manager

        current_status = await _member_status_for_session(sid, member, channel_id=channel_id)
        if current_status == MemberStatus.SHUTDOWN.value:
            return False, False
        if current_status == MemberStatus.SHUTDOWN_REQUESTED.value:
            await finalize_remote_member_shutdown_on_teammate(
                sid,
                member,
                channel_id=channel_id,
            )
            return False, False

        server = AgentWebSocketServer.get_instance()
        agent_manager = server.get_agent_manager()
        agent = agent_manager.get_agent_nowait(channel_id) or await agent_manager.get_agent(channel_id, "agent")
        if agent is None:
            logger.warning(
                "[RemoteMemberBootstrap] teammate loop start skipped: agent unavailable channel=%s session_id=%s",
                channel_id,
                sid,
            )
            return False, False
        deep_agent = await agent.ensure_instance()
        if deep_agent is None:
            logger.warning(
                "[RemoteMemberBootstrap] teammate loop start skipped: deep_agent unavailable session_id=%s",
                sid,
            )
            return False, False

        team_manager = get_team_manager(channel_id)
        request_metadata: dict[str, Any] | None = None
        if assembly_mode:
            request_metadata = {"mode": assembly_mode}
            if assembly_project_dir:
                request_metadata["project_dir"] = assembly_project_dir
        leader_team_agent = await team_manager.get_or_create_team(
            sid,
            deep_agent,
            channel_id=channel_id,
            request_metadata=request_metadata,
        )
        helper_token = set_session_id(sid)
        try:
            await _initialize_team_agent_db(
                leader_team_agent,
                session_id=sid,
                member=member,
                source="bootstrap-helper",
            )
            # Build a real TEAMMATE runtime context for the adopted member, instead
            # of using TeamManager.interact() (which drives the leader context).
            spawn_manager = getattr(leader_team_agent, "spawn_manager", None)
            build_ctx = getattr(spawn_manager, "build_context_from_db", None)
            if not callable(build_ctx):
                build_ctx = getattr(leader_team_agent, "_build_context_from_db", None)
            if not callable(build_ctx):
                logger.warning(
                    "[RemoteMemberBootstrap] teammate loop start skipped: build_context_from_db unavailable "
                    "session_id=%s member=%s",
                    sid,
                    member,
                )
                return False, False
            teammate_ctx = await build_ctx(member)
        finally:
            reset_session_id(helper_token)
        if teammate_ctx is None:
            logger.warning(
                "[RemoteMemberBootstrap] teammate loop start skipped: teammate context missing "
                "session_id=%s member=%s",
                sid,
                member,
            )
            return False, False
        if getattr(leader_team_agent, "spec", None) is None:
            logger.warning(
                "[RemoteMemberBootstrap] teammate loop start skipped: team spec unavailable session_id=%s",
                sid,
            )
            return False, False
        teammate_ctx = _retarget_teammate_direct_addr(teammate_ctx, session_id=sid, member_name=member)
        spec_obj = leader_team_agent.spec
        leader_member_name = str(getattr(spec_obj, "leader_member_name", "") or "").strip()
        if not leader_member_name:
            leader_spec = getattr(spec_obj, "leader", None)
            leader_member_name = str(getattr(leader_spec, "member_name", "") or "").strip()
        payload = {
            "spec": spec_obj.model_dump(mode="json"),
            "context": teammate_ctx.model_dump(mode="json"),
        }
        await _discard_auxiliary_team_agent(team_manager, sid, leader_team_agent)
        _cleanup_auxiliary_leader_workspace(
            str(getattr(spec_obj, "team_name", "") or ""),
            leader_member_name,
        )
        await _stop_dynamic_member_agent(sid, member)
        teammate_agent = await TeamAgent.from_spawn_payload(payload)
        _DYNAMIC_MEMBER_AGENTS[(sid, member)] = teammate_agent
        await _initialize_team_agent_db(
            teammate_agent,
            session_id=sid,
            member=member,
            source="dynamic-member",
        )
        route_applied = False
        if leader_agent_id and leader_direct_addr:
            route_applied = _apply_leader_route_from_envelope(
                teammate_agent,
                {
                    "leader_agent_id": leader_agent_id,
                    "leader_direct_addr": leader_direct_addr,
                },
            )
        await _send_bootstrap_ack_from_teammate(
            teammate_agent,
            session_id=sid,
            member_name=member,
            team_name=str(getattr(spec_obj, "team_name", "") or ""),
            leader_member_name=leader_member_name,
            leader_agent_id=leader_agent_id,
            leader_direct_addr=leader_direct_addr,
            handshake_applied=bool(route_applied and card_replaced),
        )
        kickoff = (
            f"[remote bootstrap] teammate adopted member={member}. "
            "Start/continue execution loop for assigned team tasks."
        )
        invoke_started_at = asyncio.get_running_loop().time()

        async def _run_invoke_loop() -> None:
            try:
                from openjiuwen.core.runner import Runner

                await Runner.run_agent_team(
                    teammate_agent,
                    {"query": kickoff},
                    member=True,
                    session=sid,
                )
            except Exception as exc:
                logger.warning(
                    "[RemoteMemberBootstrap] teammate invoke failed: session_id=%s member=%s channel_id=%s "
                    "elapsed_ms=%.0f error=%s",
                    sid,
                    member,
                    channel_id,
                    (asyncio.get_running_loop().time() - invoke_started_at) * 1000,
                    exc,
                    exc_info=True,
                )
            finally:
                if (sid, member) in _DYNAMIC_MEMBER_AGENTS:
                    await _stop_dynamic_member_agent(sid, member)

        invoke_task = asyncio.create_task(
            _run_invoke_loop(),
            name=f"remote-bootstrap-invoke:{sid}:{member}",
        )
        _DYNAMIC_MEMBER_INVOKE_TASKS[(sid, member)] = invoke_task

        def _on_invoke_done(task: asyncio.Task[Any]) -> None:
            if _DYNAMIC_MEMBER_INVOKE_TASKS.get((sid, member)) is task:
                _DYNAMIC_MEMBER_INVOKE_TASKS.pop((sid, member), None)

        invoke_task.add_done_callback(_on_invoke_done)
        logger.info(
            "[RemoteMemberBootstrap] teammate execution loop started session_id=%s member=%s "
            "channel=%s route_applied=%s card_replaced=%s",
            sid,
            member,
            channel_id,
            route_applied,
            card_replaced,
        )
        return True, route_applied
    except Exception as exc:
        logger.warning(
            "[RemoteMemberBootstrap] teammate execution loop kickoff failed: session_id=%s member=%s error=%s",
            sid,
            member,
            exc,
            exc_info=True,
        )
        return False, False


async def _replace_teammate_card_after_direct_bootstrap(
    *,
    channel_id: str,
    member_name: str,
) -> bool:
    """Replace this teammate's A2X card after direct control-plane bootstrap."""
    member = str(member_name or "").strip()
    if not member:
        return False
    try:
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
        from jiuwenswarm.agents.harness.team.a2x.a2x_registry_runtime import replace_teammate_agent_card_after_bootstrap

        server = AgentWebSocketServer.get_instance()
        agent_manager = server.get_agent_manager()
        agent = agent_manager.get_agent_nowait(channel_id) or await agent_manager.get_agent(channel_id, "agent")
        deep_agent = await agent.ensure_instance() if agent is not None else None
        if deep_agent is None:
            logger.warning(
                "[RemoteMemberBootstrap] teammate registry card replace skipped: deep_agent unavailable "
                "channel=%s member=%s",
                channel_id,
                member,
            )
            return False
        client = getattr(deep_agent, "_jiuwen_a2x_client", None)
        dataset = str(getattr(deep_agent, "_jiuwen_a2x_blank_dataset", "") or "").strip()
        service_id = str(getattr(deep_agent, "_jiuwen_a2x_blank_service_id", "") or "").strip()
        if client is None or not dataset or not service_id:
            logger.warning(
                "[RemoteMemberBootstrap] teammate registry card replace skipped: missing local A2X state "
                "channel=%s member=%s has_client=%s dataset=%s service_id=%s",
                channel_id,
                member,
                client is not None,
                dataset,
                service_id,
            )
            return False
        replaced = await replace_teammate_agent_card_after_bootstrap(
            client,
            dataset=dataset,
            service_id=service_id,
            member_name=member,
            source="teammate-direct-bootstrap",
        )
        if replaced:
            logger.info(
                "[RemoteMemberBootstrap] teammate registry card replaced after bootstrap "
                "channel=%s member=%s dataset=%s service_id=%s",
                channel_id,
                member,
                dataset,
                service_id,
            )
        return bool(replaced)
    except Exception as exc:
        logger.warning(
            "[RemoteMemberBootstrap] teammate registry card replace after direct bootstrap failed "
            "channel=%s member=%s: %s",
            channel_id,
            member,
            exc,
            exc_info=True,
        )
        return False


async def apply_bootstrap_envelope_from_control_plane(
    *,
    processed_ids: set[str],
    loop_kicked_members: set[tuple[str, str]],
    kickoff_tasks: set[asyncio.Task[Any]],
    adopted_member: str,
    envelope: dict[str, Any],
    source_id: str,
) -> str:
    """Handle leader bootstrap notification on a remote teammate process."""
    if not isinstance(envelope, dict):
        return adopted_member
    bootstrap_id = str(envelope.get("bootstrap_id", "")).strip() or source_id
    if bootstrap_id in processed_ids:
        return adopted_member
    envelope_team_name = str(envelope.get("team_name", "")).strip()
    envelope_session_id = str(envelope.get("session_id", "")).strip()
    if not envelope_team_name or not envelope_session_id:
        return adopted_member
    target_member = str(envelope.get("member_name", "")).strip()
    if not target_member:
        return adopted_member
    leader_agent_id = str(envelope.get("leader_agent_id", "")).strip()
    leader_direct_addr = str(envelope.get("leader_direct_addr", "")).strip()
    # Provider-assembly hints (empty for the legacy customizer path) so the
    # teammate's auxiliary leader is rebuilt provider-style with the same mode.
    assembly_mode = str(envelope.get("mode", "")).strip()
    assembly_project_dir = str(envelope.get("project_dir", "")).strip()

    logger.info(
        "[RemoteMemberBootstrap] teammate received direct bootstrap team=%s session_id=%s "
        "member=%s bootstrap_id=%s",
        envelope_team_name,
        envelope_session_id,
        target_member,
        bootstrap_id,
    )

    effective_sid = envelope_session_id
    loop_key = (effective_sid, target_member)

    from openjiuwen.agent_teams.schema.status import MemberStatus

    bootstrap_status = await _member_status_for_session(
        effective_sid,
        target_member,
        channel_id="default",
    )
    if bootstrap_status == MemberStatus.SHUTDOWN.value:
        processed_ids.add(bootstrap_id)
        return adopted_member
    if bootstrap_status == MemberStatus.SHUTDOWN_REQUESTED.value:
        processed_ids.add(bootstrap_id)
        return adopted_member
    card_replaced = await _replace_teammate_card_after_direct_bootstrap(
        channel_id="default",
        member_name=target_member,
    )
    if effective_sid and loop_key not in loop_kicked_members:
        loop_kicked_members.add(loop_key)

        async def _kickoff_loop() -> None:
            kicked, route_applied = await _ensure_dynamic_member_execution_loop(
                session_id=effective_sid,
                target_member=target_member,
                channel_id="default",
                leader_agent_id=leader_agent_id,
                leader_direct_addr=leader_direct_addr,
                card_replaced=card_replaced,
                assembly_mode=assembly_mode,
                assembly_project_dir=assembly_project_dir,
            )
            if kicked:
                logger.info(
                    "[RemoteMemberBootstrap] teammate execution kickoff scheduled from bootstrap "
                    "team=%s session_id=%s member=%s handshake_applied=%s card_replaced=%s",
                    envelope_team_name,
                    effective_sid,
                    target_member,
                    route_applied,
                    card_replaced,
                )
                return
            else:
                logger.warning(
                    "[RemoteMemberBootstrap] teammate execution kickoff failed after direct ACK "
                    "team=%s session_id=%s member=%s card_replaced=%s",
                    envelope_team_name,
                    effective_sid,
                    target_member,
                    card_replaced,
                )

        kickoff_task = asyncio.create_task(
            _kickoff_loop(),
            name=f"remote-bootstrap-kickoff:{effective_sid}:{target_member}",
        )
        kickoff_tasks.add(kickoff_task)

        def _on_kickoff_done(task: asyncio.Task[Any]) -> None:
            kickoff_tasks.discard(task)
            loop_kicked_members.discard(loop_key)
            if task.cancelled():
                return
            try:
                task.result()
            except Exception as exc:
                logger.warning(
                    "[RemoteMemberBootstrap] teammate execution kickoff task crashed "
                    "team=%s session_id=%s member=%s error=%s",
                    envelope_team_name,
                    effective_sid,
                    target_member,
                    exc,
                )

        kickoff_task.add_done_callback(_on_kickoff_done)
    processed_ids.add(bootstrap_id)
    return target_member


async def apply_member_shutdown_envelope_from_control_plane(
    *,
    kickoff_tasks: set[asyncio.Task[Any]],
    loop_kicked_members: set[tuple[str, str]],
    envelope: dict[str, Any],
    source_id: str,
    channel_id: str = "default",
) -> bool:
    """Handle leader shutdown finalize notification on a remote teammate process."""
    if not isinstance(envelope, dict):
        return False
    envelope_session_id = str(envelope.get("session_id", "")).strip()
    target_member = str(envelope.get("member_name", "")).strip()
    if not envelope_session_id or not target_member:
        return False
    force = bool(envelope.get("force", False))
    finalized = await finalize_remote_member_shutdown_on_teammate(
        envelope_session_id,
        target_member,
        channel_id=channel_id,
        force=force,
        kickoff_tasks=kickoff_tasks,
        loop_kicked_members=loop_kicked_members,
    )
    return finalized


async def apply_team_destroy_envelope_from_control_plane(
    *,
    loop_kicked_members: set[tuple[str, str]],
    kickoff_tasks: set[asyncio.Task[Any]],
    adopted_member: str,
    local_member: str,
    envelope: dict[str, Any],
    source_id: str,
) -> str:
    """Handle leader teardown notification on a remote teammate process."""
    if not isinstance(envelope, dict):
        return adopted_member
    envelope_team_name = str(envelope.get("team_name", "")).strip()
    envelope_session_id = str(envelope.get("session_id", "")).strip()
    target_member = str(envelope.get("member_name", "")).strip()
    if not envelope_team_name or not envelope_session_id or not target_member:
        return adopted_member

    await _cancel_remote_bootstrap_kickoff_tasks(
        envelope_session_id,
        target_member,
        kickoff_tasks,
        loop_kicked_members=loop_kicked_members,
    )

    await _stop_dynamic_member_agents_for_session(envelope_session_id, target_member)

    try:
        from jiuwenswarm.agents.harness.team.team_manager import get_team_manager

        await get_team_manager("default").destroy_team(envelope_session_id)
        logger.info(
            "[RemoteMemberBootstrap] teammate destroyed dynamic team runtime "
            "team=%s session_id=%s member=%s source_id=%s",
            envelope_team_name,
            envelope_session_id,
            target_member,
            source_id,
        )
    except Exception as exc:
        logger.warning(
            "[RemoteMemberBootstrap] teammate team destroy cleanup failed "
            "team=%s session_id=%s member=%s source_id=%s error=%s",
            envelope_team_name,
            envelope_session_id,
            target_member,
            source_id,
            exc,
        )

    registry = envelope.get("registry") if isinstance(envelope.get("registry"), dict) else {}
    try:
        from jiuwenswarm.common.config import get_config as _get_config
        from jiuwenswarm.agents.harness.team.a2x.a2x_registry_runtime import (
            restore_teammate_blank_agent_on_destroy,
        )

        restored = await restore_teammate_blank_agent_on_destroy(
            _get_config(),
            dataset=str(registry.get("dataset", "")).strip() or None,
            service_id=str(registry.get("service_id", "")).strip() or None,
            endpoint=str(registry.get("endpoint", "")).strip() or None,
            source="teammate-team-destroy",
        )
        if restored:
            logger.info(
                "[RemoteMemberBootstrap] teammate restored blank registry card "
                "team=%s session_id=%s member=%s service_id=%s",
                envelope_team_name,
                envelope_session_id,
                target_member,
                registry.get("service_id", ""),
            )
    except Exception as exc:
        logger.warning(
            "[RemoteMemberBootstrap] teammate registry blank restore failed "
            "team=%s session_id=%s member=%s: %s",
            envelope_team_name,
            envelope_session_id,
            target_member,
            exc,
        )

    if adopted_member == target_member:
        return local_member
    return adopted_member


async def run_teammate_bootstrap_daemon(*, stop_event: asyncio.Event, poll_interval: float = 1.0) -> None:
    """Startup daemon for distributed teammate: consume bootstrap even before team runtime exists."""
    from jiuwenswarm.common.config import get_config as _get_config

    config = _get_config()
    if not _is_distributed_teammate_runtime(config):
        return
    team_cfg = config.get("team") if isinstance(config.get("team"), dict) else {}
    transport_cfg = team_cfg.get("transport") if isinstance(team_cfg.get("transport"), dict) else {}
    transport_params = transport_cfg.get("params") if isinstance(transport_cfg.get("params"), dict) else {}

    runtime = team_cfg.get("runtime") if isinstance(team_cfg.get("runtime"), dict) else {}
    local_member = str(runtime.get("member_name", "teammate_1")).strip() or "teammate_1"
    adopted_member = local_member
    processed: set[str] = set()
    loop_kicked_members: set[tuple[str, str]] = set()
    kickoff_tasks: set[asyncio.Task[Any]] = set()
    bootstrap_router = None
    zmq_mod = None
    direct_bootstrap_addr = _normalize_leader_direct_addr(
        transport_params.get(_TRANSPORT_BOOTSTRAP_DIRECT_ADDR_KEY)
    )

    if direct_bootstrap_addr:
        try:
            import zmq
            import zmq.asyncio

            zmq_mod = zmq
            ctx = zmq.asyncio.Context.instance()
            bootstrap_router = ctx.socket(zmq.ROUTER)
            bootstrap_router.bind(direct_bootstrap_addr)
            logger.info(
                "[RemoteMemberBootstrap] teammate direct bootstrap listener started addr=%s local_member=%s",
                direct_bootstrap_addr,
                local_member,
            )
            try:
                from jiuwenswarm.agents.harness.team.a2x.a2x_registry_runtime import (
                    register_teammate_blank_agent_at_startup,
                )

                await register_teammate_blank_agent_at_startup(
                    config,
                    source="teammate-bootstrap-daemon",
                )
            except Exception as exc:
                logger.warning(
                    "[RemoteMemberBootstrap] teammate startup A2X registration failed: %s",
                    exc,
                )
        except Exception as exc:
            bootstrap_router = None
            logger.warning(
                "[RemoteMemberBootstrap] teammate direct bootstrap listener disabled addr=%s error=%s",
                direct_bootstrap_addr,
                exc,
            )

    logger.info(
        "[RemoteMemberBootstrap] teammate bootstrap daemon started local_member=%s",
        local_member,
    )
    while not stop_event.is_set():
        try:
            if bootstrap_router is not None and zmq_mod is not None:
                for _ in range(64):
                    try:
                        frames = await bootstrap_router.recv_multipart(flags=zmq_mod.NOBLOCK)
                    except zmq_mod.Again:
                        break
                    except Exception as exc:
                        logger.warning("[RemoteMemberBootstrap] direct bootstrap recv failed: %s", exc)
                        break
                    if len(frames) < 2:
                        continue
                    identity, payload = frames[0], frames[-1]
                    try:
                        raw = json.loads(payload.decode("utf-8"))
                    except Exception:
                        await bootstrap_router.send_multipart([identity, b"ok"])
                        continue
                    event_type = str(raw.get("event_type", "")).strip()
                    env = None
                    if event_type == REMOTE_BOOTSTRAP_DIRECT_EVENT_TYPE:
                        payload_obj = raw.get("payload")
                        if isinstance(payload_obj, dict):
                            env = payload_obj.get("envelope")
                    elif event_type == REMOTE_TEAM_DESTROY_DIRECT_EVENT_TYPE:
                        payload_obj = raw.get("payload")
                        if isinstance(payload_obj, dict):
                            env = payload_obj.get("envelope")
                    elif event_type == REMOTE_MEMBER_SHUTDOWN_DIRECT_EVENT_TYPE:
                        payload_obj = raw.get("payload")
                        if isinstance(payload_obj, dict):
                            env = payload_obj.get("envelope")
                    if isinstance(env, dict) and event_type == REMOTE_BOOTSTRAP_DIRECT_EVENT_TYPE:
                        source_id = str(env.get("bootstrap_id", "")).strip() or str(uuid.uuid4())
                        adopted_member = await apply_bootstrap_envelope_from_control_plane(
                            processed_ids=processed,
                            loop_kicked_members=loop_kicked_members,
                            kickoff_tasks=kickoff_tasks,
                            adopted_member=adopted_member,
                            envelope=env,
                            source_id=source_id,
                        )
                    elif isinstance(env, dict) and event_type == REMOTE_TEAM_DESTROY_DIRECT_EVENT_TYPE:
                        source_id = str(env.get("destroy_id", "")).strip() or str(uuid.uuid4())
                        adopted_member = await apply_team_destroy_envelope_from_control_plane(
                            loop_kicked_members=loop_kicked_members,
                            kickoff_tasks=kickoff_tasks,
                            adopted_member=adopted_member,
                            local_member=local_member,
                            envelope=env,
                            source_id=source_id,
                        )
                    elif isinstance(env, dict) and event_type == REMOTE_MEMBER_SHUTDOWN_DIRECT_EVENT_TYPE:
                        source_id = str(env.get("shutdown_id", "")).strip() or str(uuid.uuid4())
                        await apply_member_shutdown_envelope_from_control_plane(
                            kickoff_tasks=kickoff_tasks,
                            loop_kicked_members=loop_kicked_members,
                            envelope=env,
                            source_id=source_id,
                        )
                    await bootstrap_router.send_multipart([identity, b"ok"])

        except Exception as exc:
            logger.warning("[RemoteMemberBootstrap] teammate bootstrap daemon loop error: %s", exc)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(0.2, poll_interval))
        except asyncio.TimeoutError:
            pass

    if bootstrap_router is not None:
        try:
            bootstrap_router.close(linger=0)
        except Exception as exc:
            logger.debug("[RemoteMemberBootstrap] bootstrap router close failed: %s", exc)
    for task in list(kickoff_tasks):
        task.cancel()
    for task in list(kickoff_tasks):
        with contextlib.suppress(asyncio.CancelledError):
            await task
    logger.info("[RemoteMemberBootstrap] teammate bootstrap daemon stopped")

