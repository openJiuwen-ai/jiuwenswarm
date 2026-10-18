# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Worker-side host of the trajectory diagnosis service.

The Gateway proxies the browser HTTP API to ``diagnosis.*`` requests. This
module owns the production wiring of :class:`DiagnosisService` inside the
Worker (session metadata, trajectory reader, busy state, push channel and the
resident TTSE pipeline) and turns each request into an :class:`AgentResponse`.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from pydantic import ValidationError

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.diagnosis.engine import DiagnosisEngine
from jiuwenswarm.diagnosis.models import (
    DiagnosisHostError,
    DiagnosisRunRequest,
    TtseTriggerRequest,
)
from jiuwenswarm.diagnosis.service import DiagnosisService
from jiuwenswarm.diagnosis.store import DiagnosisStore

logger = logging.getLogger(__name__)

DIAGNOSIS_CHANNEL_ID = "web"
PushSender = Callable[[dict[str, Any]], Awaitable[bool | None]]


def build_diagnosis_engine() -> DiagnosisEngine:
    """Return the engine the production host runs diagnoses with.

    This is the only place that picks the engine. It runs the agent-core
    attribution pipeline.
    """
    from jiuwenswarm.diagnosis.attribution_engine import AttributionCoreEngine

    return AttributionCoreEngine()


def _first_validation_error(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "invalid request"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    return f"{location}: {first.get('msg', 'invalid value')}".strip(": ")


def is_session_busy(agent_manager: Any, session_id: str) -> bool:
    """Return whether any cached agent is executing work for ``session_id``.

    Args:
        agent_manager: The Worker's ``AgentManager``; ``None`` reports idle.
        session_id: Session to inspect.

    Returns:
        ``True`` when a task is running for the session. Lookup failures are
        logged and reported as idle so a broken probe cannot block diagnosis.
    """
    agents = getattr(agent_manager, "agents", None)
    if not isinstance(agents, Mapping):
        return False
    for channel_agents in list(agents.values()):
        if not isinstance(channel_agents, Mapping):
            continue
        for agent in list(channel_agents.values()):
            try:
                if _agent_session_busy(agent, session_id):
                    return True
            except Exception:  # noqa: BLE001
                logger.warning("[diagnosis] busy probe failed: session_id=%s", session_id, exc_info=True)
    return False


def _agent_session_busy(agent: Any, session_id: str) -> bool:
    has_runtime = getattr(agent, "has_session_runtime", None)
    if not callable(has_runtime) or not has_runtime(session_id):
        return False
    checker = getattr(getattr(agent, "_adapter", None), "is_session_active", None)
    if callable(checker) and checker(session_id):
        return True
    current_task = getattr(getattr(agent, "_session_manager", None), "get_current_task", None)
    task = current_task(session_id) if callable(current_task) else None
    return task is not None and not task.done()


class _TrajectoryReaderProvider:
    """Own a read-only trajectory reader that follows the configured store."""

    def __init__(self) -> None:
        self._reader: Any = None
        self._database_path: Any = None

    def __call__(self) -> Any:
        from jiuwenswarm.observability.config import load_trajectory_store_settings
        from jiuwenswarm.observability.store import AsyncTrajectoryReader

        database_path = load_trajectory_store_settings().database_path
        if self._reader is None or self._database_path != database_path:
            self._reader = AsyncTrajectoryReader(database_path, session_scoped=True)
            self._database_path = database_path
        return self._reader


def _load_session_metadata(session_id: str) -> Mapping[str, Any]:
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

    return get_session_metadata(session_id, cache_bust=True, enable_writeback=False)


class DiagnosisHost:
    """Create the diagnosis service on first use and answer ``diagnosis.*`` requests."""

    def __init__(
        self,
        *,
        get_agent_manager: Callable[[], Any],
        send_push: PushSender,
        service: DiagnosisService | None = None,
    ) -> None:
        """Bind the host to the Worker.

        Args:
            get_agent_manager: Returns the current ``AgentManager``; the server
                replaces it when the Runtime restarts, so it is read per call.
            send_push: Sends one AgentServer-to-Gateway push message.
            service: Optional prebuilt service, primarily for tests.
        """
        self._get_agent_manager = get_agent_manager
        self._send_push = send_push
        self._service = service

    @property
    def service(self) -> DiagnosisService:
        """Return the diagnosis service, building the production one lazily."""
        if self._service is None:
            self._service = self._build_service()
        return self._service

    def _build_service(self) -> DiagnosisService:
        from jiuwenswarm.server.diagnosis.ttse_trigger import trigger_ttse_induction

        async def push(session_id: str, event: str, payload: dict[str, Any]) -> None:
            diagnosis_id = payload.get("diagnosis_id", "")
            delivered = await self._send_push(
                {
                    "request_id": f"diagnosis:{session_id}:{diagnosis_id}:{event}:{payload.get('seq', 0)}",
                    "channel_id": DIAGNOSIS_CHANNEL_ID,
                    "session_id": session_id,
                    "payload": {**payload, "event_type": event},
                    "is_complete": False,
                },
            )
            if delivered is False:
                logger.debug("[diagnosis] push dropped without a Gateway connection: event=%s", event)

        async def trigger_ttse(session_id: str, signals: list[dict[str, Any]]) -> tuple[bool, dict[str, Any]]:
            result = await trigger_ttse_induction(
                agent_manager=self._get_agent_manager(),
                channel_id=DIAGNOSIS_CHANNEL_ID,
                session_id=session_id,
                signals=signals,
            )
            if result.get("ok"):
                return True, dict(result.get("payload") or {})
            return False, {"error": str(result.get("error") or ""), "code": str(result.get("code") or "")}

        return DiagnosisService(
            store=DiagnosisStore(),
            reader_provider=_TrajectoryReaderProvider(),
            push=push,
            is_busy=lambda session_id: is_session_busy(self._get_agent_manager(), session_id),
            metadata_loader=_load_session_metadata,
            ttse_trigger=trigger_ttse,
            engine=build_diagnosis_engine(),
        )

    async def handle(self, request: AgentRequest) -> AgentResponse:
        """Answer one ``diagnosis.*`` request.

        Args:
            request: Request whose ``params`` carry ``session_id`` and, per
                method, ``diagnosis_id`` and a ``request`` body.

        Returns:
            A successful response whose payload mirrors the HTTP body, or an
            error response whose payload is ``{"error", "code"}``.
        """
        params = request.params if isinstance(request.params, dict) else {}
        session_id = str(params.get("session_id") or request.session_id or "")
        try:
            payload = await self._dispatch(request.req_method, session_id, params)
        except DiagnosisHostError as exc:
            return self._error(request, exc.message, exc.code)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[diagnosis] request failed: method=%s session_id=%s", request.req_method, session_id)
            return self._error(request, str(exc) or "diagnosis request failed", "INTERNAL_ERROR")
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
        )

    @staticmethod
    def _error(request: AgentRequest, message: str, code: str) -> AgentResponse:
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": message, "code": code},
        )

    async def _dispatch(
        self,
        method: ReqMethod | None,
        session_id: str,
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        service = self.service
        diagnosis_id = str(params.get("diagnosis_id") or "")
        body = params.get("request")
        if method is ReqMethod.DIAGNOSIS_RUN:
            # Access errors (flag, session) take precedence over body errors.
            service.check_access(session_id)
            record = await service.start(session_id, _validate(DiagnosisRunRequest, body))
            return record.model_dump(mode="json", exclude_none=True)
        if method is ReqMethod.DIAGNOSIS_LIST:
            items = service.list_summaries(session_id)
            return {"items": [item.model_dump(mode="json", exclude_none=True) for item in items]}
        if method is ReqMethod.DIAGNOSIS_GET:
            return service.get_record(session_id, diagnosis_id).model_dump(mode="json", exclude_none=True)
        if method is ReqMethod.DIAGNOSIS_CANCEL:
            record = await service.cancel(session_id, diagnosis_id)
            return record.model_dump(mode="json", exclude_none=True)
        if method is ReqMethod.DIAGNOSIS_DELETE:
            service.delete(session_id, diagnosis_id)
            return {}
        if method is ReqMethod.DIAGNOSIS_TTSE_TRIGGER:
            service.check_access(session_id)
            trigger_request = _validate(TtseTriggerRequest, body)
            results = await service.trigger_ttse(session_id, diagnosis_id, trigger_request.root_cause_ids)
            return {
                "results": {key: value.model_dump(mode="json", exclude_none=True) for key, value in results.items()},
            }
        raise DiagnosisHostError("BAD_REQUEST", f"unsupported diagnosis method: {method}", 400)

    async def shutdown(self) -> None:
        """Cancel running diagnoses; a never-used host has nothing to stop."""
        if self._service is not None:
            await self._service.shutdown()


def _validate(model: type[Any], body: Any) -> Any:
    if not isinstance(body, Mapping):
        raise DiagnosisHostError("BAD_REQUEST", "request body must be a JSON object", 400)
    try:
        return model.model_validate(dict(body))
    except ValidationError as exc:
        raise DiagnosisHostError("BAD_REQUEST", _first_validation_error(exc), 400) from exc


__all__ = ["DiagnosisHost", "is_session_busy"]
