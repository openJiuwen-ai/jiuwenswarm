# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Teams management; resources remain owned by its injected host."""

from __future__ import annotations

from jiuwenswarm.common.schema.message import ReqMethod

import logging
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.common.schema.agent import AgentResponseChunk
from typing import Any
from jiuwenswarm.runtime.events import RuntimeEvent
from jiuwenswarm.server.runtime.session.session_history import append_history_record
import asyncio
from jiuwenswarm.common.utils import get_agent_sessions_dir
from jiuwenswarm.common.config import get_config
from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata
from jiuwenswarm.server.utils.utils import is_team_params
from jiuwenswarm.runtime.request import resolve_agent_request_mode
import time
from gateway.common.mode_matrix import is_team_mode

logger = logging.getLogger(__name__)


def _request_query_text(request: AgentRequest) -> str:
    """Return text chat query only; structured events are handled downstream."""
    if not isinstance(request.params, dict):
        return ""
    query = request.params.get("query")
    if not isinstance(query, str):
        return ""
    return query.strip()


class TeamsManagement:
    def __init__(self, host):
        self.host = host

    async def _find_team_session_ids(self, team_name: str) -> list[str]:
        host = self.host
        sessions_dir = get_agent_sessions_dir()
        if not sessions_dir.exists():
            return []
        matched_session_ids: list[str] = []
        for session_dir in sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            session_id = session_dir.name
            metadata = get_session_metadata(session_id)
            if not host._is_team_metadata_mode(metadata):
                continue
            metadata_team_name = str(metadata.get("team_name") or "").strip()
            if metadata_team_name == team_name:
                matched_session_ids.append(session_id)
        return sorted(set(matched_session_ids))

    @staticmethod
    def _team_binding_payload(binding: Any) -> dict[str, Any]:
        if hasattr(binding, "to_dict"):
            return binding.to_dict()
        if isinstance(binding, dict):
            return dict(binding)
        return {}

    @staticmethod
    async def _create_team_binding_from_template(
        *, team_name: str, template_id: str, config_base: dict[str, Any]
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
            template_snapshot = get_team_template_snapshot(
                config_base, template_id=template_id
            )
            binding_store = get_team_binding_store()
            binding = binding_store.create(
                team_name=normalized_name, template_id=template_id
            )
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

    @classmethod
    async def _create_generated_team_binding(
        cls,
        *,
        description: str,
        config_base: dict[str, Any],
        create_binding: Any = None,
    ) -> tuple[Any, dict[str, Any]]:
        """Generate a unique team name and persist its binding and entity."""
        from jiuwenswarm.agents.harness.team import (
            generate_team_name,
            list_team_template_summaries,
        )
        from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStoreError

        normalized_description = str(description or "").strip()
        if not normalized_description:
            raise TeamBindingStoreError("description is required", code="BAD_REQUEST")
        templates = list_team_template_summaries(config_base)
        if not templates:
            raise TeamBindingStoreError("no team template configured", code="NOT_FOUND")
        default_template = templates[0]
        template_id = str(default_template.get("template_id") or "").strip()
        generated_name = await generate_team_name(
            normalized_description, config_base=config_base, template_id=template_id
        )
        for candidate_index in range(100):
            suffix = "" if candidate_index == 0 else f"_{candidate_index + 1}"
            candidate = f"{generated_name[: 64 - len(suffix)]}{suffix}"
            try:
                factory = (
                    create_binding
                    if create_binding is not None
                    else cls._create_team_binding_from_template
                )
                binding = await factory(
                    team_name=candidate,
                    template_id=template_id,
                    config_base=config_base,
                )
                return (binding, default_template)
            except TeamBindingStoreError as exc:
                if exc.code != "CONFLICT":
                    raise
        raise TeamBindingStoreError(
            "unable to allocate a unique team_name", code="CONFLICT"
        )

    @staticmethod
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

    @staticmethod
    def _legacy_team_bindings_from_sessions(
        known_team_names: set[str],
    ) -> list[dict[str, Any]]:
        sessions_dir = get_agent_sessions_dir()
        if not sessions_dir.exists():
            return []
        legacy: dict[str, dict[str, Any]] = {}
        for session_dir in sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            metadata = get_session_metadata(session_dir.name, cache_bust=True)
            if not metadata or not is_team_mode(metadata.get("mode")):
                continue
            team_name = str(metadata.get("team_name") or "").strip()
            if not team_name or team_name in known_team_names:
                continue
            item = legacy.setdefault(
                team_name,
                {
                    "team_name": team_name,
                    "template_id": str(
                        metadata.get("team_template_id") or team_name
                    ).strip()
                    or team_name,
                    "created_at": float(
                        metadata.get("created_at") or session_dir.stat().st_ctime
                    ),
                    "updated_at": float(
                        metadata.get("last_message_at") or session_dir.stat().st_mtime
                    ),
                    "session_ids": [],
                    "last_session_id": "",
                    "legacy": True,
                },
            )
            if session_dir.name not in item["session_ids"]:
                item["session_ids"].append(session_dir.name)
            if float(metadata.get("last_message_at") or 0) >= float(
                item.get("updated_at") or 0
            ):
                item["updated_at"] = float(
                    metadata.get("last_message_at") or session_dir.stat().st_mtime
                )
                item["last_session_id"] = session_dir.name
        return list(legacy.values())

    async def _handle_team_templates_list(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        host = self.host
        from jiuwenswarm.agents.harness.team import list_team_template_summaries

        templates = list_team_template_summaries(get_config())
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"templates": templates},
            metadata=request.metadata,
        )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_team_bindings_list(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        host = self.host
        from jiuwenswarm.agents.harness.team import list_team_template_summaries
        from jiuwenswarm.server.runtime.team_binding_store import get_team_binding_store
        from jiuwenswarm.server.runtime.team_entity_store import (
            ensure_team_entity_for_binding,
            get_team_entity_store,
        )

        active_by_team = host._active_team_session_map()
        config_base = get_config()
        templates = {
            str(item.get("template_id") or ""): item
            for item in list_team_template_summaries(config_base)
        }
        store = get_team_binding_store()
        bindings = store.list()
        bindings_by_name = {binding.team_name: binding for binding in bindings}
        entity_store = get_team_entity_store()
        teams = [host._team_binding_payload(binding) for binding in bindings]
        known_team_names = {str(item.get("team_name") or "") for item in teams}
        teams.extend(host._legacy_team_bindings_from_sessions(known_team_names))
        enriched: list[dict[str, Any]] = []
        for item in teams:
            team_name = str(item.get("team_name") or "").strip()
            template_id = str(item.get("template_id") or "").strip()
            active_session_id = active_by_team.get(team_name, "")
            legacy = bool(item.get("legacy", False))
            entity = None
            entity_path = ""
            if team_name and (not legacy):
                binding = bindings_by_name.get(team_name)
                if binding is not None:
                    entity = ensure_team_entity_for_binding(
                        binding, config_base=config_base, store=entity_store
                    )
                else:
                    entity = entity_store.get(team_name)
                if entity is not None:
                    item["template_id"] = entity.template_id
                    template_id = entity.template_id
                    entity_path = str(entity_store.entity_path(team_name))
            source_template_available = bool(template_id and template_id in templates)
            team_config_available = entity is not None
            template_available = bool(
                team_config_available or source_template_available
            )
            selectable = bool(
                team_name
                and (not active_session_id)
                and (not legacy)
                and team_config_available
            )
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
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_team_binding_create(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        host = self.host
        from jiuwenswarm.runtime.session_delete import TeamDeletionInProgress
        from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStoreError
        from jiuwenswarm.server.runtime.team_entity_store import TeamEntityStoreError

        params = request.params if isinstance(request.params, dict) else {}
        team_name = str(params.get("team_name") or "")
        template_id = str(params.get("template_id") or "").strip()
        config_base = get_config()
        try:
            binding = await host._create_team_binding_from_template(
                team_name=team_name, template_id=template_id, config_base=config_base
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"team": binding.to_dict()},
                metadata=request.metadata,
            )
        except (
            TeamBindingStoreError,
            TeamEntityStoreError,
            TeamDeletionInProgress,
        ) as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "error": str(exc),
                    "code": getattr(exc, "code", "BAD_REQUEST"),
                },
                metadata=request.metadata,
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_team_binding_generate(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        host = self.host
        from jiuwenswarm.agents.harness.team import TeamNameGenerationError
        from jiuwenswarm.runtime.session_delete import TeamDeletionInProgress
        from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStoreError
        from jiuwenswarm.server.runtime.team_entity_store import TeamEntityStoreError

        params = request.params if isinstance(request.params, dict) else {}
        description = str(
            params.get("description") or params.get("prompt") or ""
        ).strip()
        config_base = get_config()
        try:
            binding, default_template = await host._create_generated_team_binding(
                description=description, config_base=config_base
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"team": binding.to_dict(), "template": default_template},
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
        except (
            TeamBindingStoreError,
            TeamEntityStoreError,
            TeamDeletionInProgress,
        ) as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "error": str(exc),
                    "code": getattr(exc, "code", "BAD_REQUEST"),
                },
                metadata=request.metadata,
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_team_session_bind(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        host = self.host
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
        _, _, canonical_mode = resolve_agent_request_mode(params.get("mode", "team"))
        try:
            if not session_id:
                raise TeamBindingStoreError(
                    "session_id is required", code="BAD_REQUEST"
                )
            if not (get_agent_sessions_dir() / session_id).is_dir():
                raise TeamBindingStoreError("session not found", code="NOT_FOUND")
            binding_store = get_team_binding_store()
            existing_binding = binding_store.get(team_name)
            if existing_binding is None:
                raise TeamBindingStoreError("team binding not found", code="NOT_FOUND")
            entity = ensure_team_entity_for_binding(
                existing_binding, config_base=get_config()
            )
            if entity is None:
                raise TeamBindingStoreError(
                    "team entity config missing", code="NOT_FOUND"
                )
            from jiuwenswarm.runtime.session_delete import TEAM_DELETION_GATE

            async with TEAM_DELETION_GATE.mutation_lock(team_name):
                TEAM_DELETION_GATE.assert_not_deleting_locked(team_name)
                binding = binding_store.bind_session(
                    team_name=team_name, session_id=session_id
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
                    "team_config_path": str(
                        get_team_entity_store().entity_path(binding.team_name)
                    ),
                },
                metadata=request.metadata,
            )
        except (
            TeamBindingStoreError,
            TeamEntityStoreError,
            TeamDeletionInProgress,
        ) as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "error": str(exc),
                    "code": getattr(exc, "code", "BAD_REQUEST"),
                },
                metadata=request.metadata,
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_team_delete(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Validate transport input and delegate Team deletion to AgentRuntime."""
        host = self.host
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
                result = await host._execution_runtime().delete_team(
                    team_name=team_name, channel_id=request.channel_id
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
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _cancel_targeted_expert(self, request: AgentRequest) -> AgentResponse:
        """Cancel only a same-organization expert stream, never the owner turn."""
        host = self.host
        params = request.params if isinstance(request.params, dict) else {}
        session_id = str(request.session_id or params.get("session_id") or "").strip()
        team_id = str(params.get("target_team_id") or "").strip()
        owner_id = str((get_session_metadata(session_id) or {}).get("team_name") or "")
        owner = host._resolve_team_backend(session_id, owner_id)
        target = host._resolve_team_backend(session_id, team_id)
        owner_org = getattr(getattr(owner, "org_task_manager", None), "organization_id", None)
        target_org = getattr(getattr(target, "org_task_manager", None), "organization_id", None)
        recovery_error = ""
        is_expert_target = bool(owner_id and team_id != owner_id)
        valid_target = is_team_mode(params.get("mode")) and is_expert_target
        needs_recovery = not owner_org or not target_org
        if needs_recovery and valid_target:
            try:
                # Resolve each missing side separately; never replace a known
                # live binding with the other Team's persisted organization.
                if not owner_org:
                    persisted_owner = await host._load_persisted_organization(session_id, owner_id)
                    if persisted_owner is not None:
                        owner_org = persisted_owner[1].organization_id
                if not target_org:
                    persisted_target = await host._load_persisted_organization(session_id, team_id)
                    if persisted_target is not None:
                        target_org = persisted_target[1].organization_id
            except Exception as exc:
                recovery_error = str(exc)
                logger.warning("[AgentWebSocketServer] expert stop membership lookup failed: %s", exc)
        intent = str(params.get("intent") or "cancel")
        same_organization = bool(owner_org and owner_org == target_org)
        success = valid_target and same_organization and intent == "cancel"
        cancelled_requests = []
        cancelled = []
        if success:
            from jiuwenswarm.server.runtime.organization_execution import stopping_teams

            stream_tasks = getattr(host, "_session_stream_tasks", {})
            streams = list(stream_tasks.get(host._organization_stream_key(request), {}).items())
            # Registry cancellation removes stream bookkeeping in finally. Capture
            # direct request identities first so reconnecting clients receive the
            # explicit terminal on their current connection after the stop.
            expert_requests = getattr(host, "_expert_stream_requests", {})
            for task, _ in streams:
                if task.done():
                    continue
                active_request = expert_requests.get(task)
                if active_request is not None:
                    cancelled_requests.append(active_request)
            async with stopping_teams(session_id, {team_id}) as cancelled:
                await host._stop_organization_team(session_id, team_id)
            tasks = []
            for task, stop in streams:
                if not task.done():
                    stop.set()
                    task.cancel()
                    tasks.append(task)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            for active_request in cancelled_requests:
                await host.send_push({
                    "request_id": active_request.request_id,
                    "channel_id": request.channel_id,
                    "session_id": session_id,
                    "payload": {
                        "event_type": "chat.processing_status", "session_id": session_id,
                        "team_id": team_id, "source": "org_expert_direct",
                        "request_id": active_request.request_id, "is_processing": False,
                        "is_complete": True,
                    },
                    "metadata": request.metadata,
                })
        cancelled_request_ids = {item.request_id for item in cancelled_requests}
        cancelled_request_ids.update(item["request_id"] for item in cancelled)
        # owner 未知时给明确原因：这是「拒绝定向取消」而不是「目标不合法」，客户端据此
        # 可以提示会话尚未就绪，而不是重试一次取消（重试仍会被拒）。
        if owner_id:
            failure_reason = recovery_error or "expert target is invalid or intent is unsupported"
        else:
            failure_reason = (
                "session metadata has no team_name (owner unknown); "
                "refused to fall back to a session-level cancel"
            )
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=success,
            payload={
                "event_type": "chat.interrupt_result",
                "success": success,
                "team_id": team_id,
                "source": "org_expert_direct",
                "cancelled_request_ids": sorted(cancelled_request_ids),
                **({"error": failure_reason} if not success else {}),
            },
        )

    async def _targeted_expert_runtime_stream(self, request: AgentRequest) -> Any:
        """Adapt organization output to the existing Runtime wire/keepalive path."""
        host = self.host
        stream = host._targeted_expert_response_stream(request)
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

    async def _targeted_expert_response_stream(self, request: AgentRequest) -> Any:
        """Run one selected expert as a normal cancellable AgentServer stream."""
        host = self.host
        from jiuwenswarm.server.runtime.team_organization_history import (
            organization_enabled,
        )
        params = request.params if isinstance(request.params, dict) else {}
        session_id = str(request.session_id or "").strip()
        team_id = str(params.get("target_team_id") or "").strip()
        channel_id = request.channel_id or "default"

        def _chunk(
            payload: dict[str, Any], *, complete: bool = False
        ) -> AgentResponseChunk:
            return AgentResponseChunk(
                request_id=request.request_id,
                channel_id=channel_id,
                payload=payload,
                is_complete=complete,
            )

        error = ""
        error_code = ""
        owner_team_id = ""
        if session_id:
            owner_team_id = str(
                (get_session_metadata(session_id) or {}).get("team_name") or ""
            ).strip()
        owner_backend = host._resolve_team_backend(session_id, owner_team_id)
        target_backend = host._resolve_team_backend(session_id, team_id)
        owner_org_id = str(
            getattr(
                getattr(owner_backend, "org_task_manager", None),
                "organization_id",
                None,
            )
            or ""
        ).strip()
        target_org_id = str(
            getattr(
                getattr(target_backend, "org_task_manager", None),
                "organization_id",
                None,
            )
            or ""
        ).strip()
        has_team_target = bool(session_id and team_id)
        has_organization_binding = bool(owner_org_id and target_org_id)
        needs_recovery = target_backend is None or not has_organization_binding
        from jiuwenswarm.server.runtime.organization_packages import (
            team_package_availability,
        )

        availability = team_package_availability(team_id) if has_team_target else {}
        if not organization_enabled():
            error = "Team Organization is disabled"
        elif availability.get("available") is False:
            error = "The expert AgentGroup is uninstalled or unavailable; reinstall it to resume this Team."
            error_code = availability["unavailable_reason"]
        elif has_team_target and needs_recovery:
            try:
                persisted = await host._load_persisted_organization(session_id, team_id)
                if persisted is None:
                    error = "target expert team does not belong to the owner's organization"
                else:
                    from jiuwenswarm.agents.harness.team.expert_org.launcher import (
                        JiuwenExpertTeamLauncher,
                    )

                    db, organization = persisted
                    owner_org_id = organization.organization_id
                    target_backend = await JiuwenExpertTeamLauncher().restore(
                        team_id=team_id, session_id=session_id, organization=organization,
                        db=db, channel_id=channel_id,
                    )
                    target_org_id = str(
                        getattr(getattr(target_backend, "org_task_manager", None), "organization_id", None) or ""
                    )
            except Exception as exc:
                logger.exception(
                    "[AgentWebSocketServer] expert recovery failed: session=%s team=%s",
                    session_id,
                    team_id,
                )
                error = f"target expert team could not be restored: {exc}"
                error_code = str(getattr(exc, "code", ""))
        if not error and (not session_id or target_backend is None):
            error = "target expert team is not bound to this session"
        elif not error and (not owner_org_id or target_org_id != owner_org_id):
            error = "target expert team does not belong to the owner's organization"
        precheck_failed = bool(error)
        succeeded = False
        if not error:
            from jiuwenswarm.agents.harness.common.rails.permissions.root_context import (
                HOST_USER_ORIGIN_EXTERNAL,
                HOST_USER_ORIGIN_INTERNAL,
            )
            from jiuwenswarm.agents.harness.team.expert_org.launcher import (
                JiuwenExpertTeamLauncher,
            )
            from jiuwenswarm.server.runtime.agent_adapter.interface import (
                is_external_user_authored_dispatch,
            )

            query = _request_query_text(request)
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
                    **{
                        key: params[key]
                        for key in ("media_items", "files")
                        if key in params
                    },
                },
                source="org_expert_direct",
                channel_id=channel_id,
                request_id=request.request_id,
                origin_kind=(
                    HOST_USER_ORIGIN_EXTERNAL
                    if is_external_user_authored_dispatch(
                        params, channel_id=channel_id, request_method=request.req_method, metadata=request.metadata,
                    )
                    else HOST_USER_ORIGIN_INTERNAL
                ),
            )
            if not succeeded:
                error = "target expert team is unavailable or busy"
        if error:
            if precheck_failed:
                logger.warning(
                    "[AgentWebSocketServer] targeted expert chat denied session=%s owner_team=%s target_team=%s owner_org=%s target_org=%s error=%s",
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
                **({"code": error_code} if error_code else {}),
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

    async def _handle_team_snapshot(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        host = self.host
        from jiuwenswarm.agents.harness.team import get_team_manager
        from jiuwenswarm.agents.harness.team.handlers.team_monitor_handler import (
            TeamMonitorHandler,
        )

        params = request.params if isinstance(request.params, dict) else {}
        session_id = str(params.get("session_id") or request.session_id or "").strip()
        channel_id = request.channel_id or "web"
        empty_payload = {"members": [], "tasks": [], "team_id": None}
        requested_team_name = str(params.get("team_name") or "").strip()
        team_manager = get_team_manager(channel_id)
        monitor_handler = (
            team_manager.get_monitor_handler(session_id) if session_id else None
        )
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
            except Exception as e:
                logger.warning(
                    "[AgentWebSocketServer] team.snapshot (live) failed: %s", e
                )

        def _snapshot_tasks(payload: dict[str, Any] | None) -> list[Any]:
            if not isinstance(payload, dict):
                return []
            tasks = payload.get("tasks")
            return tasks if isinstance(tasks, list) else []

        if snapshot is None and requested_team_name and session_id:
            try:
                from openjiuwen.core.runner.runner import GLOBAL_RUNNER

                scoped_monitor = await GLOBAL_RUNNER.get_agent_team_monitor(
                    team_name=requested_team_name, session_id=session_id
                )
                if scoped_monitor is not None:
                    scoped_snapshot = await TeamMonitorHandler(
                        scoped_monitor, session_id
                    ).get_team_snapshot()
                    if scoped_snapshot is not None:
                        snapshot = scoped_snapshot
                        source = "live-scoped"
            except Exception as e:
                logger.warning(
                    "[AgentWebSocketServer] team.snapshot (scoped live) failed: session_id=%s team_name=%s error=%s",
                    session_id,
                    requested_team_name,
                    e,
                )
        needs_db = snapshot is None or not _snapshot_tasks(snapshot)
        if needs_db and session_id:
            team_name = requested_team_name
            if not team_name:
                team_name = str(
                    team_manager.get_active_team_name(session_id) or ""
                ).strip()
            if not team_name:
                team_name = str(
                    (get_session_metadata(session_id) or {}).get("team_name") or ""
                ).strip()
            if team_name:
                try:
                    db_snapshot = await TeamMonitorHandler.get_team_snapshot_from_db(
                        session_id, team_name
                    )
                except Exception as e:
                    logger.warning(
                        "[AgentWebSocketServer] team.snapshot (db) failed: session_id=%s team_name=%s error=%s",
                        session_id,
                        team_name,
                        e,
                    )
                    db_snapshot = None
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
            "[AgentWebSocketServer] team.snapshot session_id=%s source=%s tasks_count=%s members_count=%s",
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
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _team_list_entries(
        self, session_id: str, channel_id: str
    ) -> list[dict[str, Any]]:
        """Collect every team that currently belongs to ``session_id``.
    
        Two sources are merged because a session can hold several teams, and
        they do not live in the same registry:
    
        * the Runner-owned ``TeamRuntimePool`` — the multi-team object pool,
          keyed by ``team_name``, entries bound to ``current_session_id``;
        * ``TeamManager``'s per-session markers (``_active_team_names`` /
          ``_pending_team_names``) plus session metadata ``team_name``, which
          cover sessions whose runtime has not been pooled yet (or is already
          torn down but still resumable).
    
        Later sources only fill gaps, so a pooled entry keeps its live state.
        """
        host = self.host
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
        except Exception as e:
            logger.warning("[AgentWebSocketServer] team.list pool lookup failed: %s", e)
        if runner_pool is not None and session_id:
            try:
                for active in await runner_pool.teams_for_session(session_id):
                    backend = getattr(active.agent, "team_backend", None)
                    if backend is not None and (
                        not getattr(backend, "is_leader", True)
                    ):
                        continue
                    spec_metadata = dict(
                        getattr(getattr(active.agent, "spec", None), "metadata", None)
                        or {}
                    )
                    organization_id = getattr(
                        getattr(backend, "org_task_manager", None),
                        "organization_id",
                        None,
                    )
                    _upsert(
                        active.team_name,
                        state=getattr(getattr(active, "state", None), "value", None)
                        or "running",
                        display_name=getattr(
                            getattr(active.agent, "card", None), "name", None
                        ),
                        leader_id=getattr(backend, "leader_member_name", None),
                        organization_id=organization_id,
                        agent_group_name=spec_metadata.get("agent_group_name"),
                        team_group_identity=spec_metadata.get("team_group_identity"),
                        source="pool",
                    )
            except Exception as e:
                logger.warning(
                    "[AgentWebSocketServer] team.list pool read failed: session_id=%s error=%s",
                    session_id,
                    e,
                )
        team_manager = get_team_manager(channel_id)
        metadata_team_name = ""
        live_team_name = ""
        if session_id:
            session_info = team_manager.get_runtime_team_snapshot().get(session_id)
            if session_info:
                live_team_name = str(session_info.get("team_name") or "").strip()
                _upsert(
                    live_team_name,
                    state="running" if session_info.get("state") == "active" else "pending",
                    source="manager",
                )
            active_name = team_manager.get_active_team_name(session_id)
            if active_name:
                _upsert(active_name, state="running", source="manager")
            pending_name = (
                vars(team_manager).get("_pending_team_names", {}).get(session_id)
            )
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

        from jiuwenswarm.server.runtime.team_organization_history import (
            organization_enabled,
        )

        try:
            persisted = await host._load_persisted_organization(session_id) if organization_enabled() else None
        except Exception:
            persisted = None
            logger.exception("[AgentWebSocketServer] historical team.list failed: session_id=%s", session_id)

        if persisted is not None:
            db, organization = persisted
            # Membership is authoritative; optional labels must never remove a Team.
            for leader in organization.leaders:
                _upsert(
                    leader.team_id,
                    state="configured",
                    leader_id=leader.leader_id,
                    organization_id=organization.organization_id,
                    capabilities=list(leader.capabilities or []),
                    source="database",
                )
            try:
                from jiuwenswarm.server.runtime.team_organization_identity_store import (
                    load_organization_team_identity,
                )
            except ImportError:
                load_organization_team_identity = None
                logger.warning("[AgentWebSocketServer] historical Team identity loader unavailable", exc_info=True)

            for leader in organization.leaders:
                team = None
                identity = {}
                try:
                    team = await db.team.get_team(leader.team_id)
                except Exception:
                    logger.warning(
                        "[AgentWebSocketServer] historical Team details unavailable: session=%s team=%s",
                        session_id, leader.team_id, exc_info=True,
                    )
                try:
                    if load_organization_team_identity is not None:
                        identity = load_organization_team_identity(leader.team_id)
                except Exception:
                    logger.warning(
                        "[AgentWebSocketServer] historical Team identity unavailable: session=%s team=%s",
                        session_id, leader.team_id, exc_info=True,
                    )
                try:
                    _upsert(
                        leader.team_id,
                        state="configured",
                        display_name=getattr(team, "display_name", None) or identity.get("display_name"),
                        agent_group_name=identity.get("agent_group_name"),
                    )
                except Exception:
                    logger.warning(
                        "[AgentWebSocketServer] historical Team labels unavailable: session=%s team=%s",
                        session_id, leader.team_id, exc_info=True,
                    )

        # ``team_name`` reaches session metadata only when the team runtime
        # becomes ready (sync_team_identity_metadata). During the first-turn
        # assembly window the session's own team is already listed via the
        # TeamManager markers above; without the live-binding fallback every
        # team would answer is_owner=false and the Web conversation view would
        # flip to the empty team panel until the next poll. Metadata stays
        # authoritative once it has been persisted.
        owner_team_name = metadata_team_name or live_team_name
        for entry in entries.values():
            entry["is_owner"] = bool(
                owner_team_name and entry["team_id"] == owner_team_name
            )
            if not entry.get("organization_id"):
                org_id = host._lookup_session_org_binding(session_id, entry["team_id"])
                if org_id:
                    entry["organization_id"] = org_id

        if organization_enabled():
            from jiuwenswarm.server.runtime.organization_packages import (
                team_package_availability,
            )
            from jiuwenswarm.server.runtime.organization_team_models import load_team_model

            for entry in entries.values():
                if entry.get("organization_id") or entry.get("source") == "database":
                    entry.update(team_package_availability(entry["team_id"], entry.get("agent_group_name") or ""))
                if not entry.get("organization_id") or entry.get("is_owner"):
                    continue
                try:
                    selection = load_team_model(entry["team_id"], session_id)
                    if selection is not None:
                        entry["execution_model_name"] = selection.model_name
                        entry["execution_model_selection"] = selection.selection.model_dump(exclude_none=True)
                except (ValueError, OSError):
                    logger.warning("Cannot read expert Team model for %s", entry["team_id"], exc_info=True)
        return list(entries.values())

    @staticmethod
    def _lookup_session_org_binding(session_id: str, team_id: str) -> str | None:
        """Best-effort organization id for a team, read off its live backend."""
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
            if (
                active is None
                or getattr(active, "current_session_id", None) != session_id
            ):
                return None
            backend = getattr(active.agent, "team_backend", None)
            organization_id = getattr(
                getattr(backend, "org_task_manager", None), "organization_id", None
            )
            return str(organization_id) if organization_id else None
        except Exception:
            return None

    async def _handle_team_list(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """List the teams belonging to one session, for the team selector."""
        host = self.host
        params = request.params if isinstance(request.params, dict) else {}
        session_id = str(params.get("session_id") or request.session_id or "").strip()
        channel_id = request.channel_id or "web"
        teams: list[dict[str, Any]] = []
        try:
            if session_id:
                teams = await host._team_list_entries(session_id, channel_id)
        except Exception as e:
            logger.warning(
                "[AgentWebSocketServer] team.list failed: session_id=%s error=%s",
                session_id,
                e,
            )
        default_team_id = None
        if teams:
            default_team_id = next(
                (t["team_id"] for t in teams if t.get("is_owner")),
                next(
                    (t["team_id"] for t in teams if t.get("state") == "running"),
                    teams[0]["team_id"],
                ),
            )
        logger.info(
            "[AgentWebSocketServer] team.list session_id=%s teams_count=%s",
            session_id or "-",
            len(teams),
        )
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=channel_id,
            ok=True,
            payload={
                "session_id": session_id, "teams": teams, "default_team_id": default_team_id,
                "expert_requests": host._expert_request_snapshot(session_id),
                "organization_requests": host._organization_request_snapshot(session_id),
                "organization_owner_stopped": (get_session_metadata(session_id) or {}).get(
                    "organization_owner_stopped", False,
                ),
            },
        )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_org_snapshot(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Return organization + task-pool state for the right-hand info box.

        Resolve the live Team binding first, then durable membership anchored
        to the session's owner. A caller-supplied ``organization_id`` is only
        a consistency check and never selects the organization to query.
        """
        host = self.host
        from jiuwenswarm.server.runtime.team_organization_history import (
            organization_enabled,
        )

        enabled = organization_enabled()
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

        backend = host._resolve_team_backend(session_id, team_id) if enabled else None
        db = getattr(backend, "db", None)
        organization_id = str(
            getattr(getattr(backend, "org_task_manager", None), "organization_id", None)
            or ""
        ).strip()
        if enabled and not organization_id and team_id:
            try:
                persisted = await host._load_persisted_organization(session_id, team_id)
                if persisted is not None:
                    db, organization = persisted
                    organization_id = organization.organization_id
            except Exception:
                logger.exception("[AgentWebSocketServer] historical org.snapshot failed: session_id=%s", session_id)
        if db is None or not organization_id:
            payload: dict[str, Any] = dict(empty_payload)
        elif requested_organization_id and requested_organization_id != organization_id:
            logger.warning(
                "[AgentWebSocketServer] org.snapshot organization mismatch: session_id=%s team_id=%s requested=%s bound=%s",
                session_id or "-",
                team_id or "-",
                requested_organization_id,
                organization_id,
            )
            payload = dict(empty_payload)
        else:
            payload = await host._build_org_snapshot(
                session_id=session_id,
                team_id=team_id,
                organization_id=organization_id,
                backend=backend,
                db=db,
            )
        logger.info(
            "[AgentWebSocketServer] org.snapshot session_id=%s team_id=%s organization_id=%s tasks=%s",
            session_id or "-",
            team_id or "-",
            organization_id or "-",
            len(payload.get("tasks") or []),
        )
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=channel_id,
            ok=True,
            payload=payload,
        )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _build_org_snapshot(
        self,
        *,
        session_id: str,
        team_id: str,
        organization_id: str,
        backend: Any,
        db: Any = None,
    ) -> dict[str, Any]:
        """Read durable organization state without changing runtime bindings."""
        host = self.host
        from openjiuwen.agent_teams.organization.manager import TeamOrganizationManager

        empty_payload: dict[str, Any] = {
            "organization": None,
            "tasks": [],
            "unclaimed_tasks": [],
            "pending_reviews": [],
            "stats": {},
        }

        if db is None:
            db = getattr(backend, "db", None)
        if db is None:
            logger.warning(
                "[AgentWebSocketServer] org.snapshot has no db handle: session_id=%s team_id=%s",
                session_id or "-",
                team_id or "-",
            )
            return empty_payload

        # A historical query may precede Team activation. Never cache its
        # transport-less reader as the manager used for runtime coordination.
        manager = TeamOrganizationManager(
            organization_id=organization_id,
            db=db,
            session_id=session_id or None,
        )
        task_pool = manager.task_pool
        organization = await manager.get_organization()
        if organization is None:
            return empty_payload
        tasks = await task_pool.list_tasks(limit=200)
        unclaimed = [t for t in tasks if t.unclaimed is not None]
        open_tasks = [t for t in tasks if str(t.status) == "OPEN"]
        pending_reviews: list[dict[str, Any]] = []
        if team_id:
            try:
                pending_reviews = await task_pool.list_pending_reviews(
                    team_id=team_id, limit=50
                )
            except Exception as e:
                logger.warning(
                    "[AgentWebSocketServer] org.snapshot pending reviews failed: organization_id=%s team_id=%s error=%s",
                    organization_id,
                    team_id,
                    e,
                )
        status_counts: dict[str, int] = {}
        for task in tasks:
            key = str(task.status)
            status_counts[key] = status_counts.get(key, 0) + 1

        from jiuwenswarm.server.runtime.organization_packages import (
            team_package_availability,
        )

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
                        **team_package_availability(leader.team_id),
                    }
                    for leader in organization.leaders
                ],
                "metadata": dict(organization.metadata or {}),
                "unclaimed_task_policy": organization.unclaimed_task_policy.model_dump(),
            },
            "tasks": [
                task.brief() | {"description": (task.description or "")[:280]}
                for task in tasks
            ],
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

    @staticmethod
    def _resolve_team_backend(session_id: str, team_id: str) -> Any | None:
        """Return only the backend matching this exact session and Team."""
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
            if (
                active is None
                or getattr(active, "current_session_id", None) != session_id
            ):
                return None
            return getattr(active.agent, "team_backend", None)
        except Exception as e:
            logger.warning(
                "[AgentWebSocketServer] resolve team backend failed: session_id=%s team_id=%s error=%s",
                session_id,
                team_id,
                e,
            )
        return None

    async def _handle_team_mq_publish(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Relay one external team event into the active core team runtime."""
        host = self.host
        from jiuwenswarm.agents.harness.team import get_team_manager

        session_id = request.session_id or ""
        channel_id = request.channel_id or "web"
        payload = request.params.get("payload")
        if not session_id:
            success, reason = (False, "session_id is required")
        elif payload is None:
            success, reason = (False, "payload is required")
        elif (
            not isinstance(payload, dict)
            or payload.get("type") != "team.external_event"
        ):
            success, reason = (False, "invalid_external_event")
        else:
            success, reason = await get_team_manager(channel_id).interact(
                session_id, payload
            )
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=channel_id,
            ok=success,
            payload={"published": True} if success else {"error": reason},
        )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_team_members_get(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """返回 team human_agent 席位列表供 /join 校验。

        纯查询透传：mismatch 校验与对外文案均在 gateway，server 只查 member、过滤
        human_agent、回 ok/members。查不到或异常 → ok=False（payload 不带文案，由
        gateway 拼"team 不存在"）。
        """
        host = self.host
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
                session_id,
                team_name,
            )
            members_raw = []
        members = [
            m
            for m in members_raw
            if isinstance(m, dict)
            and m.get("role") == "human_agent"
            and m.get("member_id")
        ]
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=channel_id,
            ok=bool(members),
            payload={"members": members},
        )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _cancel_organization(self, request: AgentRequest) -> AgentResponse:
        """Stop the Owner alone or, for legacy callers, its entire organization."""
        host = self.host
        from dataclasses import replace

        from openjiuwen.core.runner.runner import GLOBAL_RUNNER

        from jiuwenswarm.server.runtime.organization_execution import stopping_teams
        from jiuwenswarm.server.runtime.team_organization_history import (
            organization_enabled,
        )

        params = request.params if isinstance(request.params, dict) else {}
        session_id = str(request.session_id or "").strip()
        owner_id = str((get_session_metadata(session_id) or {}).get("team_name") or "").strip()
        target_id = str(params.get("target_team_id") or "").strip()
        owner = host._resolve_team_backend(session_id, owner_id)
        organization_id = getattr(getattr(owner, "org_task_manager", None), "organization_id", None)
        is_owner_target = bool(owner_id and target_id == owner_id)
        is_team_request = is_team_mode(params.get("mode"))
        recovery_error = ""
        if not organization_id and is_owner_target and is_team_request:
            try:
                persisted = await host._load_persisted_organization(session_id, owner_id)
                if persisted is not None:
                    organization_id = persisted[1].organization_id
            except Exception as exc:
                recovery_error = str(exc)
                logger.warning("[AgentWebSocketServer] organization stop membership lookup failed: %s", exc)
        can_cancel_organization = organization_enabled() and is_team_request
        has_owner_binding = is_owner_target and bool(organization_id)
        intent = str(params.get("intent") or "cancel")
        owner_only = params.get("organization_owner_stop") is True
        success = can_cancel_organization and has_owner_binding and intent == "cancel"
        cancelled = []
        owner_was_stopped = False
        inbox_was_enabled = None
        if success:
            if owner_only:
                from jiuwenswarm.server.runtime.organization_execution import owner_is_stopped, set_owner_stopped

                owner_was_stopped = owner_is_stopped(session_id)
                from jiuwenswarm.server.runtime.organization_owner_inbox import owner_inbox

                store = owner_inbox()
                inbox_was_enabled = await asyncio.to_thread(store.enabled_at, session_id, owner_id)
                await asyncio.to_thread(store.enable, session_id, owner_id)
                set_owner_stopped(session_id, True)
            runtime = getattr(GLOBAL_RUNNER, "_team_runtime_manager", None)
            team_ids = {owner_id}
            if runtime is not None and not owner_only:
                for entry in await runtime.pool.teams_for_session(session_id):
                    backend = getattr(entry.agent, "team_backend", None)
                    org_id = getattr(getattr(backend, "org_task_manager", None), "organization_id", None)
                    if org_id == organization_id and getattr(backend, "is_leader", True):
                        team_ids.add(entry.team_name)
            async with stopping_teams(session_id, team_ids) as cancelled:
                owner_params = {
                    key: value for key, value in params.items()
                    if key not in {"target_team_id", "organization_stop", "organization_owner_stop"}
                }
                owner_params["intent"] = "cancel"
                owner_request = replace(request, params=owner_params)
                try:
                    owner_response = await host._execution_runtime().cancel_request(owner_request, allow_create=False)
                    success = bool(success and owner_response.ok)
                    if not success:
                        recovery_error = "Owner execution could not be cancelled"
                except Exception as exc:
                    success = False
                    recovery_error = str(exc) or "Owner execution cancellation failed"
                    logger.warning("[AgentWebSocketServer] organization Owner cancellation failed: %s", exc)
                finally:
                    for team_id in team_ids:
                        try:
                            await host._stop_organization_team(session_id, team_id)
                        except Exception as exc:
                            if not owner_only:
                                raise
                            success = False
                            recovery_error = str(exc) or "Owner runtime could not be stopped"
                            logger.warning("[AgentWebSocketServer] organization Owner stop failed: %s", exc)
            if owner_only and not success:
                set_owner_stopped(session_id, owner_was_stopped)
                if inbox_was_enabled is None:
                    await asyncio.to_thread(store.disable, session_id, owner_id)
        return AgentResponse(
            request_id=request.request_id, channel_id=request.channel_id, ok=success,
            payload={
                "event_type": "chat.interrupt_result", "session_id": session_id,
                "team_id": owner_id, "source": "org_owner_stop" if owner_only else "org_stop", "intent": "cancel",
                "success": success, "has_active_task": False, "cancelled_requests": cancelled,
                **({"organization_owner_stopped": True} if owner_only and success else {}),
                **(
                    {"error": recovery_error or "organization stop requires its Owner in Team mode"}
                    if not success else {}
                ),
            },
        )

    def _expert_request_snapshot(self, session_id: str) -> list[dict[str, str]]:
        host = self.host
        return [
            {"team_id": str(request.params["target_team_id"]), "request_id": request.request_id}
            for task, request in getattr(host, "_expert_stream_requests", {}).items()
            if not task.done() and request.session_id == session_id
        ]

    @staticmethod
    def _is_targeted_expert_cancel_request(request: AgentRequest) -> bool:
        """CHAT_CANCEL 专用：owner 未知时也按「定向」处理，不降级为会话级取消。

        与 `_is_targeted_expert_request` 的唯一差异是**兜底方向**：那边要求 owner 已知
        且与被取消的 team 不同（用于 chat.send 路由，owner 未知就按普通会话走），这边
        owner 未知时保守判为定向——交给 `_cancel_targeted_expert` 明确报错，而不是回落
        `cancel_request(...)` 的会话级取消，把 owner 团队正在跑的 turn 一起取消
        （违反同一次提交里 `_cancel_targeted_expert` 文档写明的 "never the owner turn"）。
        """
        if request.req_method != ReqMethod.CHAT_CANCEL:
            return False
        pair = TeamsManagement._team_target_and_owner(request)
        if pair is None:
            return False
        team_id, owner_team_id = pair
        return not owner_team_id or team_id != owner_team_id

    @staticmethod
    def _is_targeted_expert_request(request: AgentRequest) -> bool:
        """Recognize a Team target only when the durable owner is known."""
        if request.req_method not in (ReqMethod.CHAT_SEND, ReqMethod.CHAT_CANCEL):
            return False
        pair = TeamsManagement._team_target_and_owner(request)
        if pair is None:
            return False
        team_id, owner_team_id = pair
        return bool(owner_team_id and team_id != owner_team_id)

    async def _load_persisted_organization(
        self, session_id: str, team_id: str = ""
    ) -> tuple[Any, Any] | None:
        host = self.host
        from jiuwenswarm.server.runtime.team_organization_history import (
            load_persisted_organization,
            organization_enabled,
        )

        if not organization_enabled():
            return None

        metadata = (get_session_metadata(session_id) or {}) if session_id else {}
        owner_team_id = str(metadata.get("team_name") or "").strip()
        owner_backend = host._resolve_team_backend(session_id, owner_team_id)
        return await load_persisted_organization(
            session_id, team_id, db=getattr(owner_backend, "db", None)
        )

    @staticmethod
    def _organization_request_snapshot(session_id: str) -> list[dict[str, str]]:
        from jiuwenswarm.server.runtime.organization_execution import active_turns

        return active_turns(session_id)

    async def _stop_expert_streams(self) -> None:
        host = self.host
        requests = getattr(host, "_expert_stream_requests", {})
        tasks = [task for task in requests if not task.done() and task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        requests.clear()

    @staticmethod
    async def _stop_organization_team(session_id: str, team_id: str) -> None:
        from openjiuwen.core.runner.runner import GLOBAL_RUNNER

        runtime = getattr(GLOBAL_RUNNER, "_team_runtime_manager", None)
        if runtime is not None:
            # stop_team releases the organization's queued wakes and workers,
            # then stops coordination. Durable team/organization data survives.
            await runtime.stop_team(team_name=team_id, session_id=session_id)

    @staticmethod
    def _team_target_and_owner(request: AgentRequest) -> tuple[str, str] | None:
        """取 ``(target_team_id, owner_team_id)``；非 team 模式或没给 target 时返回 None。

        下面两个判定的公共部分：只说「这个请求有没有形成一个合法的 team 目标」，
        至于 owner 该不该参与比较、怎么兜底，由调用方按各自语义决定。
        """
        params = request.params if isinstance(request.params, dict) else {}
        if not is_team_mode(params.get("mode")):
            return None
        team_id = str(params.get("target_team_id") or "").strip()
        if not team_id:
            return None
        session_id = str(request.session_id or params.get("session_id") or "").strip()
        owner_team_id = str(
            (get_session_metadata(session_id) or {}).get("team_name") or ""
        ).strip()
        return team_id, owner_team_id









