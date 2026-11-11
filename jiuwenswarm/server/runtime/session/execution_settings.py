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


async def apply_requested_settings(
    session_id: str, database: TeamDatabase, requested: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge the fields a ``chat.send`` carried into the committed settings.

    The Web client sends the toggle with every team turn, so the latest turn
    is the update path: only the fields present in *requested* change, the
    rest (e.g. ``environment`` set by another host) are kept. The write is a
    compare-and-swap; a concurrent writer gets one retry on fresh data.

    Args:
        session_id: Team session whose settings change.
        database: Team database shared with the session's members.
        requested: Partial settings from the request, or None to only seed.

    Returns:
        The committed ``{"settings", "version"}`` record.
    """
    current = await initialize_settings(session_id, database, requested)
    if not requested:
        return current
    validate_settings(requested)
    for _ in range(2):
        merged = validate_settings({**current["settings"], **requested})
        if merged == validate_settings(current["settings"]):
            return current
        try:
            version = await database.session_settings.replace(
                session_id, merged, expected_version=current["version"],
            )
            return {"settings": merged, "version": version}
        except ValueError:
            current = await database.session_settings.get(session_id) or current
    raise ValueError("Execution settings changed concurrently; retry the request")


async def prepare_session_execution(
    session_id: str, spec: "TeamAgentSpec", requested: dict[str, Any] | None,
) -> None:
    """Commit the turn's policy before starting or recovering members.

    Members resolve the policy from the team database at every tool call, so
    an update committed here applies to new tool calls of a running team too.
    """
    metadata = get_session_metadata(session_id) or {}
    if metadata.get("work_mode") != "code":
        if requested and requested.get("independent_git_identity"):
            raise ValueError("Independent Git identity requires a code team session")
        return
    database = TeamDatabase(spec.resolve_db_config())
    if database.config.connection_string == ":memory:":
        effective = validate_settings(requested or metadata.get("execution_settings", {}))
        if effective["independent_git_identity"] or effective["environment"]:
            raise ValueError("Session execution settings require persistent team storage")
        return
    try:
        current = await apply_requested_settings(session_id, database, requested)
    finally:
        await database.close()
    persist_settings_mirror(session_id, current["settings"])
    spec.build_context.execution_settings_managed = True
    spec.build_context_seed = spec.build_context.to_seed()
