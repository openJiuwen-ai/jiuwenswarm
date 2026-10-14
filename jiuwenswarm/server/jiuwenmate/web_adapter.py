"""Web management RPCs for JiuwenMate colleagues, served from the AgentServer adapter registry.

Every call works in the single local namespace. Results are the public views of
``PersistentAgentService``: no Spec, model connection or secret is returned, and
a refused operation keeps its service error code so the page can show it.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Mapping
from typing import Any

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.jiuwenmate.models import ColleagueSettings, PersistentAgentError
from jiuwenswarm.server.jiuwenmate.service import PersistentAgentService
from jiuwenswarm.server.runtime.gateway_adapter.base import GatewayAdapter, build_error_response

WEB_NAMESPACE = "local"


def configured_model_catalog() -> dict[str, Any]:
    """The models the colleague service resolves, without connection details."""
    from jiuwenswarm.common.config import get_default_models
    from jiuwenswarm.runtime.model_catalog import build_model_catalog

    return build_model_catalog(get_default_models()).to_dict()


def _agent_id(params: Mapping[str, Any]) -> str:
    value = params.get("agent_id")
    if not isinstance(value, str) or not value:
        raise PersistentAgentError("agent_id is required")
    return value


def _settings(params: Mapping[str, Any]) -> ColleagueSettings:
    return ColleagueSettings.from_dict(params.get("settings"))


class JiuwenMateWebAdapter(GatewayAdapter):
    methods = frozenset(
        {
            ReqMethod.JIUWENMATE_COLLEAGUE_ENSURE_DEFAULT.value,
            ReqMethod.JIUWENMATE_COLLEAGUE_LIST.value,
            ReqMethod.JIUWENMATE_COLLEAGUE_GET.value,
            ReqMethod.JIUWENMATE_COLLEAGUE_CREATE.value,
            ReqMethod.JIUWENMATE_COLLEAGUE_SETTINGS_GET.value,
            ReqMethod.JIUWENMATE_COLLEAGUE_SETTINGS_UPDATE.value,
            ReqMethod.JIUWENMATE_COLLEAGUE_PUBLISH.value,
            ReqMethod.JIUWENMATE_COLLEAGUE_DELETE.value,
            ReqMethod.JIUWENMATE_MODELS_LIST.value,
        }
    )

    def __init__(
        self,
        service_factory: Callable[[], PersistentAgentService] = PersistentAgentService.create_default,
        model_catalog: Callable[[], dict[str, Any]] = configured_model_catalog,
    ) -> None:
        self._service_factory = service_factory
        self._model_catalog = model_catalog
        self._service: PersistentAgentService | None = None
        self._service_lock = threading.Lock()

    def _colleagues(self) -> PersistentAgentService:
        with self._service_lock:
            if self._service is None:
                self._service = self._service_factory()
            return self._service

    async def handle(self, request: AgentRequest) -> AgentResponse:
        method = request.req_method.value if request.req_method is not None else ""
        params = request.params if isinstance(request.params, dict) else {}
        try:
            payload = await asyncio.to_thread(self._call, method, params)
        except PersistentAgentError as exc:
            return build_error_response(request, str(exc), code=exc.code)
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
            metadata=request.metadata,
        )

    def _call(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if method == ReqMethod.JIUWENMATE_MODELS_LIST.value:
            return self._model_catalog()
        service = self._colleagues()
        if method == ReqMethod.JIUWENMATE_COLLEAGUE_ENSURE_DEFAULT.value:
            view = service.ensure_default_colleague(WEB_NAMESPACE)
            return {"colleague": None if view is None else view.to_dict()}
        if method == ReqMethod.JIUWENMATE_COLLEAGUE_LIST.value:
            return {"colleagues": [view.to_dict() for view in service.list_colleagues(WEB_NAMESPACE)]}
        if method == ReqMethod.JIUWENMATE_COLLEAGUE_CREATE.value:
            return {"colleague": service.create_colleague(WEB_NAMESPACE, _settings(params)).to_dict()}
        agent_id = _agent_id(params)
        if method == ReqMethod.JIUWENMATE_COLLEAGUE_GET.value:
            return {"colleague": service.get_colleague(WEB_NAMESPACE, agent_id).to_dict()}
        if method == ReqMethod.JIUWENMATE_COLLEAGUE_SETTINGS_GET.value:
            return {"settings": service.read_settings(WEB_NAMESPACE, agent_id).to_dict()}
        if method == ReqMethod.JIUWENMATE_COLLEAGUE_SETTINGS_UPDATE.value:
            return {"colleague": service.update_settings(WEB_NAMESPACE, agent_id, _settings(params)).to_dict()}
        if method == ReqMethod.JIUWENMATE_COLLEAGUE_PUBLISH.value:
            return {"version": service.publish(WEB_NAMESPACE, agent_id).to_dict()}
        if method == ReqMethod.JIUWENMATE_COLLEAGUE_DELETE.value:
            service.delete_colleague(WEB_NAMESPACE, agent_id)
            return {"deleted": True}
        raise PersistentAgentError(f"unsupported method {method!r}")
