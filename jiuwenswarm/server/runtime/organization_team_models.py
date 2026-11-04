# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persistent, credential-free model selections scoped to one organization Team."""

from __future__ import annotations

import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from openjiuwen.agent_teams.paths import team_home
from openjiuwen.agent_teams.tools.member_options import get_member_cli_agent

from jiuwenswarm.common.model_catalog import ModelCatalog
from jiuwenswarm.server.runtime.model_routing_registry import ModelSelectionResolver
from jiuwenswarm.common.model_selection import ModelSelection
from jiuwenswarm.server.runtime.model_compiler_adapter import compile_model_selection
from jiuwenswarm.server.runtime.team_binding_store import validate_team_name

logger = logging.getLogger(__name__)


class OrganizationTeamModel(BaseModel):
    team_id: str
    session_id: str
    organization_id: str
    selection: ModelSelection
    model_name: str


def _selection_path(team_id: str) -> Path:
    return team_home(validate_team_name(team_id)) / "team-workspace" / ".team-meta" / "organization-model.json"


def load_team_model(team_id: str, session_id: str) -> OrganizationTeamModel | None:
    """Missing or different-session settings do not override an ordinary Team."""
    try:
        raw = _selection_path(team_id).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    setting = OrganizationTeamModel.model_validate_json(raw)
    if setting.team_id != team_id:
        raise ValueError("organization model setting belongs to another Team")
    if setting.session_id != session_id:
        return None
    if setting.selection.type != "model":
        raise ValueError("organization Team override requires a single model")
    return setting


def save_team_model(setting: OrganizationTeamModel) -> None:
    """Publish a complete reference atomically; never persist resolved credentials."""
    path = _selection_path(setting.team_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(setting.model_dump_json())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def resolve_team_model(selection: ModelSelection) -> Any:
    """Revalidate the saved ID against current config; never fall back on failure."""
    if selection.type != "model":
        raise ValueError("specify a single model for the expert Team")
    resolved = ModelSelectionResolver().resolve(selection)
    return compile_model_selection(resolved)


def select_team_model(model_name: str) -> tuple[ModelSelection, Any]:
    """Accept an exact name, alias or stable ID; reject ambiguous endpoint names."""
    requested = str(model_name or "").strip()
    if not requested:
        raise ValueError("model_name is required")
    matches = []
    for model in ModelCatalog().list_public_models():
        identifiers = (model["model_id"], model["model_name"], model.get("alias"))
        if model.get("enabled") and requested in identifiers:
            matches.append(model)
    if len(matches) != 1:
        ids = [model["model_id"] for model in matches]
        raise ValueError(f"model must match exactly one configured model; matching model_ids={ids}")
    selection = ModelSelection(type="model", id=matches[0]["model_id"])
    return selection, resolve_team_model(selection)


async def set_organization_team_model(
    *, runtime: Any, caller_team_id: str, session_id: str,
    organization_id: str, target_team_id: str, model_name: str,
) -> OrganizationTeamModel:
    """Only the Owner or the target's own Leader may change an expert Team."""
    caller = await runtime.pool.get(caller_team_id)
    if caller is None or caller.current_session_id != session_id:
        raise ValueError("caller Team is not active in this session")
    backend = getattr(caller.agent, "team_backend", None)
    if backend is None or not backend.is_leader:
        raise ValueError("only a Team Leader can change an expert Team model")
    organization = await runtime.organization_runtime_manager.get_organization(
        organization_id=organization_id, team_id=caller_team_id, session_id=session_id,
    )
    if organization is None:
        raise ValueError("organization not found")
    if caller_team_id not in (organization.owner_team_id, target_team_id):
        raise ValueError("only the Owner or target Team Leader can change its model")
    members = {leader.team_id for leader in organization.leaders}
    if target_team_id == organization.owner_team_id:
        raise ValueError(
            "org_set_team_model cannot change the Owner Team. If its current session model already "
            "matches the requirement, proceed without switching; use this tool only for child Teams."
        )
    if target_team_id not in members:
        raise ValueError("target must be a child Team in this organization")

    from jiuwenswarm.server.runtime.organization_packages import team_package_availability

    target = await runtime.pool.get(target_team_id)
    if target is None or target.current_session_id != session_id:
        raise ValueError("target Team must be active in this session before changing its model")
    target_backend = getattr(target.agent, "team_backend", None)
    if target_backend is None:
        raise ValueError("target Team runtime is unavailable")
    metadata = getattr(getattr(target.agent, "spec", None), "metadata", {})
    group = str((metadata or {}).get("agent_group_name") or "")
    availability = team_package_availability(target_team_id, group)
    if availability.get("available") is not True:
        raise ValueError("target expert Team package is unavailable or its identity is unknown")
    rows = await backend.db.member.get_team_members(target_team_id)
    is_bridge = getattr(target_backend, "is_bridge_agent", lambda name: False)
    for row in rows:
        if get_member_cli_agent(row) or is_bridge(row.member_name):
            raise ValueError("Team-wide model selection does not support external CLI or bridge members")

    # Resolve and validate before publishing anything. A failed change leaves
    # the previous selection and every other Team untouched.
    selection, compiled = select_team_model(model_name)
    setting = OrganizationTeamModel(
        team_id=target_team_id, session_id=session_id, organization_id=organization_id,
        selection=selection, model_name=compiled.model_request_config.model_name,
    )
    save_team_model(setting)
    return setting


async def publish_team_model(setting: OrganizationTeamModel) -> None:
    """Delivery failure never rolls back an already-persisted model selection."""
    from jiuwenswarm.server.gateway_push.transport import WebSocketGatewayPushTransport
    from jiuwenswarm.agents.harness.team.expert_org.launcher import _resolve_launch_channel_id

    try:
        await WebSocketGatewayPushTransport().send_push({
            "request_id": uuid.uuid4().hex,
            "channel_id": _resolve_launch_channel_id(None, setting.session_id),
            "session_id": setting.session_id,
            "payload": {
                "event_type": "team.model_changed", "session_id": setting.session_id,
                "team_id": setting.team_id, "model_name": setting.model_name,
                "model_selection": setting.selection.model_dump(exclude_none=True),
            },
            "is_complete": False,
        })
    except Exception:
        logger.warning("Cannot publish Team model selection for %s", setting.team_id, exc_info=True)
