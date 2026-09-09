# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""团队域 handler"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any

from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.common.utils import get_agent_sessions_dir
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.handlers._shared import (
    _effective_config_for_request,
    _is_team_metadata_mode,
    _session_team_binding_lock,
    _sessions_dir_for_request,
    resolve_agent_request_mode,
)
from jiuwenswarm.server.runtime.session.session_history import (
    read_member_history_records,
    read_team_history_records,
)
from jiuwenswarm.server.runtime.session.session_metadata import remove_session_metadata_cache
from jiuwenswarm.server.utils.utils import is_team_params
from jiuwenswarm.server.wire_truncate import (
    _TEAM_HISTORY_DEFAULT_LIMIT,
    _TEAM_HISTORY_DEFAULT_MAX_BYTES,
    _TEAM_HISTORY_MAX_LIMIT,
    _TEAM_HISTORY_MAX_MAX_BYTES,
    _TEAM_HISTORY_MIN_MAX_BYTES,
    _coerce_int,
    _sanitize_history_record_for_wire,
    _select_history_record_page,
)

logger = logging.getLogger(__name__)


def _team_binding_payload(binding: Any) -> dict[str, Any]:
    if hasattr(binding, "to_dict"):
        return binding.to_dict()
    if isinstance(binding, dict):
        return dict(binding)
    return {}


def _create_team_binding_from_template(
    *,
    team_name: str,
    template_id: str,
    config_base: dict[str, Any],
) -> Any:
    from jiuwenswarm.agents.harness.team import (
        get_team_template_snapshot,
        list_team_template_summaries,
    )
    from jiuwenswarm.server.runtime.team_binding_store import (
        TeamBindingStoreError,
        get_team_binding_store,
        validate_team_name,
    )
    from jiuwenswarm.server.runtime.team_entity_store import ensure_team_entity, get_team_entity_store

    normalized_name = validate_team_name(team_name)
    template_ids = {
        str(item.get("template_id") or "")
        for item in list_team_template_summaries(config_base)
    }
    if template_id not in template_ids:
        raise TeamBindingStoreError("template_id not found", code="NOT_FOUND")

    entity_store = get_team_entity_store()
    if entity_store.exists(normalized_name):
        raise TeamBindingStoreError("team_name already exists", code="CONFLICT")
    template_snapshot = get_team_template_snapshot(config_base, template_id=template_id)
    binding_store = get_team_binding_store()
    binding = binding_store.create(team_name=normalized_name, template_id=template_id)
    try:
        entity = ensure_team_entity(
            team_name=binding.team_name,
            template_id=binding.template_id,
            template_snapshot=template_snapshot,
            config_base=config_base,
            created_at=binding.created_at,
            store=entity_store,
        )
        if entity is None:
            raise TeamBindingStoreError("team entity config missing", code="NOT_FOUND")
    except Exception:
        binding_store.delete(binding.team_name)
        raise
    return binding


async def _create_generated_team_binding(
    *,
    description: str,
    config_base: dict[str, Any],
) -> tuple[Any, dict[str, Any]]:
    """Generate a unique team name and persist its binding and entity."""
    from jiuwenswarm.agents.harness.team import (
        generate_team_name,
        list_team_template_summaries,
    )
    from jiuwenswarm.server.runtime.team_binding_store import (
        TeamBindingStoreError,
    )

    normalized_description = str(description or "").strip()
    if not normalized_description:
        raise TeamBindingStoreError("description is required", code="BAD_REQUEST")

    templates = list_team_template_summaries(config_base)
    if not templates:
        raise TeamBindingStoreError("no team template configured", code="NOT_FOUND")

    default_template = templates[0]
    template_id = str(default_template.get("template_id") or "").strip()
    generated_name = await generate_team_name(
        normalized_description,
        config_base=config_base,
        template_id=template_id,
    )

    for candidate_index in range(100):
        suffix = "" if candidate_index == 0 else f"_{candidate_index + 1}"
        candidate = f"{generated_name[:64 - len(suffix)]}{suffix}"
        try:
            binding = _create_team_binding_from_template(
                team_name=candidate,
                template_id=template_id,
                config_base=config_base,
            )
            return binding, default_template
        except TeamBindingStoreError as exc:
            if exc.code != "CONFLICT":
                raise

    raise TeamBindingStoreError(
        "unable to allocate a unique team_name",
        code="CONFLICT",
    )


async def _find_team_session_ids(
    team_name: str,
    *,
    sessions_root: str | Path | None = None,
) -> list[str]:
    from jiuwenswarm.server.runtime.session.session_metadata import (
        get_session_metadata,
        resolve_session_subdir,
    )

    sessions_dir = Path(sessions_root) if sessions_root is not None else get_agent_sessions_dir()
    if not sessions_dir.exists():
        return []

    matched_session_ids: list[str] = []
    for session_dir in sessions_dir.iterdir():
        if not session_dir.is_dir():
            continue

        session_id = session_dir.name
        if resolve_session_subdir(session_id, sessions_root=sessions_dir) is None:
            continue
        if sessions_root is None:
            metadata = get_session_metadata(session_id)
        else:
            metadata = get_session_metadata(
                session_id,
                sessions_root=sessions_dir,
            )
        if not _is_team_metadata_mode(metadata):
            continue

        metadata_team_name = str(metadata.get("team_name") or "").strip()
        if metadata_team_name == team_name:
            matched_session_ids.append(session_id)

    return sorted(set(matched_session_ids))


def _active_team_session_map(
    *,
    sessions_root: str | Path | None = None,
) -> dict[str, str]:
    from jiuwenswarm.agents.harness.team import get_all_team_managers
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

    active: dict[str, str] = {}
    for manager in get_all_team_managers():
        snapshot_fn = getattr(manager, "get_runtime_team_snapshot", None)
        if not callable(snapshot_fn):
            continue
        for session_id, info in snapshot_fn().items():
            runtime_team_name = str(info.get("team_name") or "").strip()
            state = str(info.get("state") or "").strip()
            metadata = get_session_metadata(
                str(session_id),
                sessions_root=sessions_root,
            )
            logical_team_name = str(metadata.get("team_name") or "").strip()
            team_name = logical_team_name or runtime_team_name
            if team_name and state in {"active", "pending"}:
                active.setdefault(team_name, str(session_id))
    return active


def _legacy_team_bindings_from_sessions(known_team_names: set[str]) -> list[dict[str, Any]]:
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

    sessions_dir = get_agent_sessions_dir()
    if not sessions_dir.exists():
        return []

    legacy: dict[str, dict[str, Any]] = {}
    for session_dir in sessions_dir.iterdir():
        if not session_dir.is_dir():
            continue
        metadata = get_session_metadata(session_dir.name, cache_bust=True)
        if not metadata or not _is_team_metadata_mode(metadata):
            continue
        team_name = str(metadata.get("team_name") or "").strip()
        if not team_name or team_name in known_team_names:
            continue
        item = legacy.setdefault(
            team_name,
            {
                "team_name": team_name,
                "template_id": str(metadata.get("team_template_id") or team_name).strip() or team_name,
                "created_at": float(metadata.get("created_at") or session_dir.stat().st_ctime),
                "updated_at": float(metadata.get("last_message_at") or session_dir.stat().st_mtime),
                "session_ids": [],
                "last_session_id": "",
                "legacy": True,
            },
        )
        if session_dir.name not in item["session_ids"]:
            item["session_ids"].append(session_dir.name)
        if float(metadata.get("last_message_at") or 0) >= float(item.get("updated_at") or 0):
            item["updated_at"] = float(metadata.get("last_message_at") or session_dir.stat().st_mtime)
            item["last_session_id"] = session_dir.name
    return list(legacy.values())


async def handle_team_templates_list(ctx: RequestContext) -> None:
    request = ctx.request
    from jiuwenswarm.agents.harness.team import list_team_template_summaries

    templates = list_team_template_summaries(
        _effective_config_for_request(request)
    )
    resp = AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=True,
        payload={"templates": templates},
        metadata=request.metadata,
    )
    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)




async def handle_team_bindings_list(ctx: RequestContext) -> None:
    request = ctx.request
    from jiuwenswarm.agents.harness.team import list_team_template_summaries
    from jiuwenswarm.server.runtime.team_entity_store import ensure_team_entity_for_binding, get_team_entity_store
    from jiuwenswarm.server.runtime.team_binding_store import get_team_binding_store

    active_by_team = _active_team_session_map(
        sessions_root=_sessions_dir_for_request(request),
    )
    config_base = _effective_config_for_request(request)
    templates = {
        str(item.get("template_id") or ""): item
        for item in list_team_template_summaries(config_base)
    }
    store = get_team_binding_store()
    bindings = store.list()
    bindings_by_name = {binding.team_name: binding for binding in bindings}
    entity_store = get_team_entity_store()
    teams = [_team_binding_payload(binding) for binding in bindings]
    known_team_names = {str(item.get("team_name") or "") for item in teams}
    teams.extend(_legacy_team_bindings_from_sessions(known_team_names))

    enriched: list[dict[str, Any]] = []
    for item in teams:
        team_name = str(item.get("team_name") or "").strip()
        template_id = str(item.get("template_id") or "").strip()
        active_session_id = active_by_team.get(team_name, "")
        legacy = bool(item.get("legacy", False))
        entity = None
        entity_path = ""
        if team_name and not legacy:
            binding = bindings_by_name.get(team_name)
            if binding is not None:
                entity = ensure_team_entity_for_binding(binding, config_base=config_base, store=entity_store)
            else:
                entity = entity_store.get(team_name)
            if entity is not None:
                item["template_id"] = entity.template_id
                template_id = entity.template_id
                entity_path = str(entity_store.entity_path(team_name))
        source_template_available = bool(template_id and template_id in templates)
        team_config_available = entity is not None
        template_available = bool(team_config_available or source_template_available)
        selectable = bool(team_name and not active_session_id and not legacy and team_config_available)
        disabled_reason = ""
        if active_session_id:
            disabled_reason = "active"
        elif legacy:
            disabled_reason = "legacy"
        elif not team_config_available:
            disabled_reason = "team_config_missing"
        item.update(
            {
                "template_available": template_available,
                "source_template_available": source_template_available,
                "team_config_available": team_config_available,
                "team_config_path": entity_path,
                "session_count": len(item.get("session_ids") or []),
                "active_session_id": active_session_id,
                "selectable": selectable,
                "disabled_reason": disabled_reason,
            }
        )
        enriched.append(item)

    enriched.sort(key=lambda row: float(row.get("updated_at") or 0), reverse=True)
    resp = AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=True,
        payload={"teams": enriched},
        metadata=request.metadata,
    )
    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)




async def handle_team_binding_create(ctx: RequestContext) -> None:
    request = ctx.request
    from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStoreError
    from jiuwenswarm.server.runtime.team_entity_store import TeamEntityStoreError

    params = request.params if isinstance(request.params, dict) else {}
    team_name = str(params.get("team_name") or "")
    template_id = str(params.get("template_id") or "").strip()
    config_base = _effective_config_for_request(request)
    try:
        binding = _create_team_binding_from_template(
            team_name=team_name,
            template_id=template_id,
            config_base=config_base,
        )
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"team": binding.to_dict()},
            metadata=request.metadata,
        )
    except (TeamBindingStoreError, TeamEntityStoreError) as exc:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(exc), "code": getattr(exc, "code", "BAD_REQUEST")},
            metadata=request.metadata,
        )

    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_team_binding_generate(ctx: RequestContext) -> None:
    request = ctx.request
    from jiuwenswarm.agents.harness.team import (
        TeamNameGenerationError,
    )
    from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStoreError
    from jiuwenswarm.server.runtime.team_entity_store import TeamEntityStoreError

    params = request.params if isinstance(request.params, dict) else {}
    description = str(params.get("description") or params.get("prompt") or "").strip()
    config_base = _effective_config_for_request(request)
    try:
        binding, default_template = await _create_generated_team_binding(
            description=description,
            config_base=config_base,
        )

        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={
                "team": binding.to_dict(),
                "template": default_template,
            },
            metadata=request.metadata,
        )
    except TeamNameGenerationError as exc:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(exc), "code": "GENERATION_FAILED"},
            metadata=request.metadata,
        )
    except (TeamBindingStoreError, TeamEntityStoreError) as exc:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(exc), "code": getattr(exc, "code", "BAD_REQUEST")},
            metadata=request.metadata,
        )

    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_team_session_bind(ctx: RequestContext) -> None:
    request = ctx.request
    from jiuwenswarm.agents.harness.team import TeamManager, get_team_template_snapshot
    from jiuwenswarm.agents.harness.team.config_loader import TeamTemplateNotFoundError
    from jiuwenswarm.server.runtime.session.session_metadata import (
        capture_session_team_binding_artifacts,
        resolve_session_subdir,
        restore_session_team_binding_artifacts,
        update_session_metadata,
    )
    from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStoreError, get_team_binding_store
    from jiuwenswarm.server.runtime.team_entity_store import (
        TeamEntityStoreError,
        ensure_team_entity_for_binding,
        get_team_entity_store,
    )

    params = request.params if isinstance(request.params, dict) else {}
    session_id = str(params.get("session_id") or request.session_id or "").strip()
    team_name = str(params.get("team_name") or "").strip()
    _, _, canonical_mode = resolve_agent_request_mode(params.get("mode", "team"))
    try:
        if not session_id:
            raise TeamBindingStoreError("session_id is required", code="BAD_REQUEST")
        sessions_root = _sessions_dir_for_request(request)
        session_dir = resolve_session_subdir(
            session_id,
            sessions_root=sessions_root,
        )
        if session_dir is None:
            raise TeamBindingStoreError("invalid session_id", code="BAD_REQUEST")
        has_tenant_scope = bool(
            str(request.service_id or "").strip()
            or str(request.agent_id or "").strip()
            or request.channel_id == "officeclaw"
        )
        legacy_session_exists = False
        if not has_tenant_scope and not session_dir.is_dir():
            legacy_session_dir = resolve_session_subdir(
                session_id,
                sessions_root=get_agent_sessions_dir(),
            )
            legacy_session_exists = bool(
                legacy_session_dir is not None and legacy_session_dir.is_dir()
            )
        if not session_dir.is_dir() and not legacy_session_exists:
            raise TeamBindingStoreError("session not found", code="NOT_FOUND")
        async with _session_team_binding_lock(session_id):
            binding_store = get_team_binding_store()
            existing_binding = binding_store.get(team_name)
            if existing_binding is None:
                raise TeamBindingStoreError("team binding not found", code="NOT_FOUND")
            config_base = _effective_config_for_request(request)
            entity = ensure_team_entity_for_binding(
                existing_binding,
                config_base=config_base,
            )
            if entity is None:
                raise TeamBindingStoreError("team entity config missing", code="NOT_FOUND")
            from jiuwenswarm.server.runtime.team_entity_store import normalize_team_entity_snapshot

            try:
                raw_snapshot = get_team_template_snapshot(
                    config_base,
                    template_id=existing_binding.template_id,
                )
            except TeamTemplateNotFoundError:
                raw_snapshot = entity.template_snapshot
            session_snapshot = normalize_team_entity_snapshot(raw_snapshot, config_base)
            artifacts = capture_session_team_binding_artifacts(
                session_id,
                sessions_root=sessions_root,
            )
            try:
                update_session_metadata(
                    session_id=session_id,
                    channel_id=str(request.channel_id or "").strip() or None,
                    mode=canonical_mode,
                    team_name=existing_binding.team_name,
                    runtime_team_name=TeamManager.build_session_scoped_team_name(
                        existing_binding.team_name,
                        session_id,
                    ),
                    team_template_id=existing_binding.template_id,
                    team_template_snapshot=session_snapshot,
                    sync=True,
                    sessions_root=sessions_root,
                )
                binding = binding_store.bind_session(
                    team_name=team_name,
                    session_id=session_id,
                )
            except Exception as exc:
                try:
                    restore_session_team_binding_artifacts(
                        session_id,
                        artifacts,
                        sessions_root=sessions_root,
                    )
                except Exception:
                    logger.exception(
                        "[AgentWebSocketServer] failed to roll back Team session bind: "
                        "session_id=%s team_name=%s",
                        session_id,
                        team_name,
                    )
                raise TeamBindingStoreError(
                    "team session bind failed",
                    code="INTERNAL_ERROR",
                ) from exc
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={
                "session_id": session_id,
                "team_name": binding.team_name,
                "team_template_id": binding.template_id,
                "mode": canonical_mode,
                "team": binding.to_dict(),
                "team_config_path": str(get_team_entity_store().entity_path(binding.team_name)),
            },
            metadata=request.metadata,
        )
    except (TeamBindingStoreError, TeamEntityStoreError, OSError, ValueError) as exc:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(exc), "code": getattr(exc, "code", "BAD_REQUEST")},
            metadata=request.metadata,
        )

    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_team_delete(ctx: RequestContext) -> None:
    """Delete a team and all team sessions that persist that team."""
    request = ctx.request
    from openjiuwen.core.runner import Runner
    from jiuwenswarm.agents.harness.team import (
        stop_team_session_runtime_across_managers,
    )
    from jiuwenswarm.server.runtime.team_binding_store import (
        TeamBindingStoreError,
        get_team_binding_store,
    )
    from jiuwenswarm.server.runtime.team_entity_store import (
        TeamEntityStoreError,
        get_team_entity_store,
    )
    from jiuwenswarm.server.runtime.session.session_metadata import (
        get_session_metadata,
        resolve_session_runtime_team_name,
        resolve_session_subdir,
    )

    params = request.params if isinstance(request.params, dict) else {}
    is_team = is_team_params(params)
    team_name = str(params.get("team_name") or "").strip()

    if not team_name:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": "team_name is required", "code": "BAD_REQUEST"},
            metadata=request.metadata,
        )
    elif not is_team:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={
                "error": "team.delete is only supported for team mode",
                "code": "UNSUPPORTED_MODE",
            },
            metadata=request.metadata,
        )
    else:
        try:
            binding_store = get_team_binding_store()
            entity_store = get_team_entity_store()
            sessions_root = _sessions_dir_for_request(request)
            team_session_ids = await _find_team_session_ids(
                team_name,
                sessions_root=sessions_root,
            )
            if not team_session_ids:
                binding = binding_store.get(team_name)
                if binding is None and not entity_store.exists(team_name):
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=False,
                        payload={"error": "team not found", "code": "NOT_FOUND"},
                        metadata=request.metadata,
                    )
                else:
                    entity_store.delete_team_directory(team_name)
                    binding_store.delete(team_name)
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=True,
                        payload={
                            "team_name": team_name,
                            "session_ids": [],
                            "deleted": True,
                        },
                        metadata=request.metadata,
                    )
            else:
                checkpoint_resp = await ctx.services.ensure_persistent_checkpointer_response(request)
                if checkpoint_resp is not None:
                    resp = checkpoint_resp
                else:
                    from jiuwenswarm.agents.harness.team import (
                        kv_cache_hooks as team_kv_cache_hooks,
                    )

                    for team_session_id in team_session_ids:
                        await team_kv_cache_hooks.stop_runtime_before_terminal_delete(
                            stop_team_session_runtime_across_managers,
                            session_id=team_session_id,
                            reason="team.delete: ",
                        )

                    runtime_sessions: dict[str, list[str]] = {}
                    for team_session_id in team_session_ids:
                        metadata = get_session_metadata(
                            team_session_id,
                            sessions_root=sessions_root,
                        )
                        runtime_team_name = (
                            resolve_session_runtime_team_name(metadata) or team_name
                        )
                        runtime_sessions.setdefault(runtime_team_name, []).append(
                            team_session_id
                        )

                    runtime_deleted = True
                    for runtime_team_name, runtime_session_ids in runtime_sessions.items():
                        try:
                            deleted = await Runner.delete_agent_team(
                                team_name=runtime_team_name,
                                session_ids=runtime_session_ids,
                                force=True,
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "[AgentWebSocketServer] team.delete runtime cleanup failed: "
                                "team_name=%s session_ids=%s error=%s",
                                runtime_team_name,
                                runtime_session_ids,
                                exc,
                            )
                            runtime_deleted = False
                            continue
                        if not deleted:
                            runtime_deleted = False
                    if not runtime_deleted:
                        resp = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=False,
                            payload={
                                "error": "agent team runtime cleanup failed",
                                "code": "DELETE_FAILED",
                                "team_name": team_name,
                                "deleted": False,
                            },
                            metadata=request.metadata,
                        )
                    else:
                        failed_session_ids: list[str] = []
                        for team_session_id in team_session_ids:
                            session_dir = resolve_session_subdir(
                                team_session_id,
                                sessions_root=sessions_root,
                            )
                            if session_dir is None:
                                logger.error(
                                    "[AgentWebSocketServer] refusing to delete unsafe team session path: "
                                    "session_id=%s",
                                    team_session_id,
                                )
                                failed_session_ids.append(team_session_id)
                                continue
                            if session_dir.exists():
                                try:
                                    shutil.rmtree(session_dir)
                                except Exception as exc:
                                    logger.warning(
                                        "[AgentWebSocketServer] failed to delete local team session dir: "
                                        "session_id=%s error=%s",
                                        team_session_id,
                                        exc,
                                    )
                                    failed_session_ids.append(team_session_id)
                                    continue
                            remove_session_metadata_cache(
                                team_session_id,
                                sessions_root=sessions_root,
                            )

                        if failed_session_ids:
                            resp = AgentResponse(
                                request_id=request.request_id,
                                channel_id=request.channel_id,
                                ok=False,
                                payload={
                                    "error": "failed to delete local team session directories",
                                    "code": "DELETE_FAILED",
                                    "team_name": team_name,
                                    "failed_session_ids": failed_session_ids,
                                    "deleted": False,
                                },
                                metadata=request.metadata,
                            )
                        else:
                            # agent-core normally removes team_home; retry here because it logs and
                            # suppresses filesystem cleanup failures.
                            entity_store.delete_team_directory(team_name)
                            binding_store.delete(team_name)
                            resp = AgentResponse(
                                request_id=request.request_id,
                                channel_id=request.channel_id,
                                ok=True,
                                payload={
                                    "team_name": team_name,
                                    "session_ids": team_session_ids,
                                    "deleted": True,
                                },
                                metadata=request.metadata,
                            )
        except (TeamBindingStoreError, TeamEntityStoreError) as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": getattr(exc, "code", "DELETE_FAILED")},
                metadata=request.metadata,
            )

    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_team_session_reset(ctx: RequestContext) -> None:
    """Reset a single team session: drop its task board + release its
    checkpoint, KEEP team_info / roster / team_home / session binding.

    Session-scoped counterpart to ``handle_team_delete``: explicitly
    keyed by a single ``session_id`` (no cross-thread scan), skips the
    ``team_info`` delete, ``team_home`` rmtree, ``session_dir`` rmtree
    and binding/entity teardown that ``handle_team_delete`` performs.
    Next chat.send on the same session_id -> ``NEW_TEAM_IN_SESSION``
    (fresh empty board, members re-dispatched from the preserved roster).
    """
    request = ctx.request
    from openjiuwen.core.runner import Runner
    from jiuwenswarm.server.runtime.session.session_metadata import (
        get_session_metadata,
        resolve_session_runtime_team_name,
    )
    from jiuwenswarm.agents.harness.team import get_team_manager

    params = request.params if isinstance(request.params, dict) else {}
    team_name = str(params.get("team_name") or "").strip()
    session_id = str(params.get("session_id") or "").strip()

    if not team_name or not session_id:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={
                "error": "team.session.reset requires team_name + session_id",
                "code": "BAD_REQUEST",
            },
            metadata=request.metadata,
        )
    else:
        sessions_root = _sessions_dir_for_request(request)
        metadata = get_session_metadata(
            session_id,
            sessions_root=sessions_root,
        )
        runtime_team_name = (
            resolve_session_runtime_team_name(metadata) or team_name
        )
        team_manager = get_team_manager(
            str(metadata.get("channel_id") or request.channel_id or "")
        )
        # 1) Clear shell-side runtime tracking + first-request flag first.
        #    stop_runner=False: the Runner-owned runtime is stopped below by
        #    reset_session itself, mirroring delete_team's force-stop.
        try:
            await team_manager.stop_session_runtime(
                session_id,
                reason="team.session.reset",
                stop_runner=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AgentWebSocketServer] stop_session_runtime (reset) failed: session_id=%s error=%s",
                session_id,
                exc,
            )
        team_manager.clear_session_initialized(session_id)
        # 2) openjiuwen session-scoped reset: drop session tables + release
        #    checkpoint + remove session worktrees, keep team_info/roster/binding.
        try:
            ok = await Runner.reset_agent_team_session(
                team_name=runtime_team_name,
                session_id=session_id,
                force=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AgentWebSocketServer] team.session.reset failed: team=%s session=%s error=%s",
                runtime_team_name,
                session_id,
                exc,
            )
            ok = False
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=bool(ok),
            payload={
                "reset": bool(ok),
                "session_id": session_id,
                "team_name": team_name,
            },
            metadata=request.metadata,
        )

    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_team_runtime_dissolve(ctx: RequestContext) -> None:
    """Dissolve one session's team runtime after a template/config change.

    Stops the in-memory runtime + clears the initialized flag + resets the
    session task board (COLD_RECOVER: memory/history kept), so the next
    chat.send rebuilds from the reconciled template snapshot.
    """
    request = ctx.request
    from openjiuwen.core.runner import Runner
    from jiuwenswarm.server.runtime.session.session_metadata import (
        get_session_metadata,
        resolve_session_runtime_team_name,
    )
    from jiuwenswarm.agents.harness.team import get_team_manager

    params = request.params if isinstance(request.params, dict) else {}
    team_name = str(params.get("team_name") or "").strip()
    session_id = str(params.get("session_id") or "").strip()

    if not session_id:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={
                "error": "team.runtime.dissolve requires session_id",
                "code": "BAD_REQUEST",
            },
            metadata=request.metadata,
        )
    else:
        sessions_root = _sessions_dir_for_request(request)
        metadata = get_session_metadata(session_id, cache_bust=True, sessions_root=sessions_root)
        runtime_team_name = resolve_session_runtime_team_name(metadata) or team_name
        if not runtime_team_name:
            # 会话未绑定运行时团队且请求未带 team_name：无可 dissolve 的
            # 运行时。ok=True 与 relay resetTeamSession 接受 NOT_FOUND 的
            # 语义对齐，避免 relay 侧 teamConfigDirty 标记被永久卡住。
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "dissolved": True,
                    "session_id": session_id,
                    "team_name": "",
                },
                metadata=request.metadata,
            )
        else:
            team_manager = get_team_manager(
                str(metadata.get("channel_id") or request.channel_id or "")
            )
            # 1) 停 shell 侧运行时追踪 + 清 initialized 标记（stop_runner=False：
            #    Runner 侧运行时由下方 reset_agent_team_session 统一处理）。
            try:
                await team_manager.stop_session_runtime(
                    session_id,
                    reason="team.runtime.dissolve",
                    stop_runner=False,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[AgentWebSocketServer] stop_session_runtime (dissolve) "
                    "failed: session_id=%s error=%s",
                    session_id,
                    exc,
                )
            try:
                team_manager.clear_session_initialized(session_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[AgentWebSocketServer] clear_session_initialized (dissolve) "
                    "failed: session_id=%s error=%s",
                    session_id,
                    exc,
                )
            # 1.5) 解析 keep-set：现跑 reconcile 对齐模板后取 leader + predefined
            #      成员名（时序陷阱：dissolve 先于下一轮 chat.send，此刻冻结快照
            #      仍含被删成员，故不读冻结原样，必经 reconcile）。None = 模板不可
            #      得 → 跳过 prune（fail-open）。
            from jiuwenswarm.server.runtime.team_snapshot_refresh import (
                resolve_dissolve_keep_members,
            )
            keep_members = resolve_dissolve_keep_members(
                session_id=session_id,
                team_name=str(metadata.get("team_name") or "").strip(),
                template_id=str(metadata.get("team_template_id") or "").strip(),
                config_base=_effective_config_for_request(request),
                sessions_root=sessions_root,
                metadata=metadata,
            )
            # 2) 丢任务板行 + 清 pending_resume + 保 checkpoint bucket -> COLD_RECOVER。
            try:
                ok = await Runner.reset_agent_team_session(
                    team_name=runtime_team_name,
                    session_id=session_id,
                    force=True,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[AgentWebSocketServer] team.runtime.dissolve failed: "
                    "team=%s session=%s error=%s",
                    runtime_team_name,
                    session_id,
                    exc,
                )
                ok = False
            # 3) prune roster：reset 保 roster（COLD_RECOVER 需要），但被删成员的行
            #    会令下一轮 COLD_RECOVER 复活之。reset 成功且 keep-set 已知时删掉
            #    它们；prune 失败置 ok=false 让 relay 退避重试（幂等）。agent-core
            #    太老（无此方法）→ 跳过且 warning，ok 不变。
            if ok and keep_members is not None:
                if not hasattr(Runner, "prune_agent_team_roster"):
                    logger.warning(
                        "[AgentWebSocketServer] agent-core lacks "
                        "prune_agent_team_roster; dissolve skips roster prune "
                        "team=%s session=%s",
                        runtime_team_name,
                        session_id,
                    )
                else:
                    try:
                        pruned = await Runner.prune_agent_team_roster(
                            team_name=runtime_team_name,
                            session_id=session_id,
                            keep_members=keep_members,
                        )
                        if pruned:
                            logger.info(
                                "[AgentWebSocketServer] dissolve pruned roster "
                                "team=%s session=%s members=%s",
                                runtime_team_name,
                                session_id,
                                pruned,
                            )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "[AgentWebSocketServer] dissolve prune failed "
                            "(retryable): team=%s session=%s error=%s",
                            runtime_team_name,
                            session_id,
                            exc,
                        )
                        ok = False
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=bool(ok),
                payload={
                    "dissolved": bool(ok),
                    "session_id": session_id,
                    # 回显解析后的权威运行时名（relay 可不传 team_name）。
                    "team_name": runtime_team_name,
                },
                metadata=request.metadata,
            )

    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_team_snapshot(ctx: RequestContext) -> None:
    request = ctx.request
    from jiuwenswarm.agents.harness.team import get_team_manager
    from jiuwenswarm.agents.harness.team.handlers.team_monitor_handler import (
        TeamMonitorHandler,
    )
    from jiuwenswarm.server.runtime.session.session_metadata import (
        get_session_metadata,
        resolve_session_runtime_team_name,
    )

    params = request.params if isinstance(request.params, dict) else {}
    session_id = str(params.get("session_id") or request.session_id or "").strip()
    channel_id = request.channel_id or "web"
    empty_payload = {"members": [], "tasks": [], "team_id": None}

    team_manager = get_team_manager(channel_id)
    monitor_handler = team_manager.get_monitor_handler(session_id) if session_id else None

    snapshot: dict[str, Any] | None = None
    source = "empty"
    if monitor_handler is not None and monitor_handler.is_running:
        try:
            snapshot = await monitor_handler.get_team_snapshot()
            if snapshot is not None:
                source = "live"
        except Exception as e:
            logger.warning("[AgentWebSocketServer] team.snapshot (live) failed: %s", e)

    def _snapshot_tasks(payload: dict[str, Any] | None) -> list[Any]:
        if not isinstance(payload, dict):
            return []
        tasks = payload.get("tasks")
        return tasks if isinstance(tasks, list) else []

    # History restore often hits this RPC after the monitor has stopped, OR
    # while a live handler is still registered but already returns a truthy
    # empty board ({tasks: [], members: [], team_id: ...}). `if not snapshot`
    # alone would skip DB in that case and leave the frontend with no
    # title/content. Fall back whenever live has no tasks.
    needs_db = snapshot is None or not _snapshot_tasks(snapshot)
    if needs_db and session_id:
        metadata = get_session_metadata(
            session_id,
            sessions_root=_sessions_dir_for_request(request),
        )
        team_name = str(
            team_manager.get_active_team_name(session_id)
            or resolve_session_runtime_team_name(metadata)
            or params.get("team_name")
            or ""
        ).strip()
        if team_name:
            try:
                db_snapshot = await TeamMonitorHandler.get_team_snapshot_from_db(
                    session_id, team_name
                )
            except Exception as e:
                logger.warning(
                    "[AgentWebSocketServer] team.snapshot (db) failed: "
                    "session_id=%s team_name=%s error=%s",
                    session_id,
                    team_name,
                    e,
                )
                db_snapshot = None
            # Prefer DB when it has tasks, or when live was missing entirely.
            # If both boards have empty tasks, keep live so in-memory
            # members (if any) are not wiped by an empty DB read.
            if db_snapshot is not None and (
                snapshot is None or _snapshot_tasks(db_snapshot)
            ):
                snapshot = db_snapshot
                source = "db"

    payload = snapshot or empty_payload
    members = payload.get("members") if isinstance(payload, dict) else []
    tasks = _snapshot_tasks(payload if isinstance(payload, dict) else None)
    logger.info(
        "[AgentWebSocketServer] team.snapshot session_id=%s source=%s "
        "tasks_count=%s members_count=%s",
        session_id or "-",
        source if snapshot is not None else "empty",
        len(tasks),
        len(members) if isinstance(members, list) else 0,
    )

    resp = AgentResponse(
        request_id=request.request_id,
        channel_id=channel_id,
        ok=True,
        payload=payload,
    )
    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_team_tasks_dependencies(ctx: RequestContext) -> None:
    """team.tasks.dependencies — 取专家团任务依赖边（relay-claw finalize best-effort 调用）。

    镜像 ``handle_team_snapshot`` 的装配：单参 ctx、响应经 ``ctx.sink.send_wire``；
    team_name 三段式解析（``get_active_team_name`` OR
    ``resolve_session_runtime_team_name(metadata)`` OR ``params.team_name``）。
    monitor 不在时由 ``TeamMonitorHandler.get_team_dependencies_from_db`` 直查 per-session
    动态依赖表。best-effort：任何失败 → 空 edges 列表，不抛（relay-claw 侧 edges 缺省）。
    """
    request = ctx.request
    from jiuwenswarm.agents.harness.team import get_team_manager
    from jiuwenswarm.agents.harness.team.handlers.team_monitor_handler import TeamMonitorHandler
    from jiuwenswarm.server.runtime.session.session_metadata import (
        get_session_metadata,
        resolve_session_runtime_team_name,
    )

    params = request.params if isinstance(request.params, dict) else {}
    session_id = str(params.get("session_id") or request.session_id or "").strip()
    channel_id = request.channel_id or "web"

    team_manager = get_team_manager(channel_id)
    metadata = (
        get_session_metadata(session_id, sessions_root=_sessions_dir_for_request(request))
        if session_id
        else None
    )
    team_name = str(
        (team_manager.get_active_team_name(session_id) if session_id else "")
        or resolve_session_runtime_team_name(metadata)
        or params.get("team_name")
        or ""
    ).strip()

    edges: list[dict[str, Any]] | None = None
    if session_id and team_name:
        try:
            edges = await TeamMonitorHandler.get_team_dependencies_from_db(session_id, team_name)
        except Exception as e:
            logger.warning(
                "[AgentWebSocketServer] team.tasks.dependencies failed: "
                "session_id=%s team_name=%s error=%s",
                session_id,
                team_name,
                e,
            )
            edges = None

    payload: dict[str, Any] = {"edges": edges or []}
    logger.info(
        "[AgentWebSocketServer] team.tasks.dependencies session_id=%s team_name=%s edges_count=%s",
        session_id or "-",
        team_name or "-",
        len(payload["edges"]),
    )
    resp = AgentResponse(
        request_id=request.request_id,
        channel_id=channel_id,
        ok=True,
        payload=payload,
    )
    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


def _snapshot_tasks(payload: dict[str, Any] | None) -> list[Any]:
    if not isinstance(payload, dict):
        return []
    tasks = payload.get("tasks")
    return tasks if isinstance(tasks, list) else []




async def handle_team_mq_publish(ctx: RequestContext) -> None:
    """Relay one external team event into the active core team runtime."""
    request = ctx.request
    from jiuwenswarm.agents.harness.team import get_team_manager

    session_id = request.session_id or ""
    channel_id = request.channel_id or "web"
    payload = request.params.get("payload")

    if not session_id:
        success, reason = False, "session_id is required"
    elif payload is None:
        success, reason = False, "payload is required"
    elif not isinstance(payload, dict) or payload.get("type") != "team.external_event":
        success, reason = False, "invalid_external_event"
    else:
        success, reason = await get_team_manager(channel_id).interact(session_id, payload)

    resp = AgentResponse(
        request_id=request.request_id,
        channel_id=channel_id,
        ok=success,
        payload={"published": True} if success else {"error": reason},
    )
    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_team_history_get(ctx: RequestContext) -> None:
    """返回 team 模式历史记录的分页，避免与 history.get 并发竞争。

    支持可选 member_name 参数：传入时仅返回与该 member 相关的记录
    （p2p 消息 / @all 广播 / teammate 输出）。
    """
    request = ctx.request
    params = request.params if isinstance(request.params, dict) else {}
    session_id = params.get("session_id")
    member_name = params.get("member_name")
    channel_id = request.channel_id or "web"

    if not isinstance(session_id, str) or not session_id.strip():
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=channel_id,
            ok=False,
            payload={"error": "session_id is required"},
        )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        await ctx.sink.send_wire(wire)
        return

    session_id = session_id.strip()
    try:
        if member_name and isinstance(member_name, str) and member_name.strip():
            records = await asyncio.to_thread(
                read_member_history_records, session_id, str(member_name).strip()
            )
        else:
            records = await asyncio.to_thread(read_team_history_records, session_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[team.history.get] read failed: session_id=%s error=%s", session_id, exc)
        records = []

    sanitized_records = [
        _sanitize_history_record_for_wire(record)
        for record in records
        if isinstance(record, dict)
    ]
    total = len(sanitized_records)
    cursor = _coerce_int(
        params.get("cursor", params.get("offset", 0)),
        default=0,
        minimum=0,
        maximum=max(0, total),
    )
    limit = _coerce_int(
        params.get("limit"),
        default=_TEAM_HISTORY_DEFAULT_LIMIT,
        minimum=1,
        maximum=_TEAM_HISTORY_MAX_LIMIT,
    )
    max_bytes = _coerce_int(
        params.get("max_bytes"),
        default=_TEAM_HISTORY_DEFAULT_MAX_BYTES,
        minimum=_TEAM_HISTORY_MIN_MAX_BYTES,
        maximum=_TEAM_HISTORY_MAX_MAX_BYTES,
    )
    page_records, next_cursor = _select_history_record_page(
        sanitized_records,
        cursor=cursor,
        limit=limit,
        max_bytes=max_bytes,
        session_id=session_id,
    )
    logger.debug(
        "[team.history.get] session_id=%s member=%s total=%d cursor=%d returned=%d next_cursor=%d max_bytes=%d",
        session_id, str(member_name or ""), total, cursor,
        len(page_records), next_cursor, max_bytes,
    )

    resp = AgentResponse(
        request_id=request.request_id,
        channel_id=channel_id,
        ok=True,
        payload={
            "records": page_records,
            "session_id": session_id,
            "cursor": cursor,
            "next_cursor": next_cursor,
            "has_more": next_cursor < total,
            "total": total,
        },
    )
    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_team_members_get(ctx: RequestContext) -> None:
    """返回 team human_agent 席位列表供 /join 校验。

    纯查询透传：mismatch 校验与对外文案均在 gateway，server 只查 member、过滤
    human_agent、回 ok/members。查不到或异常 → ok=False（payload 不带文案，由
    gateway 拼"team 不存在"）。
    """
    request = ctx.request
    from jiuwenswarm.server.runtime.agent_adapter.team_helpers import (
        query_team_human_members_for_join,
    )
    from jiuwenswarm.server.runtime.session.session_metadata import (
        get_session_metadata,
        resolve_session_runtime_team_name,
    )

    params = request.params if isinstance(request.params, dict) else {}
    session_id = params.get("session_id") or request.session_id or ""
    metadata = get_session_metadata(
        session_id,
        sessions_root=_sessions_dir_for_request(request),
    )
    team_name = str(
        resolve_session_runtime_team_name(metadata)
        or params.get("team_name")
        or ""
    ).strip()
    channel_id = request.channel_id or "web"

    try:
        members_raw = await query_team_human_members_for_join(session_id, team_name)
    except Exception:
        logger.exception(
            "[AgentWebSocketServer] team.members.get failed: session=%s team=%s",
            session_id, team_name,
        )
        members_raw = []
    members = [
        m for m in members_raw
        if isinstance(m, dict) and m.get("role") == "human_agent" and m.get("member_id")
    ]
    # 空列表是「该 team 没有 human_agent 席位」的正常结果，不是服务故障。
    # WS 侧沿用 ok=False（gateway 的 fetch_team_human_members 靠它拼「team 不存在」，
    # 它只读 ok 与 members，多一个键不影响）；额外带 NOT_FOUND，让 HTTP 侧映射成
    # 404 而不是兜底的 500 —— 见 agent_http_server.frame_to_http_envelope。
    payload: dict[str, Any] = {"members": members}
    if not members:
        payload["code"] = "NOT_FOUND"
    resp = AgentResponse(
        request_id=request.request_id,
        channel_id=channel_id,
        ok=bool(members),
        payload=payload,
    )
    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


