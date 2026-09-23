# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Disk-only project list/info/session queries. No ProjectAdapter or git CLI."""

from __future__ import annotations

from typing import Any

from jiuwenswarm.common.work_mode import (
    DEFAULT_PROJECT_ID_CODE,
    DEFAULT_PROJECT_ID_WORK,
    DEFAULT_TUI_WORK_MODE,
    DEFAULT_WEB_WORK_MODE,
    is_default_project_id,
)
from jiuwenswarm.server.runtime.session import project_store
from jiuwenswarm.server.runtime.session.lifecycle import projection as lifecycle_projection
from jiuwenswarm.server.runtime.session.session_info import to_session_info
from jiuwenswarm.server.runtime.session.session_metadata import collect_all_sessions_metadata
from jiuwenswarm.server.runtime.session.work_mode import resolve_request_work_mode


def attribute_session_project(
    metadata: dict[str, Any],
    visible_project_ids: set[str],
    removed_project_ids: set[str] = frozenset(),
) -> str:
    """Return the owning project ID, ``""`` for a removed project, or the default.

    Removed (hidden) projects keep their sessions on disk, but those sessions
    must not surface: returning ``""`` makes every caller skip them instead of
    filing them under the virtual default project.  Sessions whose project
    record is missing entirely, or that carry no ``project_id`` at all, still
    fall back to the default project so legacy metadata stays readable.
    """
    project_id = str(metadata.get("project_id") or "")
    if project_id:
        if project_id in visible_project_ids:
            return project_id
        if project_id in removed_project_ids:
            return ""
    return (
        DEFAULT_PROJECT_ID_CODE
        if str(metadata.get("work_mode") or "") == DEFAULT_TUI_WORK_MODE
        else DEFAULT_PROJECT_ID_WORK
    )


def project_info_payload(
    project: Any | None,
    *,
    default_id: str | None = None,
    stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Serialize project.info exactly like the existing Web contract."""
    statistics = stats or {
        "session_count": 0,
        "last_message_at": None,
        "last_user_message_at": None,
    }
    git_defaults = {
        "enabled": False,
        "repo_root": "",
        "initialized_by_jiuwenswarm": False,
        "detected_at": 0,
        "status": "disabled",
        "branch": "",
        "error": "",
        "error_code": "",
        "hint": "",
        "is_dirty": False,
    }
    raw_git = getattr(project, "git", {}) if project is not None else {}
    git = {**git_defaults, **dict(raw_git)} if isinstance(raw_git, dict) and raw_git else git_defaults
    if default_id is not None:
        return {
            "project_id": default_id,
            "name": "默认项目",
            "project_dir": "",
            "pinned": False,
            "pin_order": 0,
            "is_default": True,
            "hidden": False,
            "lifecycle_operation": None,
            "execution_blocked": False,
            "stop_pending": False,
            "work_mode": (
                DEFAULT_TUI_WORK_MODE
                if default_id == DEFAULT_PROJECT_ID_CODE
                else DEFAULT_WEB_WORK_MODE
            ),
            "git": git,
            "session_count": statistics["session_count"],
            "last_message_at": statistics["last_message_at"],
            "last_user_message_at": statistics["last_user_message_at"],
            "created_at": 0,
            "updated_at": 0,
        }
    return {
        "project_id": project.project_id,
        "name": project.name,
        "project_dir": project.project_dir,
        "pinned": project.pinned,
        "pin_order": project.pin_order,
        "is_default": False,
        "hidden": project.hidden,
        **lifecycle_projection("project", project.project_id),
        "work_mode": getattr(project, "work_mode", "") or DEFAULT_WEB_WORK_MODE,
        "git": git,
        "session_count": statistics["session_count"],
        "last_message_at": statistics["last_message_at"],
        "last_user_message_at": statistics["last_user_message_at"],
        "created_at": project.created_at,
        "updated_at": getattr(project, "updated_at", 0),
    }


def split_project_ids(projects: list[Any]) -> tuple[set[str], set[str]]:
    """Return ``(visible, removed)`` project ID sets from the registry."""
    visible = {project.project_id for project in projects if not project.hidden}
    removed = {project.project_id for project in projects if project.hidden}
    return visible, removed


def load_project_info(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    project_id = str(params.get("project_id") or "").strip()
    if not project_id:
        return None, "project_id is required", "BAD_REQUEST"

    all_projects = project_store.list_projects(include_hidden=True, cache_bust=True)
    visible_project_ids, removed_project_ids = split_project_ids(all_projects)
    stats: dict[str, Any] = {
        "session_count": 0,
        "last_message_at": None,
        "last_user_message_at": None,
    }
    for session in collect_all_sessions_metadata():
        if session.get("channel_id") != "web" or session.get("pinned") or session.get("cron_id"):
            continue
        if attribute_session_project(
            session, visible_project_ids, removed_project_ids
        ) != project_id:
            continue
        stats["session_count"] += 1
        for key in ("last_message_at", "last_user_message_at"):
            value = session.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if stats[key] is None or value > stats[key]:
                    stats[key] = value

    if is_default_project_id(project_id):
        info = project_info_payload(None, default_id=project_id, stats=stats)
        return {"project": info, **info}, None, None

    include_hidden = bool(params.get("include_hidden"))
    project = project_store.get_project_by_id(project_id, cache_bust=True)
    if project is None or (project.hidden and not include_hidden):
        return None, "project not found", "NOT_FOUND"
    info = project_info_payload(project, stats=stats)
    return {"project": info, **info}, None, None


def load_pinned_sessions() -> dict[str, Any]:
    """Return the Web projection of pinned sessions from this user's directory.

    Sessions of a removed project are left out: removing a project hides its
    content, pinned conversations included.  Their ``pinned`` flag stays in the
    session metadata, so restoring the project puts them back in the pinned
    area without any bookkeeping here.
    """
    all_projects = project_store.list_projects(include_hidden=True, cache_bust=True)
    _, removed_project_ids = split_project_ids(all_projects)
    sessions = collect_all_sessions_metadata()
    pinned = []
    for session in sessions:
        channel_id = session.get("channel_id")
        if not session.get("pinned") or not (
            channel_id == "web" or (channel_id == "__cron__" and session.get("cron_id"))
        ):
            continue
        if str(session.get("project_id") or "") in removed_project_ids:
            continue
        pinned.append(session)
    pinned.sort(key=lambda session: int(session.get("pin_order", 0) or 0))
    return {"sessions": [to_session_info(session) for session in pinned]}


def resolve_cron_binding(
    params: dict[str, Any], channel_id: str
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Resolve a cron project against this AgentServer's injected directory."""
    work_mode, error = resolve_request_work_mode(params, channel_id=channel_id or "web")
    if error is not None:
        return None, f"invalid work_mode: {params.get('work_mode')!r}", "BAD_REQUEST"
    binding = project_store.resolve_cron_project_binding(
        params.get("project_id"), params.get("project_dir"), work_mode,
    )
    if binding.error is not None:
        return None, binding.error, binding.code or "BAD_REQUEST"
    return {
        "project_id": binding.project_id,
        "work_mode": binding.work_mode,
    }, None, None


def _parse_page(params: dict[str, Any]) -> tuple[int | None, int]:
    """Preserve the Web handler's permissive pagination parsing."""
    raw_limit = params.get("limit")
    limit: int | None = None
    if isinstance(raw_limit, int) and not isinstance(raw_limit, bool):
        limit = raw_limit
    elif isinstance(raw_limit, float) and raw_limit.is_integer():
        limit = int(raw_limit)
    elif isinstance(raw_limit, str) and raw_limit.strip().isdigit():
        limit = int(raw_limit.strip())
    raw_offset = params.get("offset")
    offset = 0
    if isinstance(raw_offset, int) and not isinstance(raw_offset, bool):
        offset = raw_offset
    elif isinstance(raw_offset, float) and raw_offset.is_integer():
        offset = int(raw_offset)
    elif isinstance(raw_offset, str) and raw_offset.strip().isdigit():
        offset = int(raw_offset.strip())
    return (max(1, limit) if limit is not None else None), max(0, offset)


def load_project_sessions(
    params: dict[str, Any], _user_id: str
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    project_id = str(params.get("project_id") or "").strip()
    if not project_id:
        return None, "project_id is required", "BAD_REQUEST"
    limit, offset = _parse_page(params)
    all_projects = project_store.list_projects(include_hidden=True, cache_bust=True)
    visible_project_ids, removed_project_ids = split_project_ids(all_projects)
    if not is_default_project_id(project_id):
        project = project_store.get_project_by_id(project_id, cache_bust=True)
        if project is None or project.hidden:
            return None, "project not found", "NOT_FOUND"

    matched: list[dict[str, Any]] = []
    for session in collect_all_sessions_metadata():
        if session.get("pinned") or session.get("cron_id") or session.get("channel_id") != "web":
            continue
        if attribute_session_project(
            session, visible_project_ids, removed_project_ids
        ) != project_id:
            continue
        matched.append(session)
    matched.sort(
        key=lambda session: (
            float(session["last_user_message_at"])
            if isinstance(session.get("last_user_message_at"), (int, float))
            and not isinstance(session.get("last_user_message_at"), bool)
            else 0.0
        ),
        reverse=True,
    )
    total = len(matched)
    page = matched[offset:offset + limit] if limit is not None else matched[offset:]
    return {
        "sessions": [to_session_info(session) for session in page],
        "total": total,
    }, None, None


def load_project_cron_sessions(
    params: dict[str, Any], _user_id: str
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Return cron execution sessions from this AgentServer's user directory."""
    project_id = str(params.get("project_id") or "").strip()
    if not project_id:
        return None, "project_id is required", "BAD_REQUEST"
    cron_id = str(params.get("cron_id") or "").strip()
    limit, offset = _parse_page(params)
    all_projects = project_store.list_projects(include_hidden=True, cache_bust=True)
    visible_project_ids, removed_project_ids = split_project_ids(all_projects)
    if not is_default_project_id(project_id):
        project = project_store.get_project_by_id(project_id, cache_bust=True)
        if project is None or project.hidden:
            return None, "project not found", "NOT_FOUND"
    matched: list[dict[str, Any]] = []
    for session in collect_all_sessions_metadata():
        if session.get("pinned") or not session.get("cron_id"):
            continue
        if attribute_session_project(
            session, visible_project_ids, removed_project_ids
        ) != project_id:
            continue
        if cron_id and session.get("cron_id") != cron_id:
            continue
        matched.append(session)
    matched.sort(
        key=lambda session: (
            float(session["last_user_message_at"])
            if isinstance(session.get("last_user_message_at"), (int, float))
            and not isinstance(session.get("last_user_message_at"), bool)
            else 0.0
        ),
        reverse=True,
    )
    total = len(matched)
    page = matched[offset:offset + limit] if limit is not None else matched[offset:]
    return {
        "sessions": [to_session_info(session) for session in page],
        "total": total,
    }, None, None


def load_project_list(
    params: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Build the existing Web project-list view in the injected directory."""
    filter_value = str(params.get("filter") or "all").strip() or "all"
    if filter_value not in {"all", "pinned", "unpinned"}:
        filter_value = "all"
    include_hidden = bool(params.get("include_hidden", False))
    raw_work_mode = params.get("work_mode")
    work_mode: str | None = None
    if isinstance(raw_work_mode, str) and raw_work_mode.strip():
        candidate = raw_work_mode.strip().lower()
        if candidate not in {DEFAULT_WEB_WORK_MODE, DEFAULT_TUI_WORK_MODE}:
            return None, f"invalid work_mode: {candidate!r}, must be 'code' or 'work'", "BAD_REQUEST"
        work_mode = candidate

    all_projects = project_store.list_projects(include_hidden=True, cache_bust=True)
    projects = [p for p in all_projects if work_mode is None or (p.work_mode or DEFAULT_WEB_WORK_MODE) == work_mode]
    visible_project_ids, removed_project_ids = split_project_ids(all_projects)
    stats: dict[str, dict[str, Any]] = {}

    def stats_for(project_id: str) -> dict[str, Any]:
        return stats.setdefault(
            project_id,
            {"session_count": 0, "last_message_at": None, "last_user_message_at": None},
        )

    for session in collect_all_sessions_metadata():
        if session.get("channel_id") != "web" or session.get("pinned") or session.get("cron_id"):
            continue
        owner = attribute_session_project(
            session, visible_project_ids, removed_project_ids
        )
        if not owner:
            continue
        entry = stats_for(owner)
        entry["session_count"] += 1
        for key in ("last_message_at", "last_user_message_at"):
            value = session.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if entry[key] is None or value > entry[key]:
                    entry[key] = value

    zero = {"session_count": 0, "last_message_at": None, "last_user_message_at": None}

    def item(project: Any | None, default_id: str | None = None) -> dict[str, Any]:
        if default_id is not None:
            return project_info_payload(None, default_id=default_id, stats=stats.get(default_id, zero))
        return project_info_payload(
            project,
            stats=zero if project.hidden else stats.get(project.project_id, zero),
        )

    default_ids: list[str] = []
    if work_mode in (None, DEFAULT_WEB_WORK_MODE):
        default_ids.append(DEFAULT_PROJECT_ID_WORK)
    if work_mode in (None, DEFAULT_TUI_WORK_MODE):
        default_ids.append(DEFAULT_PROJECT_ID_CODE)
    default_items = [item(None, default_id) for default_id in default_ids]

    def user_sort(info: dict[str, Any]) -> float:
        value = info["last_user_message_at"]
        return (
            float(value)
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            else 0.0
        )
    if filter_value == "pinned":
        result = [item(project) for project in projects if project.pinned]
        result.sort(key=lambda info: info["pin_order"])
    elif filter_value == "unpinned":
        result = [item(p) for p in projects if not p.pinned and (include_hidden or not p.hidden)]
        result.sort(key=user_sort, reverse=True)
        result.extend(default_items)
    else:
        pinned = [
            item(project)
            for project in projects
            if project.pinned and (include_hidden or not project.hidden)
        ]
        pinned.sort(key=lambda info: info["pin_order"])
        unpinned = [item(p) for p in projects if not p.pinned and (include_hidden or not p.hidden)]
        unpinned.sort(key=user_sort, reverse=True)
        result = pinned + unpinned + default_items
    return {"projects": result}, None, None
