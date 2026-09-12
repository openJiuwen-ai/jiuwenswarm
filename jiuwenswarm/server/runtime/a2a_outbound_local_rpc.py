# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""In-process A2A outbound RPC answering for AgentServer-only sidecars.

``app_gateway`` owns the real ``A2AManager`` that answers the Agent tools'
reverse-RPC requests (``a2a.outbound.tool.*``). The relay-claw packaged
topology spawns only ``app_agentserver`` — its WS client (the Node relay
gateway) has no A2A bridge — so every ``a2a_find_agents`` request sat in the
pending table until the outer tool deadline fired (OA.05000090 watchdog).

This module wires a minimal local answerer instead:

- ``find_agents`` / ``get_dispatch`` run against the same persisted
  ``A2AOutboundRepository`` the Gateway uses (empty catalog is a valid
  answer, not an error);
- ``dispatch_task`` is rejected with ``MANAGER_UNAVAILABLE`` because remote
  A2A calls require the Gateway's connection budget owner.

It activates only when no Gateway-owned ``MessageHandler`` is present in the
process (checked via a module-level flag set by ``app_gateway``); when a real
Gateway exists the flag is absent and the installed hook returns ``False``,
leaving the Gateway path fully in charge.
"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.gateway.a2a_manager.tool_rpc import (
    A2A_TOOL_CANCEL_CALL,
    A2A_TOOL_DISPATCH_TASK,
    A2A_TOOL_FIND_AGENTS,
    A2A_TOOL_GET_DISPATCH,
    A2A_TOOL_METHODS,
)

logger = logging.getLogger(__name__)

#: Set to True by ``app_gateway`` at startup: a real Gateway manager exists in
#: (or is reachable from) the answering path, so the local fallback must defer.
_GATEWAY_MANAGER_PRESENT = False


def mark_gateway_manager_present() -> None:
    global _GATEWAY_MANAGER_PRESENT
    _GATEWAY_MANAGER_PRESENT = True


class _LocalA2AManager:
    """Gateway-free answering subset of ``A2AManager``."""

    def __init__(self) -> None:
        self._repository = None
        self._storage_ctx = None
        self._repository_failed = False

    async def _ensure_repository(self):
        if self._repository is not None or self._repository_failed:
            return self._repository
        try:
            from jiuwenswarm.gateway.storage_assembly.setup import (
                create_gateway_storage_context,
            )

            self._storage_ctx = create_gateway_storage_context()
            store = await self._storage_ctx.persistent()
            from jiuwenswarm.gateway.storage_assembly import (
                create_a2a_outbound_repository,
            )

            self._repository = create_a2a_outbound_repository(store)
        except Exception:  # noqa: BLE001 - degraded answer beats a stall
            self._repository_failed = True
            logger.warning(
                "[AgentServer] local A2A outbound repository unavailable; "
                "find_agents will return an empty catalog",
                exc_info=True,
            )
        return self._repository

    async def outbound_find_agents(
        self,
        *,
        query: str = "",
        required_skills: list[str] | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        repository = await self._ensure_repository()
        if repository is None:
            return {"items": [], "total": 0, "total_matches": 0, "matched_total": 0}
        from jiuwenswarm.gateway.a2a_manager.outbound.dispatcher import (
            A2AOutboundDispatcher,
        )

        dispatcher = A2AOutboundDispatcher(repository)
        return await dispatcher.find_agents(
            query=query,
            required_skills=required_skills,
            limit=limit,
        )

    async def outbound_get_dispatch(
        self, *, dispatch_id: str, source_session_id: str
    ) -> dict[str, Any]:
        from jiuwenswarm.gateway.a2a_manager.outbound import (
            A2AOutboundError,
            A2AOutboundErrorCode,
        )

        raise A2AOutboundError(A2AOutboundErrorCode.MANAGER_UNAVAILABLE)

    async def outbound_dispatch_task(
        self,
        *,
        agent_id: str,
        task: str,
        mode: str,
        source_session_id: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        from jiuwenswarm.gateway.a2a_manager.outbound import (
            A2AOutboundError,
            A2AOutboundErrorCode,
        )

        raise A2AOutboundError(A2AOutboundErrorCode.MANAGER_UNAVAILABLE)


_LOCAL_MANAGER: _LocalA2AManager | None = None


def _local_manager() -> _LocalA2AManager:
    global _LOCAL_MANAGER
    if _LOCAL_MANAGER is None:
        _LOCAL_MANAGER = _LocalA2AManager()
    return _LOCAL_MANAGER


async def handle_a2a_outbound_tool_push(
    *, chunk: Any, session_id: str | None
) -> bool:
    """Handle ``a2a.outbound.tool.*`` pushes when no Gateway manager exists.

    Returns ``False`` to defer to the real Gateway handler (or to let the push
    fall through as chat traffic if this is not an A2A RPC at all).
    """
    if _GATEWAY_MANAGER_PRESENT:
        return False

    payload = chunk.payload if isinstance(chunk.payload, dict) else {}
    if str(payload.get("event_type") or "") != "acp.output_request":
        return False
    nested_jsonrpc = payload.get("jsonrpc")
    rpc_payload = (
        dict(nested_jsonrpc)
        if isinstance(nested_jsonrpc, dict)
        else payload
    )
    method = str(rpc_payload.get("method") or "").strip()
    if method not in A2A_TOOL_METHODS:
        return False

    from jiuwenswarm.common.e2a.adapters import build_acp_tool_response_message
    from jiuwenswarm.gateway.a2a_manager.outbound import (
        A2AOutboundError,
        A2AOutboundErrorCode,
        safe_error_summary,
    )

    jsonrpc_id = str(rpc_payload.get("id") or "").strip()
    params = rpc_payload.get("params")
    params = dict(params) if isinstance(params, dict) else {}
    manager: _LocalA2AManager | None = _local_manager()

    if method == A2A_TOOL_CANCEL_CALL:
        # Nothing runs locally without the Gateway dispatcher, so there is no
        # in-flight call to cancel; acknowledge with canceled=False.
        response = {
            "jsonrpc": "2.0",
            "id": jsonrpc_id,
            "result": {"canceled": False},
        }
    else:
        try:
            if not session_id:
                raise A2AOutboundError(A2AOutboundErrorCode.DISPATCH_REJECTED)
            if method == A2A_TOOL_FIND_AGENTS:
                required = params.get("required_skills")
                if required is not None and not isinstance(required, list):
                    raise A2AOutboundError(A2AOutboundErrorCode.TASK_INVALID)
                result = await manager.outbound_find_agents(
                    query=str(params.get("query") or ""),
                    required_skills=required,
                    limit=int(params.get("limit") or 5),
                )
            elif method == A2A_TOOL_DISPATCH_TASK:
                result = await manager.outbound_dispatch_task(
                    agent_id=str(params.get("agent_id") or ""),
                    task=str(params.get("task") or ""),
                    mode=str(params.get("mode") or ""),
                    source_session_id=session_id,
                    reason=str(params.get("reason") or "") or None,
                )
            else:  # A2A_TOOL_GET_DISPATCH
                result = await manager.outbound_get_dispatch(
                    dispatch_id=str(params.get("dispatch_id") or ""),
                    source_session_id=session_id,
                )
            response = {"jsonrpc": "2.0", "id": jsonrpc_id, "result": result}
        except A2AOutboundError as exc:
            response = {
                "jsonrpc": "2.0",
                "id": jsonrpc_id,
                "error": {
                    "code": -32060,
                    "message": exc.summary,
                    "data": {"code": exc.code.value},
                },
            }
        except Exception:
            logger.exception(
                "[AgentServer] local A2A outbound RPC failed: method=%s session_id=%s",
                method,
                session_id,
            )
            code = A2AOutboundErrorCode.MANAGER_UNAVAILABLE
            response = {
                "jsonrpc": "2.0",
                "id": jsonrpc_id,
                "error": {
                    "code": -32061,
                    "message": safe_error_summary(code),
                    "data": {"code": code.value},
                },
            }

    if not jsonrpc_id:
        # Nothing pending to complete; the request frame was malformed.
        return True

    reply_response = dict(response.get("result") or response.get("error") or {})

    reply = build_acp_tool_response_message(
        jsonrpc_id,
        response,
        str(session_id or "") or None,
        channel_id=str(chunk.channel_id or "default"),
    )
    from jiuwenswarm.agents.harness.common.tools.acp_output_tools import (
        get_acp_output_manager,
    )

    get_acp_output_manager().complete_jsonrpc_response(
        jsonrpc_id, reply_response
    )
    return True


__all__ = ["handle_a2a_outbound_tool_push", "mark_gateway_manager_present"]
