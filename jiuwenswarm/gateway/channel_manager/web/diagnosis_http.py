# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway HTTP proxy of the trajectory diagnosis feature.

The diagnosis service runs in the AgentServer Worker. These routes keep the
browser contract (paths, bodies, error shapes) and only gate the origin, bound
the request body, forward the call as a ``diagnosis.*`` E2A request and map the
answer back to HTTP. Routes live under the trajectory prefix so they share its
origin gate and its error boundary middleware; every handler marks
``trajectory_route_handled`` so the middleware does not rewrite the contract
error bodies.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.diagnosis.models import ERROR_STATUS_BY_CODE
from jiuwenswarm.gateway.channel_manager.web.trajectory_http import (
    TRAJECTORY_API_PREFIX,
    validate_http_origin,
)

if TYPE_CHECKING:
    from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel

logger = logging.getLogger(__name__)

DIAGNOSIS_CHANNEL_ID = "web"
_NO_STORE_HEADERS = {"Cache-Control": "no-store"}
_MAX_BODY_BYTES = 64 * 1024
_UNAVAILABLE_CODES = frozenset(
    {
        "SERVICE_UNAVAILABLE",
        "RUNTIME_WARMING",
        "RUNTIME_DRAINING",
        "RUNTIME_FAILED",
        "RUNTIME_QUEUE_FULL",
        "REQUEST_CANCELLED",
    },
)
_TIMEOUT_CODE = "AGENT_SERVER_TIMEOUT"

DiagnosisFetch = Callable[[ReqMethod, str, dict[str, Any]], Awaitable[tuple[bool, dict[str, Any]]]]


class _BadRequest(Exception):
    """Request rejected at the Gateway before it is forwarded."""


def _json(content: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=content, status_code=status_code, headers=_NO_STORE_HEADERS)


def _error(error: str, code: str, status_code: int) -> JSONResponse:
    return _json({"error": error, "code": code}, status_code)


def error_status(code: str) -> int:
    """Return the HTTP status for an error code answered by the AgentServer."""
    if code in ERROR_STATUS_BY_CODE:
        return ERROR_STATUS_BY_CODE[code]
    if code in _UNAVAILABLE_CODES:
        return 503
    if code == _TIMEOUT_CODE:
        return 504
    return 500


async def _read_json_object(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > _MAX_BODY_BYTES:
        raise _BadRequest("request body is too large")
    try:
        body = json.loads(raw or b"{}")
    except ValueError as exc:
        raise _BadRequest("request body must be JSON") from exc
    if not isinstance(body, dict):
        raise _BadRequest("request body must be a JSON object")
    return body


def build_diagnosis_fetch(channel: WebChannel) -> DiagnosisFetch:
    """Return the callable that sends one ``diagnosis.*`` request to the AgentServer."""

    async def fetch(method: ReqMethod, session_id: str, params: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        from jiuwenswarm.gateway.routing.e2a_proxy import fetch_agent_unary

        return await fetch_agent_unary(
            agent_client=getattr(channel, "diagnosis_agent_client", None),
            req_method=method,
            params={"session_id": session_id, **params},
            session_id=session_id,
            user_id=None,
            channel_id=DIAGNOSIS_CHANNEL_ID,
            label="diagnosis",
        )

    return fetch


def attach_diagnosis_routes(
    app: FastAPI,
    channel: WebChannel | None,
    *,
    fetch: DiagnosisFetch | None = None,
) -> None:
    """Mount the diagnosis proxy routes on the WebChannel FastAPI app.

    Args:
        app: WebChannel FastAPI application.
        channel: Owning WebChannel; may be ``None`` only when ``fetch`` is given.
        fetch: Optional AgentServer caller, primarily for tests.
    """
    if fetch is None:
        if channel is None:
            raise ValueError("channel is required when no fetch callable is supplied")
        fetch = build_diagnosis_fetch(channel)
    forward = fetch
    base = f"{TRAJECTORY_API_PREFIX}/sessions/{{session_id}}/diagnoses"

    def gate(request: Request) -> Response | None:
        request.state.trajectory_route_handled = True
        return validate_http_origin(request)

    async def proxy(
        request: Request,
        method: ReqMethod,
        session_id: str,
        *,
        diagnosis_id: str | None = None,
        with_body: bool = False,
        success_status: int = 200,
    ) -> Response:
        origin_error = gate(request)
        if origin_error is not None:
            return origin_error
        params: dict[str, Any] = {}
        if diagnosis_id is not None:
            params["diagnosis_id"] = diagnosis_id
        try:
            if with_body:
                params["request"] = await _read_json_object(request)
            ok, payload = await forward(method, session_id, params)
        except _BadRequest as exc:
            return _error(str(exc), "BAD_REQUEST", 400)
        except Exception:
            logger.exception("[diagnosis] proxy failed: method=%s session_id=%s", method.value, session_id)
            return _error("diagnosis request failed", "INTERNAL_ERROR", 500)
        if not ok:
            code = str(payload.get("code") or "INTERNAL_ERROR")
            return _error(str(payload.get("error") or "diagnosis request failed"), code, error_status(code))
        if success_status == 204:
            return Response(status_code=204, headers=_NO_STORE_HEADERS)
        return _json(payload, success_status)

    @app.post(base)
    async def run_diagnosis(session_id: str, request: Request) -> Response:
        """Fix the scope and start one diagnosis."""
        return await proxy(request, ReqMethod.DIAGNOSIS_RUN, session_id, with_body=True, success_status=202)

    @app.get(base)
    async def list_diagnoses(session_id: str, request: Request) -> Response:
        """List diagnosis summaries, newest first."""
        return await proxy(request, ReqMethod.DIAGNOSIS_LIST, session_id)

    @app.get(f"{base}/{{diagnosis_id}}")
    async def get_diagnosis(session_id: str, diagnosis_id: str, request: Request) -> Response:
        """Return one record, live when it is running."""
        return await proxy(request, ReqMethod.DIAGNOSIS_GET, session_id, diagnosis_id=diagnosis_id)

    @app.post(f"{base}/{{diagnosis_id}}/cancel")
    async def cancel_diagnosis(session_id: str, diagnosis_id: str, request: Request) -> Response:
        """Cancel a running diagnosis."""
        return await proxy(request, ReqMethod.DIAGNOSIS_CANCEL, session_id, diagnosis_id=diagnosis_id)

    @app.delete(f"{base}/{{diagnosis_id}}")
    async def delete_diagnosis(session_id: str, diagnosis_id: str, request: Request) -> Response:
        """Delete a finished diagnosis record."""
        return await proxy(
            request,
            ReqMethod.DIAGNOSIS_DELETE,
            session_id,
            diagnosis_id=diagnosis_id,
            success_status=204,
        )

    @app.post(f"{base}/{{diagnosis_id}}/ttse")
    async def trigger_ttse(session_id: str, diagnosis_id: str, request: Request) -> Response:
        """Trigger TTSE experience generation with the selected experience root causes."""
        return await proxy(
            request,
            ReqMethod.DIAGNOSIS_TTSE_TRIGGER,
            session_id,
            diagnosis_id=diagnosis_id,
            with_body=True,
        )


__all__ = [
    "attach_diagnosis_routes",
    "build_diagnosis_fetch",
    "error_status",
]
