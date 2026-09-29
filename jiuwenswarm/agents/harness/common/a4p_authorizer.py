# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Web User Authorizer broker for JiuwenSwarm A4P mandates."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from a4p.types import UserAuthorizationResponse
from a4p.user_authorizer import approve_user_mandate

from jiuwenswarm.agents.harness.common.a4p_execution_context import AuthorizerRoute
from jiuwenswarm.common.utils import logger


@dataclass(frozen=True)
class WebAuthorizationRequest:
    request_id: str
    mandate: dict[str, Any]
    signing_options: dict[str, Any]
    ui_context: dict[str, Any]
    route: AuthorizerRoute
    original_actions: list[dict[str, Any]] = field(default_factory=list)
    prepared_action_indexes: list[int] = field(default_factory=list)
    reprepare: Callable[[list[int]], Awaitable[WebAuthorizationRequest]] | None = None
    replaces_request_id: str | None = None


@dataclass
class _PendingAuthorization:
    future: asyncio.Future[UserAuthorizationResponse]
    request: WebAuthorizationRequest
    repreparing: bool = False


class WebAuthorizerBroker:
    """Own pending Web approvals independently from the A4P protocol runtime."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 300,
        approve_mandate: Callable[
            [dict[str, Any], dict[str, Any] | None],
            dict[str, Any],
        ]
        | None = None,
    ) -> None:
        self.timeout_seconds = float(timeout_seconds)
        self._approve_mandate = approve_mandate or (
            lambda mandate, _assertion: approve_user_mandate(mandate)
        )
        self._pending: dict[str, _PendingAuthorization] = {}
        self._accepting = True

    async def request_authorization(
        self,
        request: WebAuthorizationRequest,
    ) -> UserAuthorizationResponse:
        if not self._accepting:
            return UserAuthorizationResponse(
                approved=False,
                rejectReason="A4P authorizer is disabled",
            )
        session_id = str(request.ui_context.get("sessionId") or "").strip()
        if not session_id:
            return UserAuthorizationResponse(
                approved=False,
                rejectReason="A4P sessionId missing",
            )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[UserAuthorizationResponse] = loop.create_future()
        pending = _PendingAuthorization(
            future=future,
            request=request,
        )
        self._pending[request.request_id] = pending
        try:
            await self._send_push(request, event_type="a4p.authorization_request")
            try:
                return await asyncio.wait_for(future, timeout=self.timeout_seconds)
            except TimeoutError:
                await self._send_terminal_push(
                    pending.request,
                    reason="A4P authorization timed out",
                    code="A4P_AUTHORIZATION_TIMEOUT",
                )
                return UserAuthorizationResponse(
                    approved=False,
                    rejectReason="A4P authorization timed out",
                )
            except asyncio.CancelledError:
                await self._send_terminal_push(
                    pending.request,
                    reason="A4P authorization was cancelled",
                    code="A4P_AUTHORIZATION_CANCELLED",
                )
                raise
        finally:
            if self._pending.get(pending.request.request_id) is pending:
                self._pending.pop(pending.request.request_id, None)

    @staticmethod
    def _route_matches(
        pending: _PendingAuthorization,
        route: AuthorizerRoute,
    ) -> bool:
        return pending.request.route.logical_key == route.logical_key

    def complete(
        self,
        request_id: str,
        route: AuthorizerRoute,
        *,
        assertion: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        pending = self._pending.get(request_id)
        if pending is None:
            return {"ok": False, "error": "A4P authorization request not pending"}
        if not self._route_matches(pending, route):
            return {"ok": False, "error": "A4P authorization route mismatch"}
        if pending.future.done() or pending.repreparing:
            return {"ok": False, "error": "A4P authorization request not pending"}
        try:
            signed_mandate = self._approve_mandate(
                pending.request.mandate,
                assertion,
            )
        except (TypeError, ValueError) as exc:
            code = (
                "A4P_WEBAUTHN_ASSERTION_REQUIRED"
                if "assertion missing" in str(exc).lower()
                else "A4P_WEBAUTHN_ASSERTION_INVALID"
            )
            return {
                "ok": False,
                "error": str(exc),
                "code": code,
            }
        self._pending.pop(request_id, None)
        pending.future.set_result(
            UserAuthorizationResponse(
                approved=True,
                signedMandate=signed_mandate,
            )
        )
        return {"ok": True, "requestId": request_id}

    @staticmethod
    def _snapshot(pending: _PendingAuthorization) -> dict[str, Any]:
        request = pending.request
        return {
            "requestId": request.request_id,
            "kind": str(request.ui_context.get("kind") or "intent"),
            "mandate": deepcopy(request.mandate),
            "signingOptions": deepcopy(request.signing_options),
            "uiContext": deepcopy(request.ui_context),
            "originalActions": deepcopy(request.original_actions),
            "preparedActionIndexes": list(request.prepared_action_indexes),
            "replacesRequestId": request.replaces_request_id,
            "repreparing": pending.repreparing,
        }

    async def reprepare(
        self, request_id: str, route: AuthorizerRoute, indexes: Any,
    ) -> dict[str, Any]:
        pending = self._pending.get(request_id)
        if pending is None or pending.future.done() or pending.repreparing:
            return {"ok": False, "error": "A4P authorization request not pending"}
        if not self._route_matches(pending, route):
            return {"ok": False, "error": "A4P authorization route mismatch"}
        request = pending.request
        if (
            not isinstance(indexes, list) or not indexes
            or any(type(i) is not int or i < 0 or i >= len(request.original_actions) for i in indexes)
            or len(set(indexes)) != len(indexes)
        ):
            return {"ok": False, "error": "Invalid selectedActionIndexes", "code": "A4P_INVALID_SELECTION"}
        indexes = sorted(indexes)
        if indexes == request.prepared_action_indexes:
            return {"ok": True, "pending": self._snapshot(pending)}
        if request.reprepare is None:
            return {"ok": False, "error": "Range modification unavailable"}
        pending.repreparing = True
        try:
            replacement = await request.reprepare(indexes)
            if pending.future.done() or self._pending.get(request_id) is not pending:
                return {"ok": False, "error": "A4P authorization request not pending"}
            self._pending.pop(request_id)
            pending.request = replacement
            self._pending[replacement.request_id] = pending
            await self._send_push(replacement, event_type="a4p.authorization_request")
            pending.repreparing = False
            if pending.future.done():
                return {"ok": False, "error": "A4P authorization request not pending"}
            return {"ok": True, "pending": self._snapshot(pending)}
        except (Exception, asyncio.CancelledError) as exc:
            self._pending.pop(pending.request.request_id, None)
            if not pending.future.done():
                pending.future.set_result(UserAuthorizationResponse(
                    approved=False, rejectReason=f"A4P range modification failed: {exc}",
                ))
            await self._send_terminal_push(
                pending.request, reason="A4P range modification failed",
                code="A4P_REPREPARE_FAILED",
            )
            if pending.request.request_id != request_id:
                await self._send_terminal_push(
                    request, reason="A4P range modification failed",
                    code="A4P_REPREPARE_FAILED",
                )
            if isinstance(exc, asyncio.CancelledError):
                raise
            return {"ok": False, "error": str(exc), "code": "A4P_REPREPARE_FAILED"}

    def reject(
        self,
        request_id: str,
        route: AuthorizerRoute,
        reason: str | None = None,
    ) -> dict[str, Any]:
        pending = self._pending.get(request_id)
        if pending is None:
            return {"ok": False, "error": "A4P authorization request not pending"}
        if not self._route_matches(pending, route):
            return {"ok": False, "error": "A4P authorization route mismatch"}
        if pending.future.done() or pending.repreparing:
            return {"ok": False, "error": "A4P authorization request not pending"}
        self._pending.pop(request_id, None)
        pending.future.set_result(
            UserAuthorizationResponse(
                approved=False,
                rejectReason=reason or "User rejected A4P authorization",
            )
        )
        return {"ok": True, "requestId": request_id}

    def pending_for_route(self, route: AuthorizerRoute) -> dict[str, Any]:
        for pending in reversed(list(self._pending.values())):
            if not self._route_matches(pending, route):
                continue
            return {"ok": True, "pending": self._snapshot(pending)}
        return {"ok": True, "pending": None}

    async def cancel_all(
        self,
        *,
        reason: str,
        code: str,
    ) -> int:
        self._accepting = False
        pending_items = list(self._pending.values())
        self._pending.clear()
        for pending in pending_items:
            if not pending.future.done():
                pending.future.set_result(
                    UserAuthorizationResponse(
                        approved=False,
                        rejectReason=reason,
                    )
                )
            await self._send_terminal_push(
                pending.request,
                reason=reason,
                code=code,
            )
        return len(pending_items)

    async def _send_terminal_push(
        self,
        request: WebAuthorizationRequest,
        *,
        reason: str,
        code: str,
    ) -> None:
        try:
            await self._send_push(
                request,
                event_type="a4p.authorization_terminated",
                terminal={"reason": reason, "code": code},
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "[A4P] failed to push authorization terminal event request=%s",
                request.request_id,
                exc_info=True,
            )

    async def _send_push(
        self,
        request: WebAuthorizationRequest,
        *,
        event_type: str,
        terminal: dict[str, Any] | None = None,
    ) -> None:
        from jiuwenswarm.runtime.host_services import send_runtime_push

        ui_context = dict(request.ui_context)
        route = request.route
        payload: dict[str, Any] = {
            "event_type": event_type,
            "requestId": request.request_id,
            "kind": str(ui_context.get("kind") or "intent"),
            "uiContext": ui_context,
        }
        if request.replaces_request_id:
            payload["replacesRequestId"] = request.replaces_request_id
        if terminal is None:
            payload.update(
                {
                    "mandate": request.mandate,
                    "signingOptions": request.signing_options,
                    "originalActions": deepcopy(request.original_actions),
                    "preparedActionIndexes": list(request.prepared_action_indexes),
                }
            )
        else:
            payload.update(terminal)
        delivered = await send_runtime_push(
            {
                "channel_id": str(ui_context.get("channelId") or "web"),
                "session_id": route.session_id,
                "request_id": request.request_id,
                "agent_ref": {
                    "mode": route.agent_ref_mode,
                    "id": route.agent_ref_id,
                },
                "metadata": {
                    "app_id": route.app_id,
                    "ws_id": route.ws_id,
                },
                "payload": payload,
            }
        )
        if not delivered:
            raise RuntimeError("A4P authorization push could not be delivered")


__all__ = ["WebAuthorizationRequest", "WebAuthorizerBroker"]
