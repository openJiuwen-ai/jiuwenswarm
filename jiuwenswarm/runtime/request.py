# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Transport-independent request normalization for the shared Runtime."""

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import TYPE_CHECKING, Any

from jiuwenswarm.common.mode_matrix import (
    ResolvedMode,
    TEAM_PLAN_CODE_MODE,
    TEAM_PLAN_NORMAL_MODE,
    canonicalize_mode_text,
    is_code_profile_mode,
    resolve_new_canonical_mode,
    resolve_request_mode,
)
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable

    from jiuwenswarm.server.runtime.agent_manager import AgentManager

PREVIOUS_SESSION_MODE_KEY = "_session_previous_mode"

CHAT_TURN_METHODS = frozenset(
    {
        ReqMethod.CHAT_SEND,
        ReqMethod.CHAT_RESUME,
        ReqMethod.CHAT_ANSWER,
    }
)


def resolve_request_project_dir(
    request: AgentRequest,
    *,
    include_legacy_fallbacks: bool = True,
) -> str | None:
    """Resolve the stable project identity used to construct an agent."""
    params = request.params or {}
    project_dir = params.get("project_dir")
    if isinstance(project_dir, str) and project_dir.strip():
        return project_dir.strip()

    metadata = request.metadata or {}
    metadata_project_dir = (
        metadata.get("project_dir") if isinstance(metadata, dict) else None
    )
    if isinstance(metadata_project_dir, str) and metadata_project_dir.strip():
        return metadata_project_dir.strip()

    # Agent/Code projectless turns deliberately keep cwd dynamic so the
    # adapter can allocate the session's task workspace. Legacy callers and
    # Team requests may still opt into deriving the project identity from cwd
    # or trusted_dirs.
    if not include_legacy_fallbacks:
        return None

    cwd = params.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        return cwd.strip()
    metadata_cwd = metadata.get("cwd") if isinstance(metadata, dict) else None
    if isinstance(metadata_cwd, str) and metadata_cwd.strip():
        return metadata_cwd.strip()

    trusted_dirs = params.get("trusted_dirs")
    if isinstance(trusted_dirs, list) and trusted_dirs:
        first = trusted_dirs[0]
        if isinstance(first, str) and first.strip():
            return first.strip()
    return None


def _resolve_cancel_project_dir(request: AgentRequest) -> str | None:
    """Resolve the project cache key for a transport-neutral cancel request.

    Cron and other remote callers may only know ``project_id``. The target
    Runtime owns the user-scoped session metadata and project registry, so it
    resolves that identity locally instead of requiring Gateway filesystem
    access.
    """
    direct = resolve_request_project_dir(request)
    if direct:
        return direct

    params = request.params if isinstance(request.params, dict) else {}
    request_metadata = request.metadata if isinstance(request.metadata, dict) else {}
    session_metadata: dict[str, Any] = {}
    session_id = str(request.session_id or "").strip()
    if session_id:
        try:
            from jiuwenswarm.server.runtime.session.session_metadata import (
                get_session_metadata,
            )

            session_metadata = get_session_metadata(
                session_id,
                enable_writeback=False,
            ) or {}
        except (OSError, ValueError) as exc:
            logger.warning(
                "Failed to resolve cancel session metadata for %s: %s",
                session_id,
                exc,
            )

    stored_project_dir = session_metadata.get("project_dir")
    if isinstance(stored_project_dir, str) and stored_project_dir.strip():
        return stored_project_dir.strip()

    project_id = ""
    for value in (
        params.get("project_id"),
        request_metadata.get("project_id"),
        session_metadata.get("project_id"),
    ):
        if isinstance(value, str) and value.strip():
            project_id = value.strip()
            break
    if not project_id:
        return None
    try:
        from jiuwenswarm.server.runtime.session.project_store import (
            get_project_dir_by_id,
        )

        project_dir = get_project_dir_by_id(project_id)
    except (OSError, ValueError) as exc:
        logger.warning(
            "Failed to resolve cancel project_id %s: %s",
            project_id,
            exc,
        )
        return None
    return (
        project_dir.strip()
        if isinstance(project_dir, str) and project_dir.strip()
        else None
    )


def sync_chat_request_metadata(
    request: AgentRequest,
    project_dir: str | None,
    mode: str,
    explicit_mode_provided: bool = False,
    user_id: str = "",
) -> str | None:
    """Persist chat request metadata and return the locked project directory."""
    session_id = (request.session_id or "").strip()
    if not session_id:
        return project_dir
    params = request.params if isinstance(request.params, dict) else {}
    raw_model_name = params.get("model_name")
    explicit_model_provided = isinstance(raw_model_name, str) and bool(
        raw_model_name.strip()
    )
    model_name = (
        raw_model_name.strip()
        if explicit_model_provided
        else os.getenv("MODEL_NAME", "") or None
    )

    request_project_id = params.get("project_id")
    request_project_id = (
        request_project_id.strip()
        if isinstance(request_project_id, str) and request_project_id.strip()
        else None
    )
    request_cron_id = params.get("cron_id")
    request_cron_id = (
        request_cron_id.strip()
        if isinstance(request_cron_id, str) and request_cron_id.strip()
        else None
    )
    is_chat_turn = request.req_method in CHAT_TURN_METHODS
    legacy_eternal_value = params.get("eternal_conversation_enabled")
    legacy_persist_session: bool | None = None
    if isinstance(legacy_eternal_value, bool):
        legacy_persist_session = legacy_eternal_value
    elif isinstance(legacy_eternal_value, str):
        normalized_legacy = legacy_eternal_value.strip().casefold()
        if normalized_legacy in {"1", "true", "yes", "on", "enabled"}:
            legacy_persist_session = True
        elif normalized_legacy in {"0", "false", "no", "off", "disabled"}:
            legacy_persist_session = False
    try:
        from jiuwenswarm.server.runtime.session.session_metadata import (
            sync_session_request_metadata,
        )

        return sync_session_request_metadata(
            session_id=session_id,
            channel_id=request.channel_id or None,
            mode=mode,
            model=model_name,
            project_dir=str(project_dir) if project_dir else None,
            project_id=request_project_id,
            cron_id=request_cron_id,
            user_id=str(user_id or "").strip() or None,
            last_user_message_at=(
                dt.datetime.now(dt.timezone.utc).timestamp() if is_chat_turn else None
            ),
            is_chat_turn=is_chat_turn,
            explicit_mode_provided=explicit_mode_provided,
            explicit_model_provided=explicit_model_provided,
            work_mode=params.get("work_mode"),
            persist_session=legacy_persist_session,
        )
    except (OSError, ValueError) as exc:
        logger.warning("Failed to synchronize Runtime request metadata: %s", exc)
        return project_dir


def resolve_agent_request_mode(
    raw_mode: Any,
    *,
    work_mode: Any = None,
) -> tuple[str, str | None, str]:
    """Resolve a mode into manager mode, sub-mode, and canonical mode."""
    mode_text = canonicalize_mode_text(raw_mode)

    # Three-segment canonical values already encode both profile and state.
    # They must not be folded again through a supplemented ``work_mode``.
    new_mode_resolved = resolve_new_canonical_mode(mode_text)
    if new_mode_resolved is not None:
        return new_mode_resolved

    normalized_work_mode = (
        work_mode.strip().lower() if isinstance(work_mode, str) else ""
    )

    if mode_text in ("plan", "fast"):
        if normalized_work_mode == "code":
            return "code", "normal", "code.normal"
        return "agent", None, "agent"

    if mode_text == TEAM_PLAN_NORMAL_MODE:
        return "team", "plan", TEAM_PLAN_NORMAL_MODE
    if mode_text == TEAM_PLAN_CODE_MODE:
        return "code", "team", TEAM_PLAN_CODE_MODE

    # ``agent.plan`` is a real legacy plan mode, unlike the merged bare
    # ``plan``/``fast`` aliases above. Preserve its plan semantics when the
    # request does not carry a valid composable work mode.
    if mode_text == "agent.plan":
        if normalized_work_mode == "code":
            return "code", "plan", "code.plan"
        return "agent", "plan", "agent.plan"

    parts = mode_text.split(".")
    mode = parts[0] or "agent"
    if mode == "agent":
        if normalized_work_mode == "code":
            return "code", "normal", "code.normal"
        return "agent", None, "agent"
    if mode == "team":
        sub_mode = parts[1] if len(parts) > 1 and parts[1] else None
        if sub_mode is not None:
            sub_mode = None
        canonical_mode = f"team.{sub_mode}" if sub_mode else "team"
        return "team", sub_mode, canonical_mode

    default_sub_modes = {"code": "normal"}
    sub_mode = parts[1] if len(parts) > 1 and parts[1] else default_sub_modes.get(mode)
    if mode == "code" and sub_mode not in {"plan", "normal", "team"}:
        sub_mode = default_sub_modes.get(mode, "normal")
    canonical_mode = f"{mode}.{sub_mode}" if sub_mode else mode
    if canonical_mode in {"agent", "code", "code.normal"}:
        if normalized_work_mode == "code":
            return "code", "normal", "code.normal"
        if normalized_work_mode == "work":
            return "agent", None, "agent"
    return mode, sub_mode, canonical_mode


def resolve_request_runtime_mode(
    request: AgentRequest,
    *,
    work_mode: Any = None,
) -> ResolvedMode:
    """Resolve the complete Runtime mode for a request."""
    params = request.params if isinstance(request.params, dict) else {}
    return resolve_request_mode(
        params,
        resolve_agent_request_mode,
        work_mode=work_mode,
    )


def apply_resolved_mode_to_request(
    request: AgentRequest,
    *,
    work_mode: Any = None,
) -> tuple[str, str | None]:
    """Canonicalize the request mode and return manager routing values."""
    if not hasattr(request, "_original_mode") and isinstance(request.params, dict):
        raw_mode = request.params.get("mode")
        if isinstance(raw_mode, str) and raw_mode.strip():
            setattr(request, "_original_mode", canonicalize_mode_text(raw_mode))
    resolved = resolve_request_runtime_mode(request, work_mode=work_mode)
    if isinstance(request.params, dict):
        request.params["mode"] = resolved.canonical_mode
    return resolved.manager_mode, resolved.sub_mode


async def prepare_chat_turn(
    agent_manager: AgentManager,
    request: AgentRequest,
    channel_id: str,
    *,
    sync_metadata: bool = True,
    metadata_sync: Callable[..., str | None] = sync_chat_request_metadata,
) -> tuple[str, str | None, Any]:
    """Resolve session semantics and select an agent from the shared manager."""
    params = request.params if isinstance(request.params, dict) else {}
    raw_mode = params.get("mode")
    explicit_mode_provided = isinstance(raw_mode, str) and bool(raw_mode.strip())
    runtime_work_mode = None
    session_metadata: dict[str, Any] = {}
    session_id = str(request.session_id or "").strip()
    if session_id:
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
        )

        session_metadata = get_session_metadata(
            session_id,
            cache_bust=True,
            enable_writeback=False,
        )
        stored_work_mode = (
            session_metadata.get("work_mode")
            if isinstance(session_metadata, dict)
            else None
        )
        stored_session_mode = (
            session_metadata.get("mode") if isinstance(session_metadata, dict) else None
        )
        if isinstance(stored_session_mode, str) and stored_session_mode.strip():
            stored_session_mode = stored_session_mode.strip()
            params[PREVIOUS_SESSION_MODE_KEY] = stored_session_mode
            if not explicit_mode_provided:
                # Internal turns (including Heartbeat) inherit the Session's
                # locked mode without turning that inheritance into an
                # explicit client-requested transition.
                params["mode"] = stored_session_mode
        if isinstance(stored_work_mode, str) and stored_work_mode.strip().lower() in {
            "code",
            "work",
        }:
            runtime_work_mode = stored_work_mode.strip().lower()
        elif isinstance(stored_session_mode, str) and stored_session_mode:
            # Legacy metadata may predate ``work_mode``. Derive the profile
            # from the locked canonical mode instead of the channel default.
            runtime_work_mode = (
                "code" if is_code_profile_mode(stored_session_mode) else "work"
            )

    if runtime_work_mode is None:
        request_work_mode = params.get("work_mode")
        if isinstance(request_work_mode, str) and request_work_mode.strip().lower() in {
            "code",
            "work",
        }:
            runtime_work_mode = request_work_mode.strip().lower()
        else:
            from jiuwenswarm.server.runtime.session.work_mode import (
                default_work_mode_for_channel,
            )

            channel_id_for_default = channel_id or request.channel_id or "web"
            runtime_work_mode = default_work_mode_for_channel(channel_id_for_default)
            logger.warning(
                "Runtime request has no work_mode; defaulting to %r for "
                "channel=%s session=%s",
                runtime_work_mode,
                channel_id_for_default,
                request.session_id,
            )

    params["work_mode"] = runtime_work_mode
    mode, sub_mode = apply_resolved_mode_to_request(
        request,
        work_mode=runtime_work_mode,
    )
    agent_mode = "agent" if mode == "auto_harness" else mode
    requested_project_dir = resolve_request_project_dir(
        request,
        include_legacy_fallbacks=mode not in {"agent", "code"},
    )
    canonical_mode = (
        request.params.get("mode") if isinstance(request.params, dict) else None
    )
    if sync_metadata:
        project_dir = metadata_sync(
            request,
            requested_project_dir,
            canonical_mode if canonical_mode else mode,
            explicit_mode_provided=explicit_mode_provided,
            user_id=str(getattr(request, "user_id", "") or "").strip(),
        )
        if session_id:
            # Metadata sync may initialize a legacy record. Re-read it so the
            # first upgraded turn observes the atomically locked values.
            from jiuwenswarm.server.runtime.session.session_metadata import (
                get_session_metadata,
            )

            session_metadata = get_session_metadata(
                session_id,
                enable_writeback=False,
            )
    else:
        project_dir = requested_project_dir
        if not (isinstance(project_dir, str) and project_dir.strip()) and session_id:
            from jiuwenswarm.server.runtime.session.session_metadata import (
                get_session_metadata,
            )

            session_metadata = get_session_metadata(
                session_id,
                cache_bust=True,
                enable_writeback=False,
            )
            locked = (
                session_metadata.get("project_dir")
                if isinstance(session_metadata, dict)
                else None
            )
            if isinstance(locked, str) and locked.strip():
                project_dir = locked.strip()

    if isinstance(project_dir, str) and project_dir.strip():
        project_dir = project_dir.strip()
        request.params["project_dir"] = project_dir
        request.metadata = dict(request.metadata or {})
        request.metadata["project_dir"] = project_dir

    # Persist Session is a session-creation identity. Per-turn request values
    # are advisory only; expose the locked value through the legacy internal
    # adapter key without allowing a chat request to mutate it.
    effective_persist_session = (
        session_metadata.get("persist_session") is True
        if isinstance(session_metadata, dict)
        else False
    )
    requested_persist_session = params.pop("persist_session", None)
    requested_legacy_eternal = params.get("eternal_conversation_enabled")
    if (
        isinstance(requested_persist_session, bool)
        and requested_persist_session != effective_persist_session
    ):
        logger.warning(
            "Session %s has locked persist_session=%s; ignoring chat value %s",
            session_id,
            effective_persist_session,
            requested_persist_session,
        )
    if (
        isinstance(requested_legacy_eternal, bool)
        and requested_legacy_eternal != effective_persist_session
    ):
        logger.warning(
            "Session %s has locked Persist Session=%s; ignoring legacy chat value %s",
            session_id,
            effective_persist_session,
            requested_legacy_eternal,
        )
    params["eternal_conversation_enabled"] = effective_persist_session

    await agent_manager.wait_for_session_prewarm(request.session_id)
    agent = await agent_manager.get_agent(
        channel_id=channel_id,
        mode=agent_mode,
        project_dir=project_dir,
        sub_mode=sub_mode,
    )
    if agent is None:
        raise ValueError("Failed to get agent")
    return mode, sub_mode, agent


async def cancel_request(
    agent_manager: AgentManager,
    request: AgentRequest,
    *,
    allow_create: bool = False,
) -> AgentResponse:
    """Cancel one request/session through its existing Runtime agent."""
    channel_id = request.channel_id or "default"
    session_lookup = getattr(agent_manager, "get_agent_for_session_nowait", None)
    agent = (
        session_lookup(channel_id, request.session_id or "")
        if callable(session_lookup) and request.session_id
        else None
    )
    project_dir = _resolve_cancel_project_dir(request) if agent is None else None
    params = request.params if isinstance(request.params, dict) else {}
    mode_param = params.get("mode", "")
    if agent is None and mode_param:
        mode, sub_mode, _canonical = resolve_agent_request_mode(
            mode_param,
            work_mode=params.get("work_mode"),
        )
        agent_mode = "agent" if mode == "auto_harness" else mode
        agent = agent_manager.get_agent_nowait(
            channel_id,
            mode=agent_mode,
            project_dir=project_dir,
            sub_mode=sub_mode,
        )
    elif agent is None:
        agent = None

    if agent is None:
        agent = agent_manager.get_agent_nowait(
            channel_id,
            project_dir=project_dir,
        )

    if agent is None and not allow_create:
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={
                "event_type": "chat.interrupt_result",
                "success": True,
                "message": "当前会话任务已终止",
            },
        )

    if agent is None:
        mode, sub_mode = apply_resolved_mode_to_request(request)
        agent_mode = "agent" if mode == "auto_harness" else mode
        agent = await agent_manager.get_agent(
            channel_id=channel_id,
            mode=agent_mode,
            project_dir=project_dir,
            sub_mode=sub_mode,
        )
    if agent is None:
        raise ValueError("Failed to get agent for cancel request")
    return await agent.process_message(request)


__all__ = [
    "CHAT_TURN_METHODS",
    "PREVIOUS_SESSION_MODE_KEY",
    "apply_resolved_mode_to_request",
    "cancel_request",
    "prepare_chat_turn",
    "resolve_agent_request_mode",
    "resolve_request_project_dir",
    "resolve_request_runtime_mode",
    "sync_chat_request_metadata",
]
