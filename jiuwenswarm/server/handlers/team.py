# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""团队，以及暂停、继续、停止这套协作控制。

团队是一组一起干活的成员。这里处理列出模板、绑定团队、把会话挂到团队、
删除团队、看当前看板、往团队里发一条外部消息、查成员、看团队历史。

暂停、继续、停止是同一件事的三个动作：让正在跑的协作流程停一下、接着跑、
或者结束。三种请求共用一个函数，用请求类型区分动作。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from jiuwenswarm.common.mode_matrix import is_team_mode
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.runtime.session.session_history import (
    read_member_history_records,
    read_team_history_records,
)
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

# 请求类型对应协作控制的动作名。日志和控制器仍认这些名字。
_SWARMFLOW_ACTIONS = {
    ReqMethod.SWARMFLOW_PAUSE: "pause",
    ReqMethod.SWARMFLOW_RESUME: "resume",
    ReqMethod.SWARMFLOW_STOP: "stop",
}


def _server_global(name: str) -> Any:
    """用到时再读 WebSocket 模块上的同名函数。

    测试经常改的是那一层的名字。函数里当场去取，改动能跟着生效。
    """

    import jiuwenswarm.server.agent_ws_server as ws_server

    return getattr(ws_server, name)


def _is_team_metadata_mode(metadata: dict[str, Any]) -> bool:
    return is_team_mode(metadata.get("mode"))


def _team_binding_payload(binding: Any) -> dict[str, Any]:
    if hasattr(binding, "to_dict"):
        return binding.to_dict()
    if isinstance(binding, dict):
        return dict(binding)
    return {}


async def _create_team_binding_from_template(
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
    from jiuwenswarm.server.runtime.team_entity_store import get_team_entity_store

    normalized_name = validate_team_name(team_name)
    from jiuwenswarm.runtime.session_delete import TEAM_DELETION_GATE
    template_ids = {
        str(item.get("template_id") or "")
        for item in list_team_template_summaries(config_base)
    }
    if template_id not in template_ids:
        raise TeamBindingStoreError("template_id not found", code="NOT_FOUND")

    async with TEAM_DELETION_GATE.mutation_lock(normalized_name):
        TEAM_DELETION_GATE.assert_not_deleting_locked(normalized_name)
        entity_store = get_team_entity_store()
        if entity_store.exists(normalized_name):
            raise TeamBindingStoreError("team_name already exists", code="CONFLICT")
        template_snapshot = get_team_template_snapshot(config_base, template_id=template_id)
        binding_store = get_team_binding_store()
        binding = binding_store.create(team_name=normalized_name, template_id=template_id)
        try:
            entity_store.write(
                team_name=binding.team_name,
                template_id=binding.template_id,
                template_snapshot=template_snapshot,
                created_at=binding.created_at,
            )
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
            binding = await _create_team_binding_from_template(
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


def _active_team_session_map() -> dict[str, str]:
    from jiuwenswarm.agents.harness.team import get_all_team_managers

    active: dict[str, str] = {}
    for manager in get_all_team_managers():
        snapshot_fn = getattr(manager, "get_runtime_team_snapshot", None)
        if not callable(snapshot_fn):
            continue
        for session_id, info in snapshot_fn().items():
            team_name = str(info.get("team_name") or "").strip()
            state = str(info.get("state") or "").strip()
            if team_name and state in {"active", "pending"}:
                active.setdefault(team_name, str(session_id))
    return active


def _legacy_team_bindings_from_sessions(known_team_names: set[str]) -> list[dict[str, Any]]:
    sessions_dir = _server_global("get_agent_sessions_dir")()
    if not sessions_dir.exists():
        return []

    legacy: dict[str, dict[str, Any]] = {}
    for session_dir in sessions_dir.iterdir():
        if not session_dir.is_dir():
            continue
        metadata = _server_global("get_session_metadata")(session_dir.name, cache_bust=True)
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

    templates = list_team_template_summaries(_server_global("get_config")())
    resp = AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=True,
        payload={"templates": templates},
        metadata=request.metadata,
    )
    await ctx.sink.send_response(resp)


async def handle_team_bindings_list(ctx: RequestContext) -> None:
    request = ctx.request
    from jiuwenswarm.agents.harness.team import list_team_template_summaries
    from jiuwenswarm.server.runtime.team_binding_store import get_team_binding_store
    from jiuwenswarm.server.runtime.team_entity_store import (
        ensure_team_entity_for_binding,
        get_team_entity_store,
    )

    active_by_team = _active_team_session_map()
    config_base = _server_global("get_config")()
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
    await ctx.sink.send_response(resp)


async def handle_team_binding_create(ctx: RequestContext) -> None:
    request = ctx.request
    from jiuwenswarm.runtime.session_delete import TeamDeletionInProgress
    from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStoreError
    from jiuwenswarm.server.runtime.team_entity_store import TeamEntityStoreError

    params = request.params if isinstance(request.params, dict) else {}
    team_name = str(params.get("team_name") or "")
    template_id = str(params.get("template_id") or "").strip()
    config_base = _server_global("get_config")()
    try:
        binding = await _create_team_binding_from_template(
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
    except (TeamBindingStoreError, TeamEntityStoreError, TeamDeletionInProgress) as exc:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(exc), "code": getattr(exc, "code", "BAD_REQUEST")},
            metadata=request.metadata,
        )

    await ctx.sink.send_response(resp)


async def handle_team_binding_generate(ctx: RequestContext) -> None:
    request = ctx.request
    from jiuwenswarm.agents.harness.team import (
        TeamNameGenerationError,
    )
    from jiuwenswarm.runtime.session_delete import TeamDeletionInProgress
    from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStoreError
    from jiuwenswarm.server.runtime.team_entity_store import TeamEntityStoreError

    params = request.params if isinstance(request.params, dict) else {}
    description = str(params.get("description") or params.get("prompt") or "").strip()
    config_base = _server_global("get_config")()
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
    except (TeamBindingStoreError, TeamEntityStoreError, TeamDeletionInProgress) as exc:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(exc), "code": getattr(exc, "code", "BAD_REQUEST")},
            metadata=request.metadata,
        )

    await ctx.sink.send_response(resp)


async def handle_team_session_bind(ctx: RequestContext) -> None:
    request = ctx.request
    from jiuwenswarm.runtime.session_delete import TeamDeletionInProgress
    from jiuwenswarm.server.runtime.session.session_metadata import (
        update_session_metadata,
    )
    from jiuwenswarm.server.runtime.team_binding_store import (
        TeamBindingStoreError,
        get_team_binding_store,
    )
    from jiuwenswarm.server.runtime.team_entity_store import (
        TeamEntityStoreError,
        ensure_team_entity_for_binding,
        get_team_entity_store,
    )

    params = request.params if isinstance(request.params, dict) else {}
    session_id = str(params.get("session_id") or request.session_id or "").strip()
    team_name = str(params.get("team_name") or "").strip()
    _, _, canonical_mode = _server_global("resolve_agent_request_mode")(params.get("mode", "team"))
    try:
        if not session_id:
            raise TeamBindingStoreError("session_id is required", code="BAD_REQUEST")
        if not (_server_global("get_agent_sessions_dir")() / session_id).is_dir():
            raise TeamBindingStoreError("session not found", code="NOT_FOUND")
        binding_store = get_team_binding_store()
        existing_binding = binding_store.get(team_name)
        if existing_binding is None:
            raise TeamBindingStoreError("team binding not found", code="NOT_FOUND")
        entity = ensure_team_entity_for_binding(existing_binding, config_base=_server_global("get_config")())
        if entity is None:
            raise TeamBindingStoreError("team entity config missing", code="NOT_FOUND")
        from jiuwenswarm.runtime.session_delete import TEAM_DELETION_GATE

        async with TEAM_DELETION_GATE.mutation_lock(team_name):
            TEAM_DELETION_GATE.assert_not_deleting_locked(team_name)
            binding = binding_store.bind_session(
                team_name=team_name,
                session_id=session_id,
            )
            update_session_metadata(
                session_id=session_id,
                channel_id=str(request.channel_id or "").strip() or None,
                mode=canonical_mode,
                team_name=binding.team_name,
                team_template_id=binding.template_id,
                sync=True,
            )
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
    except (TeamBindingStoreError, TeamEntityStoreError, TeamDeletionInProgress) as exc:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(exc), "code": getattr(exc, "code", "BAD_REQUEST")},
            metadata=request.metadata,
        )

    await ctx.sink.send_response(resp)


async def handle_team_delete(ctx: RequestContext) -> None:
    """Validate transport input and delegate Team deletion to AgentRuntime."""
    request = ctx.request
    services = ctx.services

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
            result = await services.execution_runtime().delete_team(
                team_name=team_name,
                channel_id=request.channel_id,
            )
            payload = {
                "team_name": result.team_name,
                "session_ids": list(result.session_ids),
                "failed_session_ids": list(result.failed_session_ids),
                "deleted": result.deleted,
                "recovery_required": result.recovery_required,
            }
            if result.error_message:
                payload["error"] = result.error_message
            if result.error_code:
                payload["code"] = result.error_code
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=result.ok,
                payload=payload,
                metadata=request.metadata,
            )
        except Exception as exc:
            logger.exception("[AgentWebSocketServer] Runtime Team delete failed")
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "DELETE_FAILED"},
                metadata=request.metadata,
            )

    await ctx.sink.send_response(resp)


async def handle_team_snapshot(ctx: RequestContext) -> None:
    request = ctx.request
    from jiuwenswarm.agents.harness.team import get_team_manager
    from jiuwenswarm.agents.harness.team.handlers.team_monitor_handler import (
        TeamMonitorHandler,
    )

    params = request.params if isinstance(request.params, dict) else {}
    session_id = str(params.get("session_id") or request.session_id or "").strip()
    channel_id = request.channel_id or "web"
    empty_payload = {"members": [], "tasks": [], "team_id": None}
    # team_name 是调用方点名要看的团队。一个会话可以同时挂多个团队，
    # 会话级看板只记住最后一个。对不上就不要把别的团队画上来。
    requested_team_name = str(params.get("team_name") or "").strip()

    team_manager = get_team_manager(channel_id)
    monitor_handler = team_manager.get_monitor_handler(session_id) if session_id else None
    monitor_team = monitor_handler.team_id if monitor_handler is not None else None
    live_matches_request = (
        monitor_handler is not None
        and monitor_handler.is_running
        and (not requested_team_name or monitor_team == requested_team_name)
    )

    snapshot: dict[str, Any] | None = None
    source = "empty"
    if live_matches_request:
        try:
            snapshot = await monitor_handler.get_team_snapshot()
            if snapshot is not None:
                source = "live"
        except Exception as e:  # noqa: BLE001
            logger.warning("[AgentWebSocketServer] team.snapshot (live) failed: %s", e)

    def _snapshot_tasks(payload: dict[str, Any] | None) -> list[Any]:
        if not isinstance(payload, dict):
            return []
        tasks = payload.get("tasks")
        return tasks if isinstance(tasks, list) else []

    # 点名的团队还活着，但会话级看板不是它：另开一个只读看板读一次，用完就丢。
    if snapshot is None and requested_team_name and session_id:
        try:
            from openjiuwen.core.runner.runner import GLOBAL_RUNNER

            scoped_monitor = await GLOBAL_RUNNER.get_agent_team_monitor(
                team_name=requested_team_name,
                session_id=session_id,
            )
            if scoped_monitor is not None:
                scoped_snapshot = await TeamMonitorHandler(scoped_monitor, session_id).get_team_snapshot()
                if scoped_snapshot is not None:
                    snapshot = scoped_snapshot
                    source = "live-scoped"
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[AgentWebSocketServer] team.snapshot (scoped live) failed: "
                "session_id=%s team_name=%s error=%s",
                session_id,
                requested_team_name,
                e,
            )

    # History restore often hits this RPC after the monitor has stopped, OR
    # while a live handler is still registered but already returns a truthy
    # empty board ({tasks: [], members: [], team_id: ...}). `if not snapshot`
    # alone would skip DB in that case and leave the frontend with no
    # title/content. Fall back whenever live has no tasks.
    needs_db = snapshot is None or not _snapshot_tasks(snapshot)
    if needs_db and session_id:
        team_name = requested_team_name
        if not team_name:
            team_name = str(
                team_manager.get_active_team_name(session_id) or ""
            ).strip()
        if not team_name:
            team_name = str(
                (_server_global("get_session_metadata")(session_id) or {}).get("team_name") or ""
            ).strip()
        if team_name:
            try:
                db_snapshot = await TeamMonitorHandler.get_team_snapshot_from_db(
                    session_id, team_name
                )
            except Exception as e:  # noqa: BLE001
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

    payload = {
        **(snapshot or empty_payload),
        "members_source": source if snapshot is not None else "empty",
    }
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
    await ctx.sink.send_response(resp)


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
    await ctx.sink.send_response(resp)


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

    params = request.params if isinstance(request.params, dict) else {}
    session_id = params.get("session_id") or request.session_id or ""
    team_name = str(params.get("team_name") or "").strip()
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
    resp = AgentResponse(
        request_id=request.request_id,
        channel_id=channel_id,
        ok=bool(members),
        payload={"members": members},
    )
    await ctx.sink.send_response(resp)


async def handle_swarmflow_control(ctx: RequestContext) -> None:
    """Shared pause/resume/stop control-path handler for a swarmflow run.

    Looks up the session's BackgroundTaskController and applies the requested
    control to the run identified by ``run_id`` (accepting ``run_id`` or the
    ``workflow_run_id`` alias). Returns ok=False with a reason when run_id is
    missing, the team is asleep (no leader harness to host the run — the
    tree-view buttons are greyed then), or no matching run
    is registered on the controller.
    """
    request = ctx.request
    action = _SWARMFLOW_ACTIONS[request.req_method]
    from jiuwenswarm.server.runtime.agent_adapter.team_helpers import (
        classify_swarmflow_control_miss,
        get_background_task_controller,
    )

    session_id = request.session_id or ""
    channel_id = request.channel_id or "web"
    params = request.params if isinstance(request.params, dict) else {}
    run_id = params.get("run_id") or params.get("workflow_run_id")

    from openjiuwen.agent_teams.runtime.pool import RuntimeState
    from openjiuwen.core.runner import Runner

    from jiuwenswarm.agents.harness.team import get_team_manager

    tm = get_team_manager(channel_id)
    active_teams = await Runner.list_active_teams()
    team_runtime_running = any(
        str(info.current_session_id or "") == session_id
        and info.state == RuntimeState.RUNNING
        for info in active_teams
    )
    if not isinstance(run_id, str) or not run_id.strip():
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=channel_id,
            ok=False,
            payload={"error": "run_id is required"},
        )
    elif not team_runtime_running:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=channel_id,
            ok=False,
            payload={"error": "工作流控制不可用，发送一条消息唤醒团队后即可使用。"},
        )
    else:
        run_id = run_id.strip()
        controller = get_background_task_controller(session_id)
        status = {"pause": "paused", "resume": "resumed", "stop": "stopped"}.get(action, "")
        if action == "pause":
            acted = await controller.pause(run_id)
        elif action == "resume":
            acted = await controller.resume(run_id)
        elif action == "stop":
            acted = await controller.stop(run_id)
            # 已解栈的 paused run 没有引擎回发的 WORKFLOW_STOPPED，快照会永远停在
            # paused；由 handler 合成终态 delta（树刷新 + 落盘），不写 journal seal
            # （丢票不 seal，手动 resume_id 仍可续）。active run 由引擎事件路径更新，
            # stop_run 对非 paused run 是 no-op。
            wf_handler = tm.get_workflow_handler(session_id)
            if wf_handler is not None and await wf_handler.stop_run(run_id):
                acted = True
        else:  # pragma: no cover - internal dispatch only
            acted = False
        if not acted:
            # controller 注册表 miss ≠ run 不存在：run 可能已经自然终态
            # （用户点的是一张停在 running/paused 的陈旧卡片），也可能是
            # 进程重启后注册表清空而状态快照还在。由 team 层找回权威
            # status 一并返回，前端据此纠正卡片并明确提示，而不是
            # 一句 not found。
            miss = classify_swarmflow_control_miss(channel_id, session_id, run_id)
            if miss is not None:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=channel_id,
                    ok=False,
                    payload=miss,
                )
            else:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=channel_id,
                    ok=False,
                    payload={"error": "workflow run not found"},
                )
        else:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=channel_id,
                ok=True,
                payload={"run_id": run_id, "status": status},
            )

    logger.info(
        "[SWARMFLOW] %s req channel_id=%s session_id=%s request_id=%s run_id=%s ok=%s",
        action,
        channel_id,
        session_id,
        request.request_id,
        run_id,
        resp.ok,
    )
    await ctx.sink.send_response(resp)


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
        await ctx.sink.send_response(resp)
        return

    session_id = session_id.strip()
    try:
        if member_name and isinstance(member_name, str) and member_name.strip():
            records = await asyncio.to_thread(
                read_member_history_records,
                session_id,
                str(member_name).strip(),
            )
        else:
            records = await asyncio.to_thread(
                read_team_history_records,
                session_id,
            )
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
    await ctx.sink.send_response(resp)
