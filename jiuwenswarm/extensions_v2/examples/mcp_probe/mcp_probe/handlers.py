# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Serve MCP Probe panel actions through ops.mcp.*."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from pathlib import Path

from openjiuwen.core.events import Event, EventHandlerOps, EventNext
from openjiuwen.core.events.ops_impl.common import OpsError
from openjiuwen.core.events.ops_impl.mcp import (
    McpCallInput,
    McpCallOutput,
    McpConnectInput,
    McpConnectOutput,
    McpRegisterSpec,
    McpStatus,
    McpStdioSpec,
)

from jiuwenswarm.extensions_v2.webui.constants import WebUIEvent
from jiuwenswarm.extensions_v2.webui.models import (
    RenderStatus,
    RenderTarget,
    UiActionInput,
    UiActionOutput,
    UiRenderInput,
    UiRenderOutput,
)

from .state import ProbeState
from .views import (
    ACTION_CALL_ECHO,
    ACTION_CALL_INFO,
    ACTION_CONNECT,
    ACTION_REGISTER,
    INSTANCE_KEY,
    PACKAGE_ID,
    REGISTERED_SERVER,
    SERVER_NAME,
    SLOT_PANEL,
    panel_tree,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
ACTIONS = (
    ACTION_REGISTER,
    ACTION_CONNECT,
    ACTION_CALL_ECHO,
    ACTION_CALL_INFO,
)
logger = logging.getLogger(__name__)


class McpProbeHandlers:
    """Keep the latest MCP results per session and serve panel actions."""

    def __init__(self) -> None:
        self._states: dict[str, ProbeState] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def handlers(self) -> tuple:
        return (
            (WebUIEvent.READY.value, self.on_ready, {"target.slot_id": SLOT_PANEL}),
            (WebUIEvent.CLICK.value, self.on_click, {"target.slot_id": SLOT_PANEL}),
        )

    async def on_ready(
        self,
        event: Event,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
    ) -> UiActionOutput:
        request = event.event_input
        if not isinstance(request, UiActionInput):
            return UiActionOutput(accepted=False, status="invalid")
        target = request.target
        if target.package_id not in ("", PACKAGE_ID) or target.instance_key not in (
            "",
            INSTANCE_KEY,
        ):
            return await next_handler(event)
        lock = self._locks.setdefault(target.session_id, asyncio.Lock())
        async with lock:
            output = await _render(
                ops, target.session_id, self._states.get(target.session_id)
            )
        receipt = _receipt(output, "rendered")
        await next_handler(event)
        return receipt

    async def on_click(
        self,
        event: Event,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
    ) -> UiActionOutput:
        request = event.event_input
        if not isinstance(request, UiActionInput):
            return UiActionOutput(accepted=False, status="invalid")
        target = request.target
        action_id = (
            request.payload.get("action_id")
            if isinstance(request.payload, Mapping)
            else None
        )
        if (
            target.package_id != PACKAGE_ID
            or target.instance_key != INSTANCE_KEY
            or action_id not in ACTIONS
        ):
            return await next_handler(event)
        if ops is None:
            return UiActionOutput(accepted=False, status="unavailable")

        lock = self._locks.setdefault(target.session_id, asyncio.Lock())
        async with lock:
            state = self._states.setdefault(target.session_id, ProbeState())
            if action_id == ACTION_REGISTER:
                await _register(ops, state)
            elif action_id == ACTION_CONNECT:
                state.connect = await _connect(ops, server_name=SERVER_NAME)
            else:
                state.last_call = await _call(ops, state, action_id)
            output = await _render(ops, target.session_id, state)
        return _receipt(output, "rendered")


async def _register(ops: EventHandlerOps, state: ProbeState) -> None:
    if state.register is None:
        try:
            state.register = await ops.mcp.register(_register_spec())
            state.register_error = ""
        except Exception as exc:  # noqa: BLE001 - surface the failure in the panel
            logger.warning("[MCPProbe] register failed: %s", exc)
            state.register_error = str(exc) or type(exc).__name__
            return
    state.connect = await _connect(ops, server_id=state.register.server_id)


def _register_spec() -> McpRegisterSpec:
    """Build a runtime registration from the packaged mcp.json declaration."""

    declaration = json.loads((PACKAGE_ROOT / "mcp.json").read_text("utf-8"))
    params = declaration["mcpServers"][SERVER_NAME]["params"]
    return McpRegisterSpec(
        name=REGISTERED_SERVER,
        transport="stdio",
        connection=McpStdioSpec(
            command=params["command"],
            args=tuple(params.get("args", ())),
        ),
    )


async def _connect(
    ops: EventHandlerOps, *, server_id: str = "", server_name: str = ""
) -> McpConnectOutput:
    request = (
        McpConnectInput(server_id=server_id)
        if server_id
        else McpConnectInput(server_name=server_name)
    )
    try:
        return await ops.mcp.connect(request)
    except Exception as exc:  # noqa: BLE001 - surface the failure in the panel
        logger.warning("[MCPProbe] connect failed: %s", exc)
        return _connect_failure(exc, server_name or server_id)


async def _call(
    ops: EventHandlerOps, state: ProbeState, action_id: str
) -> McpCallOutput:
    connect = state.connect
    if connect is None or not connect.connected or not connect.server_id:
        return McpCallOutput(
            status=McpStatus.SERVER_NOT_FOUND,
            error=OpsError(kind="not_connected", message="connect the server first"),
        )
    state.calls += 1
    state.last_tool = "server_info" if action_id == ACTION_CALL_INFO else "echo"
    request = (
        McpCallInput(server_id=connect.server_id, tool=state.last_tool)
        if action_id == ACTION_CALL_INFO
        else McpCallInput(
            server_id=connect.server_id,
            tool=state.last_tool,
            arguments={"text": f"probe #{state.calls}"},
        )
    )
    try:
        return await ops.mcp.call(request)
    except Exception as exc:  # noqa: BLE001 - surface the failure in the panel
        logger.warning("[MCPProbe] call failed: %s", exc)
        return McpCallOutput(
            status=McpStatus.ERROR,
            error=OpsError(kind="mcp_error", message=str(exc) or type(exc).__name__),
        )


def _connect_failure(exc: Exception, server_name: str) -> McpConnectOutput:
    return McpConnectOutput(
        status=McpStatus.ERROR,
        server_name=server_name,
        error=OpsError(kind="mcp_error", message=str(exc) or type(exc).__name__),
    )


async def _render(
    ops: EventHandlerOps | None,
    session_id: str,
    state: ProbeState | None,
) -> UiRenderOutput | None:
    if ops is None:
        return None
    target = RenderTarget(
        session_id=session_id,
        slot_id=SLOT_PANEL,
        package_id=PACKAGE_ID,
        instance_key=INSTANCE_KEY,
    )
    return await ops.ui.render(UiRenderInput(target=target, root=panel_tree(state)))


def _receipt(output: UiRenderOutput | None, delivered_status: str) -> UiActionOutput:
    if output is None:
        return UiActionOutput(accepted=False, status="unavailable")
    if output.status is RenderStatus.DELIVERED:
        return UiActionOutput(accepted=True, status=delivered_status)
    return UiActionOutput(accepted=False, status=output.status.value)
