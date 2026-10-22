# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read historical organization membership without activating Team runtimes."""

from typing import Any

from openjiuwen.agent_teams.organization.manager import TeamOrganizationManager
from openjiuwen.agent_teams.organization.task_pool import OrgTaskManager
from openjiuwen.agent_teams.spawn.shared_resources import get_shared_db
from openjiuwen.agent_teams.tools.database import DatabaseConfig

from jiuwenswarm.agents.harness.team.config_loader import resolve_team_sqlite_db_path
from jiuwenswarm.common.team_template_config import get_team_template_snapshot
from jiuwenswarm.common.config import get_config
from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata
from jiuwenswarm.server.runtime.team_entity_store import get_team_entity_store


def organization_enabled() -> bool:
    """Match the switch used when assembling TeamAgentSpec.enable_organization."""
    return bool((get_config().get("experimental") or {}).get("team_organization_ui_enabled", False))


def _history_database(metadata: dict[str, Any], owner_team_id: str) -> Any | None:
    """Use the saved template's SQLite location before current configuration."""
    entity = get_team_entity_store().get(owner_team_id)
    snapshot = entity.template_snapshot if entity is not None else metadata.get("team_template_snapshot")
    if not snapshot and metadata.get("team_template_id"):
        snapshot = get_team_template_snapshot(template_id=metadata["team_template_id"])
    db_path = resolve_team_sqlite_db_path(snapshot if snapshot else None)
    # A history query must not create a new database when state is absent.
    if db_path is None or not db_path.is_file():
        return None
    params = dict((snapshot.get("storage", {}).get("params") or {}) if snapshot else {})
    params.update(db_type="sqlite", connection_string=str(db_path))
    return get_shared_db(DatabaseConfig.model_validate(params))


async def load_persisted_organization(
    session_id: str,
    team_id: str = "",
    *,
    db: Any = None,
) -> tuple[Any, Any] | None:
    """Anchor membership lookup to the owner saved in this session.

    A requested Team must belong to that same organization. The caller cannot
    select an arbitrary organization in the shared database by supplying an ID.
    """
    if not session_id or not organization_enabled():
        return None
    metadata = get_session_metadata(session_id) or {}
    owner_team_id = str(metadata.get("team_name") or "").strip()
    if not owner_team_id:
        return None
    if db is None:
        db = _history_database(metadata, owner_team_id)
    if db is None:
        return None
    organization_ids = await OrgTaskManager.find_organization_ids_for_team(db, owner_team_id)
    if len(organization_ids) != 1:
        return None
    manager = TeamOrganizationManager(
        organization_id=organization_ids[0], db=db, session_id=session_id
    )
    organization = await manager.get_organization()
    if organization is None:
        return None
    member_ids = {leader.team_id for leader in organization.leaders}
    if owner_team_id not in member_ids or (team_id and team_id not in member_ids):
        return None
    return db, organization
