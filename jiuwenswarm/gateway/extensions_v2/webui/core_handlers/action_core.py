# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""CORE handlers of the host actions ``ui.appearance``, ``ui.download`` and ``ui.splash.preview``.

Each is the default action at the end of its chain: it checks that the calling activation is current
and that the call came from a browser connection, then asks the owning service to act. Refusals are
answers with ``status="rejected"``, never exceptions.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from openjiuwen.core.events import Event, EventHandler, EventHandlerOps, EventNext

from jiuwenswarm.gateway.extensions_v2.webui.call_context import trusted_connection
from jiuwenswarm.gateway.extensions_v2.webui.models import (
    HostActionOutput,
    UiAppearanceInput,
    UiDownloadInput,
    UiSplashPreviewInput,
)

if TYPE_CHECKING:
    from jiuwenswarm.gateway.extensions_v2.webui.bridge import BrowserBridge

CODE_INVALID_ACTION = "INVALID_ACTION"


def _core(event_name: str, input_type: type, act: Callable[..., Awaitable[dict]]) -> EventHandler:
    async def core(event: Event, ops: EventHandlerOps | None, next_handler: EventNext) -> HostActionOutput:
        del next_handler
        request = event.event_input
        if not isinstance(request, input_type):
            raise TypeError(f"{event_name} requires {input_type.__name__}")
        try:
            answer = await act(request, trusted_connection(ops))
        except ValueError as exc:
            return HostActionOutput(status="rejected", code=CODE_INVALID_ACTION, message=str(exc))
        return HostActionOutput(status=answer["status"], revision=answer.get("revision"))
    return core


def build_appearance_core_handler(browser: BrowserBridge) -> EventHandler:
    """Deliver or revoke the caller's appearance (S_08)."""

    async def act(request: UiAppearanceInput, connection_id: str) -> dict:
        return await browser.apply_appearance(request.package_id, request.generation, connection_id, request.appearance)

    return _core("ui.appearance", UiAppearanceInput, act)


def build_download_core_handler(browser: BrowserBridge) -> EventHandler:
    """Offer one data file to the caller's connection (S_10 section 7)."""

    async def act(request: UiDownloadInput, connection_id: str) -> dict:
        return await browser.offer_download(request.package_id, request.generation, connection_id,
                                            request.path, request.filename)

    return _core("ui.download", UiDownloadInput, act)


def build_splash_preview_core_handler(browser: BrowserBridge) -> EventHandler:
    """Replay the caller's splash on its connection (S_09 section 3.3)."""

    async def act(request: UiSplashPreviewInput, connection_id: str) -> dict:
        return await browser.preview_splash(request.package_id, request.generation, connection_id)

    return _core("ui.splash.preview", UiSplashPreviewInput, act)
