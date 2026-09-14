"""ProjectAdapter: project-domain requests executed in AgentServer.

Reads both the project store and session metadata from this process's
injected data directory; the request ``user_id`` remains routing/observability
metadata only.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.work_mode import (
    DEFAULT_PROJECT_ID_CODE,
    DEFAULT_PROJECT_ID_WORK,
    DEFAULT_TUI_WORK_MODE,
    DEFAULT_WEB_WORK_MODE,
    is_default_project_id,
)
from jiuwenswarm.server.runtime.gateway_adapter.base import (
    GatewayAdapter,
    build_error_response,
)
from jiuwenswarm.server.runtime.session import project_store
from jiuwenswarm.server.runtime.session.lifecycle import LifecycleError, projection as lifecycle_projection
from jiuwenswarm.server.runtime.session.session_metadata import (
    collect_all_sessions_metadata,
)
from jiuwenswarm.server.runtime.session.session_info import to_session_info
from jiuwenswarm.server.runtime.session.work_mode import resolve_request_work_mode

logger = logging.getLogger(__name__)


def _attribute_session_project(
    metadata: dict[str, Any], visible_project_ids: set[str]
) -> str:
    """Return the visible project ID, or the matching virtual default project."""
    project_id = str(metadata.get("project_id") or "")
    if project_id and project_id in visible_project_ids:
        return project_id
    return (
        DEFAULT_PROJECT_ID_CODE
        if str(metadata.get("work_mode") or "") == DEFAULT_TUI_WORK_MODE
        else DEFAULT_PROJECT_ID_WORK
    )


def _project_info_payload(
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
        "archived_at": getattr(project, "archived_at", 0),
        **lifecycle_projection("project", project.project_id),
        "work_mode": getattr(project, "work_mode", "") or DEFAULT_WEB_WORK_MODE,
        "git": git,
        "session_count": statistics["session_count"],
        "last_message_at": statistics["last_message_at"],
        "last_user_message_at": statistics["last_user_message_at"],
        "created_at": project.created_at,
        "updated_at": getattr(project, "updated_at", 0),
    }


def _load_project_info(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    project_id = str(params.get("project_id") or "").strip()
    if not project_id:
        return None, "project_id is required", "BAD_REQUEST"

    all_projects = project_store.list_projects(include_hidden=True, cache_bust=True)
    visible_project_ids = {project.project_id for project in all_projects if not project.hidden}
    stats: dict[str, Any] = {
        "session_count": 0,
        "last_message_at": None,
        "last_user_message_at": None,
    }
    for session in collect_all_sessions_metadata():
        if session.get("channel_id") != "web" or session.get("pinned") or session.get("cron_id"):
            continue
        if _attribute_session_project(session, visible_project_ids) != project_id:
            continue
        stats["session_count"] += 1
        for key in ("last_message_at", "last_user_message_at"):
            value = session.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if stats[key] is None or value > stats[key]:
                    stats[key] = value

    if is_default_project_id(project_id):
        info = _project_info_payload(None, default_id=project_id, stats=stats)
        return {"project": info, **info}, None, None

    include_hidden = bool(params.get("include_hidden"))
    project = project_store.get_project_by_id(project_id, cache_bust=True)
    if project is None or (project.hidden and not include_hidden):
        return None, "project not found", "NOT_FOUND"
    info = _project_info_payload(project, stats=stats)
    return {"project": info, **info}, None, None


def _load_pinned_sessions() -> dict[str, Any]:
    """Return the Web projection of pinned sessions from this user's directory."""
    sessions = collect_all_sessions_metadata()
    pinned = [
        session
        for session in sessions
        if session.get("pinned") and session.get("channel_id") == "web"
    ]
    pinned.sort(key=lambda session: int(session.get("pin_order", 0) or 0))
    return {"sessions": [to_session_info(session) for session in pinned]}


def _resolve_cron_binding(
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


def _load_project_sessions(
    params: dict[str, Any], _user_id: str
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    project_id = str(params.get("project_id") or "").strip()
    if not project_id:
        return None, "project_id is required", "BAD_REQUEST"
    limit, offset = _parse_page(params)
    all_projects = project_store.list_projects(include_hidden=True, cache_bust=True)
    visible_project_ids = {project.project_id for project in all_projects if not project.hidden}
    if not is_default_project_id(project_id):
        project = project_store.get_project_by_id(project_id, cache_bust=True)
        if project is None or project.hidden:
            return None, "project not found", "NOT_FOUND"

    matched: list[dict[str, Any]] = []
    for session in collect_all_sessions_metadata():
        if session.get("pinned") or session.get("cron_id") or session.get("channel_id") != "web":
            continue
        if _attribute_session_project(session, visible_project_ids) != project_id:
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


def _load_project_cron_sessions(
    params: dict[str, Any], _user_id: str
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Return cron execution sessions from this AgentServer's user directory."""
    project_id = str(params.get("project_id") or "").strip()
    if not project_id:
        return None, "project_id is required", "BAD_REQUEST"
    cron_id = str(params.get("cron_id") or "").strip()
    limit, offset = _parse_page(params)
    all_projects = project_store.list_projects(include_hidden=True, cache_bust=True)
    visible_project_ids = {project.project_id for project in all_projects if not project.hidden}
    if not is_default_project_id(project_id):
        project = project_store.get_project_by_id(project_id, cache_bust=True)
        if project is None or project.hidden:
            return None, "project not found", "NOT_FOUND"
    matched: list[dict[str, Any]] = []
    for session in collect_all_sessions_metadata():
        if session.get("pinned") or not session.get("cron_id"):
            continue
        if _attribute_session_project(session, visible_project_ids) != project_id:
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


def _load_project_list(
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
    visible_project_ids = {project.project_id for project in all_projects if not project.hidden}
    stats: dict[str, dict[str, Any]] = {}

    def stats_for(project_id: str) -> dict[str, Any]:
        return stats.setdefault(
            project_id,
            {"session_count": 0, "last_message_at": None, "last_user_message_at": None},
        )

    for session in collect_all_sessions_metadata():
        if session.get("channel_id") != "web" or session.get("pinned") or session.get("cron_id"):
            continue
        entry = stats_for(_attribute_session_project(session, visible_project_ids))
        entry["session_count"] += 1
        for key in ("last_message_at", "last_user_message_at"):
            value = session.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if entry[key] is None or value > entry[key]:
                    entry[key] = value

    zero = {"session_count": 0, "last_message_at": None, "last_user_message_at": None}

    def item(project: Any | None, default_id: str | None = None) -> dict[str, Any]:
        if default_id is not None:
            return _project_info_payload(None, default_id=default_id, stats=stats.get(default_id, zero))
        return _project_info_payload(
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
        pinned = [item(project) for project in projects if project.pinned]
        pinned.sort(key=lambda info: info["pin_order"])
        unpinned = [item(p) for p in projects if not p.pinned and (include_hidden or not p.hidden)]
        unpinned.sort(key=user_sort, reverse=True)
        result = pinned + unpinned + default_items
    return {"projects": result}, None, None


def _rename_project(
    params: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    project_id = str(params.get("project_id") or "").strip()
    if not project_id:
        return None, "project_id is required", "BAD_REQUEST"
    name = str(params.get("name") or "").strip()
    if not name:
        return None, "name is required", "BAD_REQUEST"
    if is_default_project_id(project_id):
        return None, "default project cannot be renamed", "FORBIDDEN"
    try:
        updated = project_store.rename_project(project_id, name)
    except project_store.ProjectNameConflict:
        return None, "project name already exists", "CONFLICT"
    except ValueError as exc:
        return None, str(exc), "BAD_REQUEST"
    if updated is None:
        return None, "project not found", "NOT_FOUND"
    return {
        "project_id": updated.project_id,
        "name": updated.name,
        "work_mode": updated.work_mode or DEFAULT_WEB_WORK_MODE,
    }, None, None


def _pin_project(
    params: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    project_id = str(params.get("project_id") or "").strip()
    if not project_id:
        return None, "project_id is required", "BAD_REQUEST"
    pinned = params.get("pinned")
    if not isinstance(pinned, bool):
        return None, "pinned must be boolean", "BAD_REQUEST"
    if is_default_project_id(project_id):
        return None, "default project cannot be pinned", "FORBIDDEN"
    project = project_store.get_project_by_id(project_id, cache_bust=True)
    if project is None or project.hidden:
        return None, "project not found", "NOT_FOUND"
    project.pinned = pinned
    if not pinned:
        project.pin_order = 0
    project_store.save_project(project)
    project_store.reindex_project_pin_orders()
    updated = project_store.get_project_by_id(project_id, cache_bust=True)
    return {
        "pinned": pinned,
        "pin_order": updated.pin_order if updated is not None else 0,
    }, None, None


def _create_project(
    params: dict[str, Any], channel_id: str
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Create/restore a project using this AgentServer's injected directory."""
    name = str(params.get("name") or "").strip()
    if not name:
        return None, "name is required", "BAD_REQUEST"
    project_dir = str(params.get("project_dir") or "").strip()
    if project_dir and not os.path.isabs(project_dir):
        return None, "project_dir must be an absolute path", "BAD_REQUEST"
    if project_dir and not os.path.isdir(project_dir):
        return None, "project directory does not exist", "PROJECT_DIR_MISSING"

    work_mode, mode_error = resolve_request_work_mode(params, channel_id)
    if mode_error is not None:
        return None, f"invalid work_mode: {params.get('work_mode')!r}", mode_error
    if not project_dir:
        try:
            project_dir = project_store.resolve_default_project_dir(name, work_mode)
        except ValueError as exc:
            return None, str(exc), "BAD_REQUEST"
        try:
            os.makedirs(project_dir, exist_ok=True)
        except OSError as exc:
            return None, f"failed to create project directory: {exc}", "INTERNAL_ERROR"

    try:
        project, restored = project_store.create_or_restore_project(
            name, project_dir, work_mode
        )
    except project_store.ProjectDirConflict:
        return None, "project_dir already exists", "CONFLICT"
    except project_store.ProjectNameConflict:
        return None, "project name already exists", "CONFLICT"
    except ValueError as exc:
        return None, str(exc), "BAD_REQUEST"

    if not restored and project.work_mode == "code":
        try:
            from jiuwenswarm.server.runtime.session.project_git import (
                get_project_git_service,
            )

            get_project_git_service().ensure_on_project_create(project)
            refreshed = project_store.get_project_by_id(project.project_id, cache_bust=True)
            if refreshed is not None:
                project = refreshed
        except Exception as exc:  # noqa: BLE001 - Git probe must not block creation
            logger.warning(
                "[ProjectAdapter] git probe failed on project create (id=%s dir=%s): %s",
                project.project_id,
                project.project_dir,
                exc,
            )
    info = _project_info_payload(project)
    return {
        "project_id": project.project_id,
        "project_dir": project.project_dir,
        "restored": restored,
        "work_mode": project.work_mode or DEFAULT_WEB_WORK_MODE,
        "git": info["git"],
        "project": info,
    }, None, None


def _git_status_payload(project: Any, repo_status: Any) -> dict[str, Any]:
    """Build the stable Web Git-status payload in the AgentServer."""
    return {
        "project_id": project.project_id,
        "project_name": project.name,
        "project_dir": project.project_dir,
        "work_mode": project.work_mode,
        "repo": {
            "is_git": repo_status.is_git,
            "repo_root": repo_status.repo_root,
            "branch": repo_status.branch,
            "head": repo_status.head,
            "detached": repo_status.detached,
            "transient": repo_status.transient,
            "upstream": repo_status.upstream,
        },
        "working_tree": {
            "is_dirty": repo_status.is_dirty,
            "staged": repo_status.staged,
            "unstaged": repo_status.unstaged,
            "untracked": repo_status.untracked,
            "conflicted": repo_status.conflicted,
        },
        "branches": {
            "current": repo_status.branch,
            "locals": list(repo_status.local_branches),
            "remotes": list(repo_status.remote_branches),
        },
        "generated_at": time.time(),
    }


def _git_error_payload(error: Any) -> dict[str, Any]:
    """Encode Git errors without coupling the adapter to the Web channel."""
    git_error = getattr(error, "git_error", error)
    if hasattr(git_error, "to_dict") and hasattr(git_error, "code"):
        detail = git_error.to_dict()
        return {
            "error": str(getattr(git_error, "message", error)),
            "code": str(git_error.code),
            "detail": detail,
        }
    return {"error": f"handler error: {error}", "code": "INTERNAL_ERROR"}


def _run_git_status(
    params: dict[str, Any], *, probe: bool
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Run status/probe against the current AgentServer's project directory."""
    from jiuwenswarm.server.runtime.session.project_git import (
        get_project_git_service,
        resolve_git_project,
    )

    project_id = str(params.get("project_id") or "").strip()
    project, error, code = resolve_git_project(project_id, cache_bust=probe)
    if project is None:
        if not probe and code == "NOT_FOUND":
            code = "PROJECT_NOT_FOUND"
        return None, {"error": error or "project not found", "code": code or "BAD_REQUEST"}
    try:
        status = (
            get_project_git_service().probe(project)
            if probe
            else get_project_git_service().status(project)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ProjectAdapter] project.git.%s failed: %s", "probe" if probe else "status", exc)
        return None, _git_error_payload(exc)
    if status.error is not None:
        return None, _git_error_payload(status.error)
    return _git_status_payload(project, status), None


def _run_git_init(
    params: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Initialize Git in a code project owned by this AgentServer."""
    from jiuwenswarm.server.runtime.session.project_git import (
        get_project_git_service,
        resolve_git_project,
    )

    project_id = str(params.get("project_id") or "").strip()
    project, error, code = resolve_git_project(project_id, cache_bust=True)
    if project is None:
        return None, {"error": error or "project not found", "code": code or "BAD_REQUEST"}
    initial_branch = str(params.get("initial_branch") or "main").strip() or "main"
    try:
        status = get_project_git_service().init(project, initial_branch=initial_branch)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ProjectAdapter] project.git.init failed: %s", exc)
        return None, _git_error_payload(exc)
    if status.error is not None:
        return None, _git_error_payload(status.error)
    return _git_status_payload(project, status), None


def _run_git_branch_operation(
    params: dict[str, Any], *, create: bool
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Switch or create a branch in this AgentServer-owned worktree."""
    from jiuwenswarm.server.runtime.session.project_git import (
        get_project_git_service,
        resolve_git_project,
    )

    project_id = str(params.get("project_id") or "").strip()
    project, error, code = resolve_git_project(project_id, cache_bust=True)
    if project is None:
        return None, {"error": error or "project not found", "code": code or "BAD_REQUEST"}
    branch = str(params.get("branch") or "").strip()
    if not branch:
        return None, {"error": "branch is required", "code": "BAD_REQUEST"}

    if create:
        checkout = bool(params.get("checkout") if "checkout" in params else True)
        raw_start_point = params.get("start_point")
        start_point = str(raw_start_point).strip() or None if raw_start_point is not None else None
        try:
            result = get_project_git_service().create_branch(
                project, branch, checkout=checkout, start_point=start_point
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ProjectAdapter] project.git.create_branch failed: %s", exc)
            return None, _git_error_payload(exc)
        if not result.success:
            return None, _git_error_payload(result.error)
        return {
            "created": True,
            "checked_out": checkout,
            "branch": branch,
            "status": _git_status_payload(project, result.repo_status),
        }, None

    require_clean = bool(params.get("require_clean") or False)
    try:
        result = get_project_git_service().switch_branch(
            project, branch, require_clean=require_clean
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ProjectAdapter] project.git.switch_branch failed: %s", exc)
        return None, _git_error_payload(exc)
    if not result.success:
        return None, _git_error_payload(result.error)
    return {
        "switched": True,
        "previous_branch": result.previous_branch,
        "current_branch": result.repo_status.branch,
        "status": _git_status_payload(project, result.repo_status),
    }, None


def _strict_bool_param(
    params: dict[str, Any], key: str, *, default: bool
) -> tuple[bool, str | None]:
    """Accept only JSON booleans for Git's high-impact flags."""
    if key not in params:
        return default, None
    value = params[key]
    if isinstance(value, bool):
        return value, None
    return default, (
        f"{key} must be a JSON boolean (true/false), "
        f"got {type(value).__name__}: {value!r}"
    )


def _run_git_commit(
    params: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Validate and commit only through the target AgentServer's Git service."""
    from jiuwenswarm.server.runtime.session.project_git import (
        get_project_git_service,
        resolve_git_project,
    )

    project_id = str(params.get("project_id") or "").strip()
    project, error, code = resolve_git_project(project_id, cache_bust=True)
    if project is None:
        return None, {"error": error or "project not found", "code": code or "BAD_REQUEST"}
    message = params.get("message")
    if message is None:
        return None, {"error": "message is required", "code": "BAD_REQUEST"}
    if not isinstance(message, str):
        return None, {
            "error": f"message must be a string, got {type(message).__name__}: {message!r}",
            "code": "BAD_REQUEST",
        }
    if not message.strip():
        return None, {"error": "message must not be empty", "code": "BAD_REQUEST"}
    raw_paths = params.get("paths")
    paths: list[str] | None = None
    if raw_paths is not None:
        if not isinstance(raw_paths, list):
            return None, {"error": "paths must be an array of strings", "code": "BAD_REQUEST"}
        paths = []
        for index, path in enumerate(raw_paths):
            if not isinstance(path, str):
                return None, {
                    "error": f"paths[{index}] must be a string, got {type(path).__name__}: {path!r}",
                    "code": "BAD_REQUEST",
                }
            if not path.strip():
                return None, {
                    "error": f"paths[{index}] must not be empty or whitespace",
                    "code": "BAD_REQUEST",
                }
            paths.append(path)
        if not paths:
            return None, {"error": "paths is empty", "code": "BAD_REQUEST"}
    amend, bool_error = _strict_bool_param(params, "amend", default=False)
    if bool_error:
        return None, {"error": bool_error, "code": "BAD_REQUEST"}
    stage_all, bool_error = _strict_bool_param(
        params, "stage_all", default=(raw_paths is None and not amend)
    )
    if bool_error:
        return None, {"error": bool_error, "code": "BAD_REQUEST"}
    if stage_all and paths:
        return None, {"error": "stage_all and paths are mutually exclusive", "code": "BAD_REQUEST"}
    no_verify, bool_error = _strict_bool_param(params, "no_verify", default=False)
    if bool_error:
        return None, {"error": bool_error, "code": "BAD_REQUEST"}
    try:
        result = get_project_git_service().commit(
            project,
            message,
            stage_all=stage_all,
            paths=paths,
            amend=amend,
            no_verify=no_verify,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ProjectAdapter] project.git.commit failed: %s", exc)
        return None, _git_error_payload(exc)
    if not result.success:
        return None, _git_error_payload(result.error)
    return {
        "committed": True,
        "commit_hash": result.commit_hash,
        "amended": amend,
        "status": _git_status_payload(project, result.repo_status),
    }, None


def _run_git_push(
    params: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Validate and push only through the target AgentServer's Git service."""
    from jiuwenswarm.server.runtime.session.project_git import (
        get_project_git_service,
        resolve_git_project,
    )

    project_id = str(params.get("project_id") or "").strip()
    project, error, code = resolve_git_project(project_id, cache_bust=True)
    if project is None:
        return None, {"error": error or "project not found", "code": code or "BAD_REQUEST"}
    remote = str(params.get("remote") or "origin").strip() or "origin"
    raw_branch = params.get("branch")
    if raw_branch is not None and not isinstance(raw_branch, str):
        return None, {
            "error": f"branch must be a string, got {type(raw_branch).__name__}: {raw_branch!r}",
            "code": "BAD_REQUEST",
        }
    branch = raw_branch.strip() or None if raw_branch is not None else None
    set_upstream, bool_error = _strict_bool_param(params, "set_upstream", default=False)
    if bool_error:
        return None, {"error": bool_error, "code": "BAD_REQUEST"}
    force, bool_error = _strict_bool_param(params, "force", default=False)
    if bool_error:
        return None, {"error": bool_error, "code": "BAD_REQUEST"}
    delete, bool_error = _strict_bool_param(params, "delete", default=False)
    if bool_error:
        return None, {"error": bool_error, "code": "BAD_REQUEST"}
    if delete and (set_upstream or force):
        return None, {
            "error": "delete is mutually exclusive with set_upstream and force",
            "code": "BAD_REQUEST",
        }
    try:
        result = get_project_git_service().push(
            project,
            remote=remote,
            branch=branch,
            set_upstream=set_upstream,
            force=force,
            delete=delete,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ProjectAdapter] project.git.push failed: %s", exc)
        return None, _git_error_payload(exc)
    if not result.success:
        return None, _git_error_payload(result.error)
    return {
        "pushed": True,
        "remote": result.pushed_remote or remote,
        "branch": branch or result.repo_status.branch,
        "deleted": delete,
        "upstream_set": set_upstream,
        "status": _git_status_payload(project, result.repo_status),
    }, None


def _run_git_diff_status(
    params: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Read diff state and session history from this AgentServer's user data."""
    from jiuwenswarm.server.runtime.session.git_diff_status import get_diff_status_service
    from jiuwenswarm.server.runtime.session.project_git import resolve_git_project

    project_id = str(params.get("project_id") or "").strip()
    project, error, code = resolve_git_project(project_id)
    if project is None:
        if code == "NOT_FOUND":
            code = "PROJECT_NOT_FOUND"
        return None, {"error": error or "project not found", "code": code or "BAD_REQUEST"}
    raw_session_id = params.get("session_id")
    session_id = str(raw_session_id).strip() or None if raw_session_id is not None else None
    include_hunks = bool(params.get("include_hunks") or False)
    include_files = bool(params.get("include_files") or False) or include_hunks
    hunk_paths: list[str] | None = None
    raw_hunk_paths = params.get("hunk_paths")
    if isinstance(raw_hunk_paths, list):
        hunk_paths = [str(p) for p in raw_hunk_paths if isinstance(p, str) and p.strip()]
        if not hunk_paths:
            hunk_paths = None
    try:
        status = get_diff_status_service().get_project_diff_status(
            project=project,
            session_id=session_id,
            include_files=include_files,
            include_hunks=include_hunks,
            hunk_paths=hunk_paths,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ProjectAdapter] project.git.diff_status failed: %s", exc)
        return None, _git_error_payload(exc)
    return status.to_dict(include_hunks=include_hunks), None


def _validate_diff_session_binding(
    project_id: str, session_id: str
) -> tuple[str | None, str | None]:
    """Confirm that session history belongs to the requested user project."""
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

    try:
        metadata = get_session_metadata(
            session_id, cache_bust=True, enable_writeback=False
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ProjectAdapter] failed to read session metadata: %s", exc)
        return f"failed to read session metadata: {exc}", "INTERNAL_ERROR"
    if not metadata:
        return f"session not found: {session_id}", "SESSION_NOT_FOUND"
    session_project_id = str(metadata.get("project_id") or "").strip()
    if not session_project_id:
        return "session has no project_id binding; cannot verify project ownership", "SESSION_NOT_BOUND"
    if session_project_id != project_id:
        return (
            f"session_id does not belong to project_id: expected {project_id}, "
            f"got {session_project_id}",
            "PROJECT_SESSION_MISMATCH",
        )
    return None, None


def _resolve_diff_project(
    params: dict[str, Any]
) -> tuple[Any | None, str | None, str | None]:
    from jiuwenswarm.server.runtime.session.project_git import resolve_git_project

    return resolve_git_project(str(params.get("project_id") or "").strip())


def _run_turn_diff_list(
    params: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    from jiuwenswarm.server.runtime.session.git_diff_status import get_diff_status_service

    project, error, code = _resolve_diff_project(params)
    if project is None:
        return None, {"error": error or "project not found", "code": code or "BAD_REQUEST"}
    session_id = str(params.get("session_id") or "").strip()
    if not session_id:
        return None, {"error": "session_id is required", "code": "BAD_REQUEST"}
    binding_error, binding_code = _validate_diff_session_binding(project.project_id, session_id)
    if binding_error:
        return None, {"error": binding_error, "code": binding_code}
    try:
        limit = int(params.get("limit", 50))
    except (ValueError, TypeError):
        limit = 50
    try:
        cursor = int(params.get("cursor", 0))
    except (ValueError, TypeError):
        cursor = 0
    try:
        payload = get_diff_status_service().get_turn_diff_list(
            project=project,
            session_id=session_id,
            limit=50 if limit < 0 else limit,
            cursor=0 if cursor < 0 else cursor,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ProjectAdapter] project.git.turn_diff_list failed: %s", exc)
        return None, _git_error_payload(exc)
    return payload, None


def _permissive_bool(params: dict[str, Any], key: str, default: bool) -> bool:
    value = params.get(key, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _run_turn_diff(
    params: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    from jiuwenswarm.server.runtime.session.git_diff_status import get_diff_status_service
    from jiuwenswarm.server.utils.diff_service import DiffHistoryExpiredError

    project, error, code = _resolve_diff_project(params)
    if project is None:
        return None, {"error": error or "project not found", "code": code or "BAD_REQUEST"}
    session_id = str(params.get("session_id") or "").strip()
    if not session_id:
        return None, {"error": "session_id is required", "code": "BAD_REQUEST"}
    change_set_id = str(params.get("change_set_id") or "").strip() or None
    turn_index: int | None = None
    if change_set_id is None:
        try:
            turn_index = int(params.get("turn_index"))
        except (ValueError, TypeError):
            return None, {
                "error": "either change_set_id or turn_index (integer) is required",
                "code": "BAD_REQUEST",
            }
    include_hunks = _permissive_bool(params, "include_hunks", True)
    include_files = _permissive_bool(params, "include_files", True) or include_hunks
    binding_error, binding_code = _validate_diff_session_binding(project.project_id, session_id)
    if binding_error:
        return None, {"error": binding_error, "code": binding_code}
    try:
        payload = get_diff_status_service().get_turn_diff_detail(
            project=project,
            session_id=session_id,
            turn_index=turn_index,
            change_set_id=change_set_id,
            include_files=include_files,
            include_hunks=include_hunks,
        )
    except DiffHistoryExpiredError as exc:
        return None, {"error": str(exc) or "diff history expired", "code": "DIFF_HISTORY_EXPIRED"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ProjectAdapter] project.git.turn_diff failed: %s", exc)
        return None, _git_error_payload(exc)
    if payload is None:
        identifier = change_set_id or f"turn_index={turn_index}"
        return None, {
            "error": f"turn diff not found for {identifier}",
            "code": "CHANGE_SET_NOT_FOUND" if change_set_id else "TURN_DIFF_NOT_FOUND",
        }
    return payload, None


def _resolve_turn_operation(
    params: dict[str, Any]
) -> tuple[Any | None, str, dict[str, Any] | None]:
    """Validate the project/session pair in this AgentServer's data directory."""
    from jiuwenswarm.server.runtime.session.project_git import resolve_git_project

    project_id = str(params.get("project_id") or "").strip()
    project, error, code = resolve_git_project(project_id, cache_bust=True)
    if project is None:
        return None, "", {"error": error or "project not found", "code": code or "BAD_REQUEST"}
    session_id = str(params.get("session_id") or "").strip()
    if not session_id:
        return None, "", {"error": "session_id is required", "code": "BAD_REQUEST"}
    binding_error, binding_code = _validate_diff_session_binding(project_id, session_id)
    if binding_error:
        return None, "", {"error": binding_error, "code": binding_code}
    return project, session_id, None


def _run_discard_turn_changes(
    params: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Restore the latest turn and update its diff history in AgentServer."""
    from jiuwenswarm.agents.harness.common.session_ops_service import (
        get_last_turn_info,
        restore_session_files,
    )
    from jiuwenswarm.server.runtime.session.git_diff_status import (
        get_session_extra_history_roots,
    )
    from jiuwenswarm.server.utils.diff_service import get_diff_service

    project, session_id, failure = _resolve_turn_operation(params)
    if failure is not None:
        return {}, failure
    last_turn = get_last_turn_info(session_id=session_id)
    turn_index = last_turn["turn_index"]
    cut_timestamp = last_turn["timestamp"]
    if turn_index <= 0:
        return {}, {"error": "no turn to discard: session has no user messages", "code": "NO_TURN_TO_DISCARD"}
    roots = get_session_extra_history_roots(session_id)
    try:
        restored = restore_session_files(
            session_id=session_id,
            turn_index=turn_index,
            project_dir=project.project_dir,
            extra_history_roots=roots,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ProjectAdapter] discard_turn_changes restore failed: %s", exc)
        return {}, {"error": f"failed to restore session files: {exc}", "code": "INTERNAL_ERROR"}
    errors = restored.get("errors", []) or []
    change_set_id: str | None = None
    truncated = False
    if cut_timestamp > 0 and not errors:
        service = get_diff_service()
        change_set_id = service.mark_turn_discarded(
            session_id, turn_index, project_dir=project.project_dir, extra_history_roots=roots
        )
        service.truncate_file_ops_by_timestamp(
            session_id, cut_timestamp, soft=True, discarded=True,
            project_dir=project.project_dir, extra_history_roots=roots,
        )
        truncated = True
    partial = bool(errors)
    return {
        "session_id": session_id,
        "turn_index": turn_index,
        "change_set_id": change_set_id,
        "restored_files": restored.get("restored_files", []),
        "deleted_files": restored.get("deleted_files", []),
        "errors": errors,
        "file_ops_truncated": truncated,
        "global_file_ops_truncated": False,
        "partial": partial,
    }, (
        {
            "error": f"partial failure: {len(errors)} file(s) failed to restore; file_ops not truncated, retryable",
            "code": "PARTIAL_RESTORE_FAILED",
            "result": "partial",
        }
        if partial else None
    )


def _run_redo_turn_changes(
    params: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Reapply the latest discarded turn in this AgentServer's worktree."""
    from jiuwenswarm.agents.harness.common.session_ops_service import (
        get_last_turn_info,
        redo_session_files,
    )
    from jiuwenswarm.server.runtime.session.git_diff_status import (
        get_session_extra_history_roots,
    )
    from jiuwenswarm.server.utils.diff_service import get_diff_service

    project, session_id, failure = _resolve_turn_operation(params)
    if failure is not None:
        return {}, failure
    turn_index = get_last_turn_info(session_id=session_id)["turn_index"]
    if turn_index <= 0:
        return {}, {"error": "no turn to redo: session has no user messages", "code": "NO_TURN_TO_REDO"}
    roots = get_session_extra_history_roots(session_id)
    service = get_diff_service()
    target = service.get_turn_diff(
        session_id, turn_index=turn_index, project_dir=project.project_dir,
        extra_history_roots=roots,
    )
    status = str((target or {}).get("status") or "")
    if status != "discarded":
        return {}, {
            "error": f"last turn (index={turn_index}) is not discarded (status={status or 'unknown'}); nothing to redo",
            "code": "NOTHING_TO_REDO",
        }
    try:
        redone = redo_session_files(
            session_id=session_id,
            turn_index=turn_index,
            project_dir=project.project_dir,
            extra_history_roots=roots,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ProjectAdapter] redo_turn_changes failed: %s", exc)
        return {}, {"error": f"failed to redo session files: {exc}", "code": "INTERNAL_ERROR"}
    errors = redone.get("errors", []) or []
    redone_files = redone.get("redone_files", []) or []
    deleted_files = redone.get("deleted_files", []) or []
    base = {
        "session_id": session_id,
        "turn_index": turn_index,
        "redone_files": redone_files,
        "deleted_files": deleted_files,
        "errors": errors,
    }
    if not errors and not redone_files and not deleted_files:
        return base, {
            "error": (
                "no redoable files found: file_ops for this discarded turn is "
                "missing or has no discarded_out entries; discarded status preserved"
            ),
            "code": "REDO_HISTORY_MISSING",
        }
    change_set_id = None
    if not errors:
        change_set_id = service.unmark_turn_discarded(
            session_id, turn_index, project_dir=project.project_dir, extra_history_roots=roots
        )
    partial = bool(errors)
    return {
        **base,
        "change_set_id": change_set_id,
        "partial": partial,
    }, (
        {
            "error": f"partial failure: {len(errors)} file(s) failed to redo; discarded status not cleared, retryable",
            "code": "PARTIAL_REDO_FAILED",
            "result": "partial",
        }
        if partial else None
    )


def _ok_response(request: AgentRequest, payload: Any) -> AgentResponse:
    """构造成功 AgentResponse（请求元数据透传）。"""
    return AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=True,
        payload=payload,
        metadata=request.metadata,
    )


def _request_params(request: AgentRequest) -> dict[str, Any]:
    """返回请求 params（非 dict 视为空）。"""
    return request.params if isinstance(request.params, dict) else {}


async def _run_threaded(
    request: AgentRequest,
    label: str,
    fn: Any,
    *args: Any,
    result_mode: str = "triple",
    **fn_kwargs: Any,
) -> AgentResponse:
    """在事件循环外执行 project 域同步函数，并把结果映射为 AgentResponse。

    ``result_mode`` 对应本域三类返回契约：
    - ``triple``: ``(payload, error, code)``（project 管理域）；error 非空 → 失败响应；
    - ``dual_replace``: ``(payload, error_payload)``（Git 域）；失败时 payload 直接
      采用 error_payload（结构化错误明细）；
    - ``dual_merge``: ``(payload, error_payload)``（discard/redo）；失败时在 payload
      上合并 error 字段（保留部分成功信息）；
    - ``plain``: 直接返回 payload（无错误语义，如 pinned_sessions）。
    统一异常映射：函数抛错 → ``INTERNAL_ERROR``。
    """
    try:
        result = await asyncio.to_thread(fn, *args, **fn_kwargs)
    except LifecycleError as exc:
        return build_error_response(
            request, str(exc), code=exc.code, extra=exc.details
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ProjectAdapter] %s failed: %s", label, exc)
        return build_error_response(request, str(exc), code="INTERNAL_ERROR")

    if result_mode == "triple":
        payload, error, code = result
        if error is not None:
            return build_error_response(request, error, code=code or "INTERNAL_ERROR")
        return _ok_response(request, payload)
    if result_mode == "dual_replace":
        payload, error_payload = result
        if error_payload is not None:
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload=error_payload,
                metadata=request.metadata,
            )
        return _ok_response(request, payload)
    if result_mode == "dual_merge":
        payload, error_payload = result
        if error_payload is not None:
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    **payload,
                    **{key: value for key, value in error_payload.items() if key != "result"},
                },
                metadata=request.metadata,
            )
        return _ok_response(request, payload)
    return _ok_response(request, result)


class ProjectAdapter(GatewayAdapter):
    """Project-domain adapter: project/git CRUD, diff status, session queries."""

    methods: frozenset[str] = frozenset(
        {
            ReqMethod.PROJECT_INFO.value,
            ReqMethod.PROJECT_PINNED_SESSIONS.value,
            ReqMethod.PROJECT_GET_SESSIONS.value,
            ReqMethod.PROJECT_GET_CRON_SESSIONS.value,
            ReqMethod.PROJECT_CRON_RESOLVE_BINDING.value,
            ReqMethod.PROJECT_LIST.value,
            ReqMethod.PROJECT_CREATE.value,
            ReqMethod.PROJECT_RENAME.value,
            ReqMethod.PROJECT_PIN.value,
            ReqMethod.PROJECT_GIT_STATUS.value,
            ReqMethod.PROJECT_GIT_PROBE.value,
            ReqMethod.PROJECT_GIT_INIT.value,
            ReqMethod.PROJECT_GIT_SWITCH_BRANCH.value,
            ReqMethod.PROJECT_GIT_CREATE_BRANCH.value,
            ReqMethod.PROJECT_GIT_COMMIT.value,
            ReqMethod.PROJECT_GIT_PUSH.value,
            ReqMethod.PROJECT_GIT_DIFF_STATUS.value,
            ReqMethod.PROJECT_GIT_TURN_DIFF_LIST.value,
            ReqMethod.PROJECT_GIT_TURN_DIFF.value,
            ReqMethod.PROJECT_GIT_DISCARD_TURN_CHANGES.value,
            ReqMethod.PROJECT_GIT_REDO_TURN_CHANGES.value,
        }
    )

    async def handle(self, request: AgentRequest) -> AgentResponse:
        method = request.req_method
        params = _request_params(request)
        if method not in {ReqMethod.PROJECT_LIST, ReqMethod.PROJECT_INFO, ReqMethod.PROJECT_PINNED_SESSIONS,
                          ReqMethod.PROJECT_GET_SESSIONS, ReqMethod.PROJECT_GET_CRON_SESSIONS}:
            from jiuwenswarm.server.runtime.session.lifecycle import guard
            try:
                guard(project_id=str(params.get("project_id") or ""))
            except LifecycleError as exc:
                return build_error_response(request, str(exc), code=exc.code)

        if method == ReqMethod.PROJECT_GIT_DISCARD_TURN_CHANGES:
            return await _run_threaded(
                request, "project.git.discard_turn_changes",
                _run_discard_turn_changes, params, result_mode="dual_merge",
            )
        if method == ReqMethod.PROJECT_GIT_REDO_TURN_CHANGES:
            return await _run_threaded(
                request, "project.git.redo_turn_changes",
                _run_redo_turn_changes, params, result_mode="dual_merge",
            )
        if method == ReqMethod.PROJECT_GIT_TURN_DIFF_LIST:
            return await _run_threaded(
                request, "project.git.turn_diff_list",
                _run_turn_diff_list, params, result_mode="dual_replace",
            )
        if method == ReqMethod.PROJECT_GIT_TURN_DIFF:
            return await _run_threaded(
                request, "project.git.turn_diff",
                _run_turn_diff, params, result_mode="dual_replace",
            )
        if method == ReqMethod.PROJECT_GIT_DIFF_STATUS:
            return await _run_threaded(
                request, "project.git.diff_status",
                _run_git_diff_status, params, result_mode="dual_replace",
            )
        if method in {ReqMethod.PROJECT_GIT_COMMIT, ReqMethod.PROJECT_GIT_PUSH}:
            fn = _run_git_commit if method == ReqMethod.PROJECT_GIT_COMMIT else _run_git_push
            return await _run_threaded(
                request, method.value, fn, params, result_mode="dual_replace",
            )
        if method in {ReqMethod.PROJECT_GIT_SWITCH_BRANCH, ReqMethod.PROJECT_GIT_CREATE_BRANCH}:
            create = method == ReqMethod.PROJECT_GIT_CREATE_BRANCH
            return await _run_threaded(
                request, method.value, _run_git_branch_operation, params,
                result_mode="dual_replace", create=create,
            )
        if method == ReqMethod.PROJECT_GIT_INIT:
            return await _run_threaded(
                request, "project.git.init",
                _run_git_init, params, result_mode="dual_replace",
            )
        if method in {ReqMethod.PROJECT_GIT_STATUS, ReqMethod.PROJECT_GIT_PROBE}:
            probe = method == ReqMethod.PROJECT_GIT_PROBE
            return await _run_threaded(
                request, method.value, _run_git_status, params,
                result_mode="dual_replace", probe=probe,
            )
        if method == ReqMethod.PROJECT_LIST:
            return await _run_threaded(request, "project.list", _load_project_list, params)
        if method == ReqMethod.PROJECT_RENAME:
            return await _run_threaded(request, "project.rename", _rename_project, params)
        if method == ReqMethod.PROJECT_PIN:
            return await _run_threaded(request, "project.pin", _pin_project, params)
        if method == ReqMethod.PROJECT_CREATE:
            return await _run_threaded(
                request, "project.create", _create_project, params, request.channel_id,
            )
        if method == ReqMethod.PROJECT_PINNED_SESSIONS:
            return await _run_threaded(
                request, "project.pinned_sessions", _load_pinned_sessions,
                result_mode="plain",
            )
        if method == ReqMethod.PROJECT_GET_SESSIONS:
            return await _run_threaded(
                request, "project.get_sessions", _load_project_sessions,
                params, request.user_id,
            )
        if method == ReqMethod.PROJECT_GET_CRON_SESSIONS:
            return await _run_threaded(
                request, "project.get_cron_sessions", _load_project_cron_sessions,
                params, request.user_id,
            )
        if method == ReqMethod.PROJECT_CRON_RESOLVE_BINDING:
            return await _run_threaded(
                request, "project.cron.resolve_binding", _resolve_cron_binding,
                params, request.channel_id,
            )
        if method == ReqMethod.PROJECT_INFO:
            return await _run_threaded(request, "project.info", _load_project_info, params)
        return build_error_response(
            request, f"unsupported method: {method}", code="BAD_REQUEST"
        )
