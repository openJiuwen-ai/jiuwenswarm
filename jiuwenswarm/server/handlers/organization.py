# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""同一个会话里的多个团队：列出团队、看组织任务、把话直接发给某个专家团。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from jiuwenswarm.common.mode_matrix import is_team_mode
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.runtime.session.session_history import append_history_record
from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

logger = logging.getLogger(__name__)


def request_query_text(request: AgentRequest) -> str:
    """只取文字问题。结构化事件交给后面的流程。"""
    if not isinstance(request.params, dict):
        return ""
    query = request.params.get("query")
    if not isinstance(query, str):
        return ""
    return query.strip()


def organization_stream_key(request: AgentRequest) -> str:
    """流式任务的钥匙。点名某个专家团时，不和会话主人的那一轮共用一把钥匙。"""
    params = request.params if isinstance(request.params, dict) else {}
    session_id = request.session_id or "default"
    team_id = str(params.get("target_team_id") or "").strip()
    return f"{session_id}::organization::{team_id}" if team_id else session_id


def is_targeted_expert_chat(request: AgentRequest) -> bool:
    """这次 chat.send 是不是点名发给某个非主人的团队。"""
    if request.req_method != ReqMethod.CHAT_SEND:
        return False
    params = request.params if isinstance(request.params, dict) else {}
    if not is_team_mode(params.get("mode")):
        return False
    team_id = str(params.get("target_team_id") or "").strip()
    if not team_id:
        return False
    session_id = str(request.session_id or params.get("session_id") or "").strip()
    owner_team_id = str((get_session_metadata(session_id) or {}).get("team_name") or "").strip()
    return team_id != owner_team_id


def _resolve_team_backend(session_id: str, team_id: str) -> Any | None:
    """只返回正好绑在这个会话、这个团队上的后端。对不上就当没有。"""
    if not session_id or not team_id:
        return None
    try:
        from openjiuwen.core.runner.runner import GLOBAL_RUNNER

        runtime_mgr = getattr(GLOBAL_RUNNER, "_team_runtime_manager", None)
        pool = getattr(runtime_mgr, "pool", None)
        teams = getattr(pool, "_teams", None)
        if not isinstance(teams, dict):
            return None
        active = teams.get(team_id)
        if active is None or getattr(active, "current_session_id", None) != session_id:
            return None
        return getattr(active.agent, "team_backend", None)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[AgentWebSocketServer] resolve team backend failed: session_id=%s team_id=%s error=%s",
            session_id,
            team_id,
            exc,
        )
    return None


def _lookup_session_org_binding(session_id: str, team_id: str) -> str | None:
    """尽量从活着的后端上读出这个团队属于哪个组织。"""
    if not session_id or not team_id:
        return None
    try:
        from openjiuwen.core.runner.runner import GLOBAL_RUNNER

        runtime_mgr = getattr(GLOBAL_RUNNER, "_team_runtime_manager", None)
        pool = getattr(runtime_mgr, "pool", None)
        teams = getattr(pool, "_teams", None)
        if not isinstance(teams, dict):
            return None
        active = teams.get(team_id)
        if active is None or getattr(active, "current_session_id", None) != session_id:
            return None
        backend = getattr(active.agent, "team_backend", None)
        organization_id = getattr(getattr(backend, "org_task_manager", None), "organization_id", None)
        return str(organization_id) if organization_id else None
    except Exception:
        return None


async def cancel_targeted_expert(ctx: RequestContext) -> AgentResponse:
    """只停掉点名的那个专家团，不停会话主人正在说的那一轮。"""
    request = ctx.request
    params = request.params if isinstance(request.params, dict) else {}
    session_id = str(request.session_id or "").strip()
    team_id = str(params.get("target_team_id") or "").strip()
    owner_id = str((get_session_metadata(session_id) or {}).get("team_name") or "")
    owner = _resolve_team_backend(session_id, owner_id)
    target = _resolve_team_backend(session_id, team_id)
    owner_org = getattr(getattr(owner, "org_task_manager", None), "organization_id", None)
    target_org = getattr(getattr(target, "org_task_manager", None), "organization_id", None)
    intent = str(params.get("intent") or "cancel")
    success = bool(owner_org and owner_org == target_org and team_id != owner_id and intent == "cancel")
    if success:
        tasks = []
        for task, stop in list(ctx.services.session_stream_tasks.get(organization_stream_key(request), {}).items()):
            if not task.done():
                stop.set()
                task.cancel()
                tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    return AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=success,
        payload={
            "event_type": "chat.interrupt_result",
            "success": success,
            "team_id": team_id,
            "source": "org_expert_direct",
            **({"error": "expert target is invalid or intent is unsupported"} if not success else {}),
        },
    )


async def targeted_expert_runtime_stream(request: AgentRequest) -> Any:
    """把专家团的输出改成现有流式回包能接着用的事件。"""
    from jiuwenswarm.runtime.events import RuntimeEvent

    stream = _targeted_expert_response_stream(request)
    try:
        async for chunk in stream:
            yield RuntimeEvent.from_agent_message(
                chunk,
                request_id=request.request_id,
                channel_id=request.channel_id or "default",
                session_id=request.session_id,
            )
    finally:
        await stream.aclose()


async def _targeted_expert_response_stream(request: AgentRequest) -> Any:
    """让选中的专家团像普通一轮对话一样跑，并且可以取消。"""
    params = request.params if isinstance(request.params, dict) else {}
    session_id = str(request.session_id or "").strip()
    team_id = str(params.get("target_team_id") or "").strip()
    channel_id = request.channel_id or "default"

    def _chunk(payload: dict[str, Any], *, complete: bool = False) -> AgentResponseChunk:
        return AgentResponseChunk(
            request_id=request.request_id,
            channel_id=channel_id,
            payload=payload,
            is_complete=complete,
        )

    error = ""
    owner_team_id = ""
    if session_id:
        owner_team_id = str((get_session_metadata(session_id) or {}).get("team_name") or "").strip()
    owner_backend = _resolve_team_backend(session_id, owner_team_id)
    target_backend = _resolve_team_backend(session_id, team_id)
    owner_org_id = str(getattr(getattr(owner_backend, "org_task_manager", None), "organization_id", None) or "").strip()
    target_org_id = str(
        getattr(getattr(target_backend, "org_task_manager", None), "organization_id", None) or ""
    ).strip()
    if not session_id or target_backend is None:
        error = "target expert team is not bound to this session"
    elif not owner_org_id or target_org_id != owner_org_id:
        error = "target expert team does not belong to the owner's organization"
    precheck_failed = bool(error)

    succeeded = False
    if not error:
        from jiuwenswarm.agents.harness.team.expert_org.launcher import JiuwenExpertTeamLauncher

        query = request_query_text(request)
        append_history_record(
            session_id=session_id,
            request_id=request.request_id,
            channel_id=channel_id,
            role="user",
            content=query,
            timestamp=time.time(),
            extra={
                "team_id": team_id,
                "team_name": team_id,
                "source": "org_expert_direct",
            },
            channel_metadata=request.metadata,
            mode="team",
        )
        succeeded = await JiuwenExpertTeamLauncher().run_organization_turn(
            team_id,
            session_id,
            {
                "query": query,
                **{key: params[key] for key in ("media_items", "files") if key in params},
            },
            source="org_expert_direct",
            channel_id=channel_id,
            request_id=request.request_id,
        )
        if not succeeded:
            error = "target expert team is unavailable or busy"

    if error:
        if precheck_failed:
            logger.warning(
                "[AgentWebSocketServer] targeted expert chat denied session=%s "
                "owner_team=%s target_team=%s owner_org=%s target_org=%s error=%s",
                session_id,
                owner_team_id,
                team_id,
                owner_org_id,
                target_org_id,
                error,
            )
        error_payload = {
            "event_type": "chat.error",
            "error": error,
            "team_id": team_id,
            "team_name": team_id,
            "source": "org_expert_direct",
        }
        if not precheck_failed:
            append_history_record(
                session_id=session_id,
                request_id=request.request_id,
                channel_id=channel_id,
                role="assistant",
                event_type="chat.error",
                content=error,
                timestamp=time.time(),
                extra=error_payload,
                mode="team",
            )
        yield _chunk(error_payload)
    yield _chunk(
        {
            "event_type": "chat.processing_status",
            "is_processing": False,
            "team_id": team_id,
            "team_name": team_id,
            "source": "org_expert_direct",
            "success": succeeded,
        },
        complete=True,
    )


async def _team_list_entries(session_id: str, channel_id: str) -> list[dict[str, Any]]:
    """收集这个会话现在拥有的全部团队。

    一个会话可以同时挂好几个团队，它们不在同一个登记处：
    运行时的团队池里是正在跑的；团队管理器的标记和会话记录里是还没进池、
    或者已经拆掉但还能接着用的。后登记的只补空，不覆盖已经记下的活状态。
    """
    from jiuwenswarm.agents.harness.team import get_team_manager

    entries: dict[str, dict[str, Any]] = {}

    def _upsert(team_name: str, *, state: str, **extra: Any) -> None:
        team_name = str(team_name or "").strip()
        if not team_name:
            return
        existing = entries.get(team_name)
        if existing is None:
            entries[team_name] = {
                "team_id": team_name,
                "team_name": team_name,
                "display_name": None,
                "state": state,
                "leader_id": None,
                "organization_id": None,
                "is_leader": True,
                "capabilities": [],
                "agent_group_name": None,
                "source": extra.get("source") or "runtime",
            }
            existing = entries[team_name]
        for key, value in extra.items():
            if value is not None and existing.get(key) in (None, [], ""):
                existing[key] = value
        if state == "running" or existing.get("state") in (None, ""):
            existing["state"] = state

    runner_pool = None
    try:
        from openjiuwen.core.runner.runner import GLOBAL_RUNNER

        runtime_mgr = getattr(GLOBAL_RUNNER, "_team_runtime_manager", None)
        runner_pool = getattr(runtime_mgr, "pool", None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[AgentWebSocketServer] team.list pool lookup failed: %s", exc)

    if runner_pool is not None and session_id:
        try:
            for active in await runner_pool.teams_for_session(session_id):
                backend = getattr(active.agent, "team_backend", None)
                if backend is not None and not getattr(backend, "is_leader", True):
                    continue
                spec_metadata = dict(getattr(getattr(active.agent, "spec", None), "metadata", None) or {})
                organization_id = getattr(getattr(backend, "org_task_manager", None), "organization_id", None)
                _upsert(
                    active.team_name,
                    state=getattr(getattr(active, "state", None), "value", None) or "running",
                    display_name=getattr(getattr(active.agent, "card", None), "name", None),
                    leader_id=getattr(backend, "leader_member_name", None),
                    organization_id=organization_id,
                    agent_group_name=spec_metadata.get("agent_group_name"),
                    source="pool",
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AgentWebSocketServer] team.list pool read failed: session_id=%s error=%s",
                session_id,
                exc,
            )

    team_manager = get_team_manager(channel_id)
    metadata_team_name = ""
    if session_id:
        session_info = team_manager.get_runtime_team_snapshot().get(session_id)
        if session_info:
            _upsert(
                str(session_info.get("team_name") or ""),
                state="running" if session_info.get("state") == "active" else "pending",
                source="manager",
            )
        active_name = team_manager.get_active_team_name(session_id)
        if active_name:
            _upsert(active_name, state="running", source="manager")
        pending_name = vars(team_manager).get("_pending_team_names", {}).get(session_id)
        if pending_name:
            _upsert(pending_name, state="pending", source="manager")
        metadata = get_session_metadata(session_id) or {}
        metadata_team_name = str(metadata.get("team_name") or "").strip()
        if metadata_team_name:
            _upsert(
                metadata_team_name,
                state="configured",
                agent_group_name=metadata.get("agent_group_name"),
                source="metadata",
            )

    for entry in entries.values():
        entry["is_owner"] = bool(metadata_team_name and entry["team_id"] == metadata_team_name)
        if not entry.get("organization_id"):
            org_id = _lookup_session_org_binding(session_id, entry["team_id"])
            if org_id:
                entry["organization_id"] = org_id
    return list(entries.values())


async def handle_team_list(ctx: RequestContext) -> None:
    """列出这个会话里的团队，给团队选择器用。"""
    request = ctx.request
    params = request.params if isinstance(request.params, dict) else {}
    session_id = str(params.get("session_id") or request.session_id or "").strip()
    channel_id = request.channel_id or "web"
    teams: list[dict[str, Any]] = []
    try:
        if session_id:
            teams = await _team_list_entries(session_id, channel_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[AgentWebSocketServer] team.list failed: session_id=%s error=%s", session_id, exc)

    default_team_id = None
    if teams:
        default_team_id = next(
            (item["team_id"] for item in teams if item.get("is_owner")),
            next((item["team_id"] for item in teams if item.get("state") == "running"), teams[0]["team_id"]),
        )
    logger.info("[AgentWebSocketServer] team.list session_id=%s teams_count=%s", session_id or "-", len(teams))
    await ctx.sink.send_response(
        AgentResponse(
            request_id=request.request_id,
            channel_id=channel_id,
            ok=True,
            payload={"session_id": session_id, "teams": teams, "default_team_id": default_team_id},
        )
    )


async def _build_org_snapshot(
    *,
    session_id: str,
    team_id: str,
    organization_id: str,
    backend: Any,
) -> dict[str, Any]:
    """通过这个进程里的组织管理器，读组织说明和任务池。"""
    from openjiuwen.agent_teams.organization.pool import get_process_org_manager

    empty_payload: dict[str, Any] = {
        "organization": None,
        "tasks": [],
        "unclaimed_tasks": [],
        "pending_reviews": [],
        "stats": {},
    }
    db = getattr(backend, "db", None)
    if db is None:
        logger.warning(
            "[AgentWebSocketServer] org.snapshot has no db handle: session_id=%s team_id=%s",
            session_id or "-",
            team_id or "-",
        )
        return empty_payload

    manager = get_process_org_manager(
        organization_id=organization_id,
        db=db,
        messager=getattr(backend, "messager", None),
        session_id=session_id or None,
    )
    task_pool = manager.task_pool
    organization = await manager.get_organization()
    if organization is None:
        return empty_payload

    tasks = await task_pool.list_tasks(limit=200)
    unclaimed = [task for task in tasks if task.unclaimed is not None]
    open_tasks = [task for task in tasks if str(task.status) == "OPEN"]
    pending_reviews: list[dict[str, Any]] = []
    if team_id:
        try:
            pending_reviews = await task_pool.list_pending_reviews(team_id=team_id, limit=50)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AgentWebSocketServer] org.snapshot pending reviews failed: "
                "organization_id=%s team_id=%s error=%s",
                organization_id,
                team_id,
                exc,
            )

    status_counts: dict[str, int] = {}
    for task in tasks:
        key = str(task.status)
        status_counts[key] = status_counts.get(key, 0) + 1

    return {
        "organization": {
            "organization_id": organization.organization_id,
            "display_name": organization.display_name,
            "description": organization.description,
            "owner_team_id": organization.owner_team_id,
            "owner_leader_id": organization.owner_leader_id,
            "leaders": [
                {
                    "organization_id": leader.organization_id,
                    "team_id": leader.team_id,
                    "leader_id": leader.leader_id,
                    "leader_member_name": leader.leader_member_name,
                    "capabilities": list(leader.capabilities or []),
                }
                for leader in organization.leaders
            ],
            "metadata": dict(organization.metadata or {}),
            "unclaimed_task_policy": organization.unclaimed_task_policy.model_dump(),
        },
        "tasks": [task.brief() | {"description": (task.description or "")[:280]} for task in tasks],
        "unclaimed_tasks": [task.brief() for task in unclaimed],
        "pending_reviews": pending_reviews,
        "stats": {
            "total": len(tasks),
            "open": len(open_tasks),
            "unclaimed": len(unclaimed),
            "pending_reviews": len(pending_reviews),
            "by_status": status_counts,
        },
    }


async def handle_org_snapshot(ctx: RequestContext) -> None:
    """给右侧信息框返回组织和任务池。组织只从这条会话绑着的团队后端来，调用方带的组织号只用来核对。"""
    request = ctx.request
    params = request.params if isinstance(request.params, dict) else {}
    session_id = str(params.get("session_id") or request.session_id or "").strip()
    team_id = str(params.get("team_id") or "").strip()
    requested_organization_id = str(params.get("organization_id") or "").strip()
    channel_id = request.channel_id or "web"
    empty_payload: dict[str, Any] = {
        "organization": None,
        "tasks": [],
        "unclaimed_tasks": [],
        "pending_reviews": [],
        "stats": {},
    }
    backend = _resolve_team_backend(session_id, team_id)
    organization_id = str(getattr(getattr(backend, "org_task_manager", None), "organization_id", None) or "").strip()
    if not backend or not organization_id:
        payload: dict[str, Any] = dict(empty_payload)
    elif requested_organization_id and requested_organization_id != organization_id:
        logger.warning(
            "[AgentWebSocketServer] org.snapshot organization mismatch: "
            "session_id=%s team_id=%s requested=%s bound=%s",
            session_id or "-",
            team_id or "-",
            requested_organization_id,
            organization_id,
        )
        payload = dict(empty_payload)
    else:
        payload = await _build_org_snapshot(
            session_id=session_id,
            team_id=team_id,
            organization_id=organization_id,
            backend=backend,
        )
    logger.info(
        "[AgentWebSocketServer] org.snapshot session_id=%s team_id=%s organization_id=%s tasks=%s",
        session_id or "-",
        team_id or "-",
        organization_id or "-",
        len(payload.get("tasks") or []),
    )
    await ctx.sink.send_response(
        AgentResponse(
            request_id=request.request_id,
            channel_id=channel_id,
            ok=True,
            payload=payload,
        )
    )


async def handle_a4p_request(ctx: RequestContext) -> None:
    """处理网页授权那边转发来的 a4p 请求。"""
    from jiuwenswarm.agents.harness.common.a4p_rpc import dispatch_a4p_request

    resp = await dispatch_a4p_request(ctx.request)
    await ctx.sink.send_response(resp)
