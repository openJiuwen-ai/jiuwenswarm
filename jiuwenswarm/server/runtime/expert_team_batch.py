# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""User-selected expert Teams, using the same organization API as model tools."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from agent_gateway.common.mode_matrix import is_team_mode

from jiuwenswarm.common.utils import mask_sensitive
from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata, update_session_metadata

logger = logging.getLogger(__name__)


def validate_group_names(value: Any) -> list[str]:
    """Validate the entire envelope before creating any runtime."""
    if not isinstance(value, list) or not value or len(value) > 32:
        raise ValueError("agent_group_names must contain between 1 and 32 names")
    if any(not isinstance(name, str) or not name.strip() for name in value):
        raise ValueError("agent_group_names must contain nonempty strings")
    return list(dict.fromkeys(name.strip() for name in value))


async def _prepare_owner(request: Any, team_manager: Any, runtime: Any, metadata: dict[str, Any]) -> Any:
    """Activate the owner without opening a stream or sending an LLM input."""
    session_id = request.session_id
    owner_id = str(metadata.get("team_name") or "").strip()
    active = await runtime.pool.get(owner_id) if owner_id else None
    if active is not None:
        if active.current_session_id != session_id:
            raise ValueError("owner team is bound to another session")
        return active.agent.team_backend
    from jiuwenswarm.server.runtime.agent_adapter.team_helpers import (
        _resolve_session_swarmflow_config, _resolve_session_team_dispatch_mode,
        _resolve_session_team_bootstrap_mode,
    )
    from jiuwenswarm.common.config import get_config

    params = request.params or {}
    dispatch_mode = _resolve_session_team_dispatch_mode(params, session_id=session_id)
    bootstrap_mode = _resolve_session_team_bootstrap_mode(params, session_id=session_id)
    spec = await team_manager.get_swarm_enriched_team_spec(
        session_id=session_id,
        mode=metadata["mode"],
        project_dir=metadata.get("project_dir") or None,
        request_id=request.request_id,
        user_id=request.user_id or None,
        channel_id=request.channel_id,
        request_metadata={**metadata, **(request.metadata or {})},
        requested_model_name=metadata.get("model") or metadata.get("model_name") or None,
        model_selection=metadata.get("model_selection"),
        agent_group_name=metadata.get("agent_group_name") or None,
        dispatch_mode=dispatch_mode,
        bootstrap_mode=bootstrap_mode,
        swarmflow_config=_resolve_session_swarmflow_config(
            params, get_config(), session_id=session_id, project_dir=metadata.get("project_dir"),
        ),
    )
    if owner_id and spec.team_name != owner_id:
        raise ValueError("owner team identity changed during recovery")
    spec.enable_organization = True
    from jiuwenswarm.server.runtime.agent_adapter.team_helpers import (
        persist_session_team_bootstrap_mode, persist_session_team_dispatch_mode,
        persist_session_swarmflow_config,
    )

    await team_manager.prepare_runtime_activation(session_id, spec.team_name)
    try:
        activation = await runtime.activate(spec, session_id)
        agent = getattr(activation, "agent", None)
        if agent is None:
            raise ValueError("owner team could not be activated")
        # activate prepares the runtime/session tables; coordination normally
        # calls build_team on its first LLM turn. This entry deliberately has
        # no turn, so materialize the owner and leader with the public API.
        await agent.team_backend.build_team(
            display_name=str(getattr(spec, "display_name", "") or spec.team_name),
            desc=str(getattr(spec, "description", "") or ""),
            leader_display_name=str(getattr(agent, "member_name", "") or "Leader"),
            leader_desc="",
        )
        update_session_metadata(session_id=session_id, team_name=spec.team_name,
                                touch_last_message_at=False, sync_write=True)
        persist_session_team_dispatch_mode(session_id, spec.dispatch_mode)
        persist_session_team_bootstrap_mode(session_id, spec.bootstrap_mode)
        persist_session_swarmflow_config(session_id, {
            "enable_swarmflow": spec.enable_swarmflow, "swarmflow_budget": spec.swarmflow_budget,
        })
        await runtime.pause(team_name=spec.team_name, session_id=session_id)
        # A materialized, paused owner has not started its chat stream or
        # Harness. Only team.runtime_ready from that stream may commit the
        # local active/initialized markers. Otherwise the first chat.send is
        # misrouted to interact(), which requires a started Harness.
        team_manager.clear_pending_runtime(session_id)
        team_manager.clear_active_runtime(session_id)
        team_manager.clear_session_initialized(session_id)
        return agent.team_backend
    except BaseException:
        team_manager.clear_pending_runtime(session_id)
        try:
            await runtime.stop_team(team_name=spec.team_name, session_id=session_id)
        except Exception:
            logger.exception("Failed to clean up owner activation session=%s", session_id)
        raise


async def _validate_package(name: str) -> None:
    from openjiuwen.agent_teams.experts import load_agent_group_package_bundle
    from jiuwenswarm.agents.harness.team.experts.catalog import JiuwenExpertCatalog
    from jiuwenswarm.server.runtime.extension_package_manager import disabled_agent_group_runtimes

    location = await JiuwenExpertCatalog().locate_group(name)
    package = await asyncio.to_thread(load_agent_group_package_bundle, location.path)
    disabled = disabled_agent_group_runtimes(package)
    if disabled:
        raise ValueError(f"Expert team requires disabled runtimes: {', '.join(disabled)}")


async def _invite_groups(
    *, names: list[str], runtime: Any, team_runtime: Any, organization: Any, session_id: str,
) -> list[dict[str, Any]]:
    """Use durable membership to deduplicate even after a browser/server restart."""
    from jiuwenswarm.server.runtime.team_organization_identity_store import load_organization_team_identity

    existing: dict[str, str] = {}
    for leader in organization.leaders:
        if leader.team_id == organization.owner_team_id:
            continue
        identity = await asyncio.to_thread(load_organization_team_identity, leader.team_id)
        name = identity.get("agent_group_name")
        if name:
            existing.setdefault(name, leader.team_id)
    # The identity file is best effort in the launcher. Live specs provide the
    # fallback while the process is alive; membership still gates every entry.
    for active in await team_runtime.pool.teams_for_session(session_id):
        if not any(leader.team_id == active.team_name for leader in organization.leaders):
            continue
        if active.team_name == organization.owner_team_id:
            continue
        name = (getattr(active.agent.spec, "metadata", None) or {}).get("agent_group_name")
        if name:
            existing.setdefault(name, active.team_name)
    results = []
    for name in names:
        if name in existing:
            results.append({"agent_group_name": name, "status": "existing", "team_id": existing[name]})
            continue
        try:
            await _validate_package(name)
            result = await runtime.create_and_invite_expert_team(
                organization_id=organization.organization_id,
                owner_team_id=organization.owner_team_id,
                agent_group_name=name,
                session_id=session_id,
            )
            existing[name] = result["team_id"]
            results.append({"agent_group_name": name, "status": "created", "team_id": result["team_id"]})
        except Exception as exc:
            logger.exception("Expert batch invite failed session=%s group=%s", session_id, name)
            results.append({"agent_group_name": name, "status": "failed", "error": mask_sensitive(str(exc))})
    return results


async def add_expert_teams(request: Any) -> dict[str, Any]:
    from openjiuwen.agent_teams.context import reset_session_id, set_session_id
    from jiuwenswarm.server.runtime.session.session_history import is_valid_session_id

    session_id = request.session_id
    if not is_valid_session_id(session_id) or session_id == "new":
        raise ValueError("a real session_id is required")
    token = set_session_id(session_id)
    try:
        return await _add_expert_teams_in_session(request)
    finally:
        reset_session_id(token)


async def _add_expert_teams_in_session(request: Any) -> dict[str, Any]:
    from openjiuwen.core.runner.runner import GLOBAL_RUNNER

    from jiuwenswarm.agents.harness.team import get_team_manager
    from jiuwenswarm.agents.harness.team.expert_org.wiring import register_expert_adapter_installer
    from jiuwenswarm.server.runtime.team_organization_history import (
        load_persisted_organization, organization_enabled,
    )

    names = validate_group_names((request.params or {}).get("agent_group_names"))
    session_id = str(request.session_id or "").strip()
    if not session_id or session_id == "new":
        raise ValueError("a real session_id is required")
    if not organization_enabled():
        raise ValueError("Team Organization is disabled")
    manager = get_team_manager(request.channel_id or "web")
    # Shares first-turn assembly locking with chat.send, and serializes browser
    # retries. The lock is owned by the session's existing TeamManager.
    async with manager.get_startup_lock(session_id):
        metadata = get_session_metadata(session_id, cache_bust=True) or {}
        if not is_team_mode(metadata.get("mode")):
            raise ValueError("expert teams can only be added to a Team session")
        register_expert_adapter_installer()
        team_runtime = GLOBAL_RUNNER._team_runtime_manager
        org_runtime = team_runtime.organization_runtime_manager
        backend = await _prepare_owner(request, manager, team_runtime, metadata)
        owner_id = backend.team_name
        organization_id = str(getattr(getattr(backend, "org_task_manager", None), "organization_id", "") or "")
        persisted = await load_persisted_organization(session_id, db=backend.db)
        if persisted is not None:
            _, organization = persisted
            organization_id = organization.organization_id
        # create_organization is also the public owner bind/recovery operation.
        organization = await org_runtime.create_organization(
            organization_id=organization_id or f"org_{owner_id}",
            owner_team_id=owner_id, session_id=session_id,
        )
        results = await _invite_groups(
            names=names, runtime=org_runtime, team_runtime=team_runtime,
            organization=organization, session_id=session_id,
        )
        return {"session_id": session_id, "organization_id": organization.organization_id, "results": results}
