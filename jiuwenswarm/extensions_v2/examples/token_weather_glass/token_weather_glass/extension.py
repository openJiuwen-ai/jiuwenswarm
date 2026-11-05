# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Own independent session usage and detail state for one package activation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import Any

from openjiuwen.core.events import Event, EventHandlerOps, EventNext

from jiuwenswarm.extensions_v2.event_types.context_usage import CONTEXT_USAGE_EVENT, ContextUsageUpdate
from jiuwenswarm.extensions_v2.webui.models import RenderTarget, UiActionInput, UiActionOutput, UiRenderInput
from jiuwenswarm.extensions_v2.webui.resources import WebUIResourceDirectory, complete_before_cancel

from .details import DETAILS_INSTANCE, DETAILS_SLOT, write_details, details_view
from .locales import MESSAGES
from .history import TurnHistory
from .views import DETAILS_ACTION, INSTANCE, PACKAGE_ID, PREFERENCE, SETTING, SETTINGS_SLOT, SLOT, setting_view, weather_view


@dataclass(slots=True)
class WeatherState:
    usage: dict[str, Any]
    mounted: bool = False
    details_open: bool = False
    history: TurnHistory = field(default_factory=TurnHistory)
    resources: WebUIResourceDirectory | None = None


class TokenWeatherGlass:
    """Keep observations scoped to the bound conversation; preferences belong to the client."""

    def __init__(self) -> None:
        self._sessions: dict[str, WeatherState] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def register(self, registrar) -> None:
        registrar.register_event_handler(CONTEXT_USAGE_EVENT, self.on_usage, {}, "user")
        for event in ("ui.ready", "ui.click"):
            for slot in (SLOT, SETTINGS_SLOT, DETAILS_SLOT):
                registrar.register_event_handler(event, self.on_ui, {"target.slot_id": slot}, "user")

    async def on_usage(self, event: Event, ops: EventHandlerOps | None, next_handler: EventNext):
        update = event.event_input
        if (not isinstance(update, ContextUsageUpdate) or ops is None or ops.session_context is None
                or ops.session_context.session_id != update.session_id):
            return await next_handler(event)
        async with self._locks.setdefault(update.session_id, asyncio.Lock()):
            usage = await ops.session.usage(breakdown="summary")
            state = self._sessions.setdefault(update.session_id, WeatherState(usage))
            reading = state.history.observe(update, usage)
            if reading is not None and reading.number == state.history.count:
                state.usage = usage
            if reading is not None and state.mounted:
                await ops.ui.render(UiRenderInput(
                    target=RenderTarget(update.session_id, SLOT, PACKAGE_ID, INSTANCE),
                    root=weather_view(state.usage, state.history.readings),
                    visible_when=PREFERENCE,
                ))
            if reading is not None and state.details_open:
                await self._render_details(ops, update.session_id, state)
        return await next_handler(event)

    async def on_ui(self, event: Event, ops: EventHandlerOps | None, next_handler: EventNext):
        request = event.event_input
        if not isinstance(request, UiActionInput) or request.target.package_id not in ("", PACKAGE_ID):
            return await next_handler(event)
        if ops is None:
            return UiActionOutput(accepted=False, status="unavailable")
        target = request.target
        if target.slot_id == SETTINGS_SLOT:
            if request.event_type != "ui.ready" or target.instance_key != SETTING or target.session_id:
                return UiActionOutput(accepted=False, status="invalid")
            output = await ops.ui.render(UiRenderInput(target=replace(target, package_id=PACKAGE_ID), root=setting_view()))
        elif target.slot_id == DETAILS_SLOT and target.session_id and target.instance_key in ("", DETAILS_INSTANCE):
            if request.event_type != "ui.ready":
                return UiActionOutput(accepted=False, status="invalid")
            async with self._locks.setdefault(target.session_id, asyncio.Lock()):
                state = self._sessions.get(target.session_id)
                if state is None or not state.details_open:
                    return UiActionOutput(accepted=True, status="not_open")
                output = await self._render_details(ops, target.session_id, state)
        elif target.slot_id == SLOT and target.session_id and target.instance_key in ("", INSTANCE):
            async with self._locks.setdefault(target.session_id, asyncio.Lock()):
                if request.event_type == "ui.ready":
                    usage = await ops.session.usage(breakdown="summary")
                    state = self._sessions.setdefault(target.session_id, WeatherState(usage))
                    state.usage = usage
                elif (request.event_type == "ui.click" and target.package_id == PACKAGE_ID
                      and target.instance_key == INSTANCE and request.payload.get("action_id") == DETAILS_ACTION
                      and target.session_id in self._sessions and self._sessions[target.session_id].mounted):
                    state = self._sessions[target.session_id]
                    output = await self._render_details(ops, target.session_id, state)
                    delivered = output is not None and output.status == "delivered"
                    if delivered:
                        state.details_open = True
                    return UiActionOutput(accepted=delivered, status=output.status if output else "unavailable")
                else:
                    return UiActionOutput(accepted=False, status="invalid")
                output = await ops.ui.render(UiRenderInput(
                    target=replace(target, package_id=PACKAGE_ID, instance_key=INSTANCE),
                    root=weather_view(state.usage, state.history.readings),
                    visible_when=PREFERENCE,
                ))
                if output is not None and output.status == "delivered":
                    state.mounted = True
        else:
            return UiActionOutput(accepted=False, status="invalid")
        delivered = output is not None and output.status == "delivered"
        return UiActionOutput(accepted=delivered, status=output.status if output is not None else "unavailable")

    @staticmethod
    async def _render_details(ops: EventHandlerOps, session_id: str, state: WeatherState):
        previous = state.resources
        resources = await ops.ui.create_resource_dir(
            previous=previous.resource_id if previous is not None else "",
        )
        try:
            async with resources:
                await complete_before_cancel(asyncio.to_thread(
                    write_details, resources.path, state.usage, state.history.readings,
                ))
            sources = {language: await ops.ui.asset_url(f"{language}.html", resource_id=resources.resource_id)
                       for language in MESSAGES}
            output = await ops.ui.render(UiRenderInput(
                target=RenderTarget(session_id, DETAILS_SLOT, PACKAGE_ID, DETAILS_INSTANCE),
                root=details_view(sources),
                visible_when=PREFERENCE,
            ))
        except BaseException as error:
            try:
                await ops.ui.release_resources(resources.resource_id)
            except (Exception, asyncio.CancelledError) as cleanup_error:
                if isinstance(error, asyncio.CancelledError):
                    if error.__cause__ is not None:
                        raise error from BaseExceptionGroup(
                            "WebUI operation and cleanup failed", [error.__cause__, cleanup_error],
                        )
                    raise error from cleanup_error
                raise
            raise
        if output is not None and output.status == "delivered":
            state.resources = resources
            if previous is not None:
                await ops.ui.release_resources(previous.resource_id)
        else:
            await ops.ui.release_resources(resources.resource_id)
        return output


def create_extension() -> TokenWeatherGlass:
    return TokenWeatherGlass()
