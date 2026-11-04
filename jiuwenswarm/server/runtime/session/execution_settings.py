# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Authoritative team-session execution settings and member environment policy."""

import hashlib
import re
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from openjiuwen.agent_teams.schema.blueprint import TeamAgentSpec

from openjiuwen.agent_teams.tools.database import TeamDatabase
from openjiuwen.core.sys_operation.execution_environment import ExecutionEnvironment

from jiuwenswarm.server.runtime.session.session_metadata import (
    get_session_metadata,
    _enqueue_write,
)


def persist_settings_mirror(session_id: str, settings: dict[str, Any]) -> None:
    """Merge only this field so concurrent session writers are not rolled back."""
    metadata = get_session_metadata(session_id, cache_bust=True) or {}
    metadata["execution_settings"] = settings
    _enqueue_write(
        session_id,
        metadata,
        sync_write=True,
        merge_fields=frozenset({"execution_settings"}),
    )


def validate_settings(value: Any) -> dict[str, Any]:
    """Validate settings without accepting truthy strings or unknown policies."""
    if not isinstance(value, dict) or set(value) - {"independent_git_identity", "environment"}:
        raise ValueError("Invalid execution settings")
    enabled = value.get("independent_git_identity", False)
    if not isinstance(enabled, bool):
        raise ValueError("independent_git_identity must be a boolean")
    environment = value.get("environment", {})
    if not isinstance(environment, dict):
        raise ValueError("environment must be an object")
    validated = ExecutionEnvironment(values=environment)
    return {"independent_git_identity": enabled, "environment": dict(validated.values)}


def member_environment(settings: dict[str, Any], member_name: str | None) -> ExecutionEnvironment:
    """Resolve generic values and optional Git identity for the executing member."""
    settings = validate_settings(settings)
    overrides = {}
    if settings["independent_git_identity"]:
        if not member_name or member_name != member_name.strip() or any(c in member_name for c in "\n\r\0<>"):
            raise ValueError("Independent Git identity requires a valid member name")
        local = member_name
        reserved = re.fullmatch(r"member-[0-9a-f]{40}", local) is not None
        if not re.fullmatch(r"[A-Za-z0-9_+-]+(?:\.[A-Za-z0-9_+-]+)*", local) or len(local) > 64 or reserved:
            local = "member-" + hashlib.sha256(member_name.encode("utf-8")).hexdigest()[:40]
        email = f"{local}@jiuwenswarm.com"
        overrides = {
            "GIT_AUTHOR_NAME": member_name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": member_name,
            "GIT_COMMITTER_EMAIL": email,
        }
    return ExecutionEnvironment(values=settings["environment"], overrides=overrides)


async def initialize_settings(
    session_id: str, database: TeamDatabase, initial: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Seed once from session metadata, preserving settings already committed."""
    await database.initialize()
    current = await database.session_settings.get(session_id)
    if current is None:
        metadata = get_session_metadata(session_id) or {}
        settings = validate_settings(initial if initial is not None else metadata.get("execution_settings", {}))
        await database.session_settings.initialize(session_id, settings)
        current = await database.session_settings.get(session_id)
    if current is None:
        raise RuntimeError("Session execution settings could not be initialized")
    return current


async def prepare_session_execution(
    session_id: str, spec: "TeamAgentSpec", initial: dict[str, Any] | None,
) -> None:
    """Initialize authoritative policy before starting or recovering members."""
    metadata = get_session_metadata(session_id) or {}
    if metadata.get("work_mode") != "code":
        if initial and initial.get("independent_git_identity"):
            raise ValueError("Independent Git identity requires a code team session")
        return
    database = TeamDatabase(spec.resolve_db_config())
    if database.config.connection_string == ":memory:":
        requested = validate_settings(initial or metadata.get("execution_settings", {}))
        if requested["independent_git_identity"] or requested["environment"]:
            raise ValueError("Session execution settings require persistent team storage")
        return
    try:
        current = await initialize_settings(session_id, database, initial)
    finally:
        await database.close()
    persist_settings_mirror(session_id, current["settings"])
    spec.build_context.execution_settings_managed = True
    spec.build_context_seed = spec.build_context.to_seed()


async def session_settings_request(
    session_id: str, params: dict[str, Any], channel_id: str | None,
) -> dict[str, Any]:
    """Read or CAS-update settings in the same database used by team members."""
    from jiuwenswarm.agents.harness.team.team_manager import get_team_manager

    metadata = get_session_metadata(session_id, cache_bust=True, enable_writeback=False)
    if not metadata:
        raise ValueError("Session not found")
    mode = str(metadata.get("mode") or "")
    if not metadata.get("message_count") and params.get("mode") == "team":
        mode = "team"
    if metadata.get("work_mode") != "code" or not mode.startswith("team"):
        raise ValueError("Execution settings require a code team session")
    manager = get_team_manager(channel_id)
    agent = manager.get_team_agent(session_id)
    if agent is not None and agent.team_backend is not None:
        database = agent.team_backend.db
    else:
        spec, _ = manager._load_session_team_spec(session_id)
        database = TeamDatabase(spec.resolve_db_config())
    owns_database = agent is None or agent.team_backend is None
    if database.config.connection_string == ":memory:":
        raise ValueError("Session execution settings require persistent team storage")
    try:
        return await _request_with_database(session_id, database, params)
    finally:
        if owns_database:
            await database.close()


async def _request_with_database(
    session_id: str, database: TeamDatabase, params: dict[str, Any],
) -> dict[str, Any]:
    """Apply the request using an owned or borrowed database connection."""
    await database.initialize()
    current = await database.session_settings.get(session_id)
    if current is None:
        metadata = get_session_metadata(session_id) or {}
        current = {"settings": validate_settings(metadata.get("execution_settings", {})), "version": -1}
    if "settings" in params:
        settings = validate_settings(params["settings"])
        version = params.get("expected_version")
        if type(version) is not int or version < -1:
            raise ValueError("expected_version is required")
        if version != current["version"]:
            raise ValueError("Execution settings changed; refresh and retry")
        if version == -1:
            await database.session_settings.initialize(session_id, current["settings"])
            version = 0
        version = await database.session_settings.replace(
            session_id, settings, expected_version=version,
        )
        current = {"settings": settings, "version": version}
        persist_settings_mirror(session_id, settings)
    return current
