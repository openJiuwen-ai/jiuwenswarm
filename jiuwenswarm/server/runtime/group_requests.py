"""Thin WebSocket adapters for group management and group chat routing."""

from __future__ import annotations

import logging

from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.runtime.session_provisioner import (
    SessionProvisionCommitContext,
    SessionProvisionCommitTiming,
    SessionProvisionState,
)
from jiuwenswarm.server.runtime.group_service import (
    GROUP_METHODS,
    GroupError,
    group_path,
    read_group,
)
from jiuwenswarm.server.runtime.session.lifecycle import LifecycleError
from gateway.common.protocol_ids import validate_session_id
from gateway.common.mode_matrix import is_team_mode

logger = logging.getLogger(__name__)


async def dispatch_group_request(server, ws, request, send_lock) -> bool:
    from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire
    from jiuwenswarm.server.ws_send import send_wire_payload

    method = request.req_method.value if request.req_method else ""
    params = request.params if isinstance(request.params, dict) else {}
    user_id, channel_id = (
        str(getattr(request, "user_id", "") or ""),
        request.channel_id or "default",
    )
    if method not in GROUP_METHODS and method not in {"chat.send", "chat.resume"}:
        return False
    if method in {"chat.send", "chat.resume"} and not params.get("group_id"):
        if not request.session_id or read_group(request.session_id) is None:
            return False
    service = server.get_group_service()

    async def reply(payload, *, ok=True):
        response = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=ok,
            payload=payload,
            metadata=request.metadata,
        )
        async with send_lock:
            return await send_wire_payload(
                ws,
                encode_agent_response_for_wire(
                    response, response_id=request.request_id
                ),
            )

    prepared = None
    created = False
    response_delivered = False
    group_id = None
    try:
        if method == "group.create":
            payload, prepared, created = await service.prepare_create(
                params,
                user_id=user_id,
                channel_id=channel_id,
                request_id=request.request_id,
            )
            group_id = payload["group_id"]
            if not await reply(payload):
                raise GroupError(
                    "RESPONSE_TOO_LARGE", "group response could not be delivered"
                )
            response_delivered = True
            await service.runtime.commit_session_provision(
                prepared,
                timing=SessionProvisionCommitTiming.AFTER_RESULT_DELIVERY,
                context=SessionProvisionCommitContext(
                    foreground_scope_id=str(params.get("view_id") or f"ws:{id(ws)}")
                ),
            )
        elif method == "group.list":
            await reply(service.list(params, user_id=user_id, channel_id=channel_id))
        else:
            group_id = validate_session_id(params.get("group_id") or request.session_id)
            if method in {"chat.send", "chat.resume"}:
                if request.session_id and request.session_id != group_id:
                    raise GroupError(
                        "BAD_REQUEST", "group_id and session_id must match"
                    )
                group = service.owned(group_id, user_id, channel_id)
                if params.get("mode") and (
                    not isinstance(params["mode"], str)
                    or not is_team_mode(params["mode"])
                ):
                    raise GroupError("MODE_MISMATCH", "group chat requires team mode")
                if params.get("type") not in {None, "group_chat"}:
                    raise GroupError(
                        "MODE_MISMATCH", "group chat requires group_chat input"
                    )
                request.session_id = group_id
                request.params = {**params, "mode": group["mode"]}
                if method == "chat.send":
                    request.params["type"] = "group_chat"
                request.metadata = {**(request.metadata or {}), "mode": group["mode"]}
                return False
            if method == "group.get":
                payload = service.get(group_id, user_id=user_id, channel_id=channel_id)
            elif method == "group.update":
                payload = await service.update(
                    group_id, params, user_id=user_id, channel_id=channel_id
                )
            elif method == "group.member.add":
                payload = await service.add_member(
                    group_id,
                    params,
                    user_id=user_id,
                    channel_id=channel_id,
                    request_id=request.request_id,
                )
            elif method == "group.history.get":
                payload = await service.history(
                    group_id, params, user_id=user_id, channel_id=channel_id
                )
            else:
                payload = await service.delete(
                    group_id, user_id=user_id, channel_id=channel_id
                )
            await reply(payload)
    except (ValueError, LifecycleError) as exc:
        if not response_delivered:
            await reply(
                {"code": getattr(exc, "code", "BAD_REQUEST"), "error": str(exc)},
                ok=False,
            )
        else:
            logger.exception("group provision failed after result delivery")
    except Exception:
        logger.exception("group request failed: method=%s", method)
        if not response_delivered:
            await reply(
                {"code": "INTERNAL_ERROR", "error": "group request failed"}, ok=False
            )
    finally:
        if prepared is not None and prepared.state is SessionProvisionState.PREPARED:
            try:
                await service.runtime.abort_session_provision(prepared)
            finally:
                if created and group_id is not None:
                    group_path(group_id).unlink(missing_ok=True)
    return True
