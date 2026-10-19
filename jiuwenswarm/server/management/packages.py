# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Packages management; resources remain owned by its injected host."""

from __future__ import annotations

import logging
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.agent import AgentResponse
from typing import Any
from jiuwenswarm.runtime.request import (
    apply_resolved_mode_to_request as _apply_resolved_mode_to_request,
)
import asyncio
from jiuwenswarm.runtime.request import resolve_request_project_dir

logger = logging.getLogger(__name__)


def create_auto_harness_service(*args: Any, **kwargs: Any) -> Any:
    """Construct the optional service at its first RPC, outside startup."""
    from jiuwenswarm.agents.harness.common.auto_harness import (
        AutoHarnessService as Service,
    )

    return Service(*args, **kwargs)


def _harness_error_code(exc: BaseException) -> str:
    """Map a harness package exception to a wire ``code`` for the frontend.

    Mirrors the import/export code mapping in app_web_handlers.py so the web UI
    can localize the error via ``err.code`` instead of showing the raw backend
    message (which is locale-unaware). Keep in sync with the frontend
    ``resolveHarnessError`` code→i18n mapping.
    """
    msg = str(exc).lower()
    if "already active" in msg or "already exists" in msg:
        return "CONFLICT"
    if "not found" in msg:
        return "NOT_FOUND"
    if "native" in msg:
        return "BAD_REQUEST"
    return "BAD_REQUEST"


class PackagesManagement:
    def __init__(self, host):
        self.host = host

    async def _handle_harness_packages_get(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle harness.packages.get request - retrieve packages info."""
        host = self.host
        try:
            service = create_auto_harness_service(rail=None, agent=None)
            payload = await asyncio.to_thread(service.get_packages_info)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
            )
        except Exception as exc:
            logger.exception("[AgentServer] harness.packages.get failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc)},
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_harness_packages_scan(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle harness.packages.scan request - scan runtime extensions."""
        host = self.host
        try:
            service = create_auto_harness_service(rail=None, agent=None)
            payload = await asyncio.to_thread(service.scan_runtime_extensions)
            await asyncio.to_thread(service.save_packages, payload)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
            )
        except Exception as exc:
            logger.exception("[AgentServer] harness.packages.scan failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc)},
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_harness_packages_activate(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle harness.packages.activate request - activate a harness package."""
        host = self.host
        params = request.params if isinstance(request.params, dict) else {}
        package_id = params.get("package_id")
        if not package_id:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "missing package_id", "code": "BAD_REQUEST"},
            )
            wire = host.services.encode_response(resp, response_id=request.request_id)
            async with send_lock:
                await host.services.send(ws, wire)
            return
        try:
            mode, sub_mode = _apply_resolved_mode_to_request(request)
            agent_mode = "agent" if mode == "auto_harness" else mode
            channel_id = request.channel_id or "web"
            agent = await host._agent_manager.get_agent(
                channel_id=channel_id,
                mode=agent_mode,
                project_dir=resolve_request_project_dir(request),
                sub_mode=sub_mode,
            )
            agent_instance = None
            if agent is not None:
                agent_instance = await agent.ensure_instance()
                logger.info(
                    "[AgentServer] harness.packages.activate: agent_instance type=%s, has_load_harness_config=%s",
                    type(agent_instance).__name__ if agent_instance else None,
                    hasattr(agent_instance, "load_harness_config")
                    if agent_instance
                    else False,
                )
            service = create_auto_harness_service(
                rail=None, agent=agent_instance, agent_manager=host._agent_manager
            )
            payload = await service.activate_package(package_id, channel_id=channel_id)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
            )
        except ValueError as exc:
            logger.warning(
                "[AgentServer] harness.packages.activate validation error: %s", exc
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": _harness_error_code(exc)},
            )
        except Exception as exc:
            logger.exception("[AgentServer] harness.packages.activate failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "INTERNAL_ERROR"},
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_harness_packages_deactivate(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle harness.packages.deactivate request - deactivate a harness package."""
        host = self.host
        params = request.params if isinstance(request.params, dict) else {}
        package_id = params.get("package_id")
        if not package_id:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "missing package_id", "code": "BAD_REQUEST"},
            )
            wire = host.services.encode_response(resp, response_id=request.request_id)
            async with send_lock:
                await host.services.send(ws, wire)
            return
        try:
            channel_id = request.channel_id or "web"
            mode, sub_mode = _apply_resolved_mode_to_request(request)
            agent_mode = "agent" if mode == "auto_harness" else mode
            agent = await host._agent_manager.get_agent(
                channel_id=channel_id,
                project_dir=resolve_request_project_dir(request),
                mode=agent_mode,
                sub_mode=sub_mode,
            )
            agent_instance = None
            if agent is not None:
                agent_instance = await agent.ensure_instance()
            service = create_auto_harness_service(
                rail=None, agent=agent_instance, agent_manager=host._agent_manager
            )
            payload = await service.deactivate_package(
                package_id, channel_id=channel_id
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
            )
        except ValueError as exc:
            logger.warning(
                "[AgentServer] harness.packages.deactivate validation error: %s", exc
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": _harness_error_code(exc)},
            )
        except Exception as exc:
            logger.exception(
                "[AgentServer] harness.packages.deactivate failed: %s", exc
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "INTERNAL_ERROR"},
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_harness_packages_delete(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle harness.packages.delete request - delete a harness package."""
        host = self.host
        params = request.params if isinstance(request.params, dict) else {}
        package_id = params.get("package_id")
        if not package_id:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "missing package_id", "code": "BAD_REQUEST"},
            )
            wire = host.services.encode_response(resp, response_id=request.request_id)
            async with send_lock:
                await host.services.send(ws, wire)
            return
        if package_id == "native":
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "error": "Cannot delete native agent version",
                    "code": "BAD_REQUEST",
                },
            )
            wire = host.services.encode_response(resp, response_id=request.request_id)
            async with send_lock:
                await host.services.send(ws, wire)
            return
        try:
            mode, sub_mode = _apply_resolved_mode_to_request(request)
            agent_mode = "agent" if mode == "auto_harness" else mode
            agent = await host._agent_manager.get_agent(
                channel_id=request.channel_id,
                project_dir=resolve_request_project_dir(request),
                mode=agent_mode,
                sub_mode=sub_mode,
            )
            agent_instance = None
            if agent is not None:
                agent_instance = await agent.ensure_instance()
            service = create_auto_harness_service(
                rail=None, agent=agent_instance, agent_manager=host._agent_manager
            )
            payload = await service.delete_package(
                package_id, channel_id=request.channel_id
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
            )
        except ValueError as exc:
            logger.warning(
                "[AgentServer] harness.packages.delete validation error: %s", exc
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": _harness_error_code(exc)},
            )
        except Exception as exc:
            logger.exception("[AgentServer] harness.packages.delete failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "INTERNAL_ERROR"},
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)
