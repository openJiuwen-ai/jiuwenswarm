# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Controlled appearance: validation, providers and package release (F_11)."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from openjiuwen.core.events import Event, SessionContext

from jiuwenswarm.gateway.extensions_v2.config import GatewayExtensionConfig
from jiuwenswarm.gateway.extensions_v2.loader import LoadedPackage
from jiuwenswarm.gateway.extensions_v2.registry import HandlerDeclaration
from jiuwenswarm.gateway.extensions_v2.runtime import GatewayRuntime
from jiuwenswarm.gateway.extensions_v2.webui.appearance import validate_appearance
from jiuwenswarm.gateway.extensions_v2.webui.models import (
    RenderNode, RenderStatus, RenderTarget, UiActionInput, UiActionOutput, UiRenderInput,
)

THEME = "test.theme"
OTHER = "test.other-theme"
PLAIN = "test.plain"
BACKGROUND_UI = {"apiVersion": 1, "items": [{"type": "web.slot.contribution", "slot": "application.background"}]}
CASES = json.loads((Path(__file__).parents[4] / (
    "jiuwenswarm/channels/web/frontend/tests/fixtures/gatewayAppearanceCases.json"
)).read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_appearance_validation_matches_browser(case):
    if case["valid"]:
        assert validate_appearance(case["appearance"]) == case["appearance"]
    else:
        with pytest.raises(ValueError):
            validate_appearance(case["appearance"])


def _channel():
    events = []
    sockets = {}

    async def ensure_ws_id(ws):
        ws._jiuwen_ws_id = getattr(ws, "_jiuwen_ws_id", "") or f"browser-{len(sockets) + 1}"
        sockets[ws._jiuwen_ws_id] = ws
        return ws._jiuwen_ws_id

    async def send_event(ws, event, payload):
        events.append((ws._jiuwen_ws_id, event, payload))

    async def send_response(ws, req_id, *, ok, payload=None, error=None, code=None):
        return None

    return SimpleNamespace(
        channel_id="web", events=events, ensure_ws_id=ensure_ws_id, connection_user_id=lambda ws: None,
        ws_by_id=sockets.get, send_event=send_event, send_response=send_response,
    )


class Theme:
    """A package whose ready handler runs queued appearance calls on the caller's connection."""

    def __init__(self, package_id: str, ui=None) -> None:
        self.package_id = package_id
        self.ui = BACKGROUND_UI if ui is None else ui
        self.calls: list = []
        self.results: list = []

    async def ready(self, event, ops, next_handler):
        for call in self.calls:
            try:
                self.results.append(await call(ops, event))
            except ValueError as exc:
                self.results.append(exc)
        self.calls.clear()
        return UiActionOutput(accepted=True, status="ready")

    def package(self, root: Path) -> LoadedPackage:
        root.mkdir(parents=True, exist_ok=True)
        (root / "assets").mkdir(exist_ok=True)
        return LoadedPackage(
            package_id=self.package_id, version="1.0.0", root=root, client={}, ui=self.ui,
            handlers=(HandlerDeclaration("ui.ready", self.ready, {"target.package_id": self.package_id}, "ready"),),
        )


def _apply(appearance):
    async def call(ops, event):
        return await ops.appearance.apply(appearance)
    return call


def _render_background(ops, event):
    target = event.event_input.target
    return ops.ui.render(UiRenderInput(
        target=RenderTarget(session_id="", slot_id=target.slot_id, instance_key=target.instance_key),
        root=RenderNode(component="MediaSurface", props={"kind": "image", "src": "/extension-assets/x/y/bg.png",
                                                         "title": "Theme"}),
    ))


async def _start(tmp_path: Path) -> GatewayRuntime:
    runtime = GatewayRuntime(channel=_channel(), config=GatewayExtensionConfig(
        install_dir=tmp_path / "install", data_dir=tmp_path / "data"))
    await runtime.start()
    return runtime


async def _ready(runtime: GatewayRuntime, ws, package_id: str, slot: str = "application.background"):
    visited, _ = await runtime.intercept(ws, "ready", "webui.dispatch", {
        "event": "ui.ready", "payload": {},
        "target": {"scope": "application", "slot_id": slot, "instance_key": "main", "package_id": package_id},
    })
    assert visited is True


def _ready_input(package_id: str, connection_id: str) -> UiActionInput:
    target = RenderTarget(session_id="", slot_id="application.background", package_id=package_id,
                          instance_key="main", scope="application", connection_id=connection_id)
    return UiActionInput("ui.ready", target, {}, {})


def _frames(runtime: GatewayRuntime, event: str = "webui.appearance"):
    return [(connection, payload) for connection, name, payload in runtime.channel.events if name == event]


async def test_apply_revoke_and_provider_switch(tmp_path):
    theme, other = Theme(THEME), Theme(OTHER)
    runtime = await _start(tmp_path)
    try:
        runtime.publish(theme.package(tmp_path / "theme"))
        runtime.publish(other.package(tmp_path / "other"))
        ws = SimpleNamespace(closed=False)
        accent = {"accent": "#b45309", "surface": "translucent", "surfaceOpacity": 0.8}
        theme.calls.append(_apply(accent))
        await _ready(runtime, ws, THEME)
        assert theme.results == [{"status": "applied", "revision": 1}]
        assert _frames(runtime) == [("browser-1", {"package_id": THEME, "revision": 1, "appearance": accent})]

        other.calls.append(_apply({"accent": "#2f6fae"}))
        await _ready(runtime, ws, OTHER)
        assert other.results == [{"status": "applied", "revision": 2}]
        assert runtime.browser.appearance.provider("browser-1") == OTHER

        # The replaced provider cannot clear the current appearance.
        theme.calls.append(_apply(None))
        await _ready(runtime, ws, THEME)
        assert theme.results[-1] == {"status": "unchanged", "revision": 2}
        assert len(_frames(runtime)) == 2

        other.calls.append(_apply(None))
        await _ready(runtime, ws, OTHER)
        assert other.results[-1] == {"status": "revoked", "revision": 3}
        assert _frames(runtime)[-1] == ("browser-1", {"package_id": OTHER, "revision": 3, "appearance": None})
        assert runtime.browser.appearance.provider("browser-1") is None
    finally:
        await runtime.shutdown()


async def test_invalid_calls_are_rejected_without_frames(tmp_path):
    theme = Theme(THEME)
    plain = Theme(PLAIN, ui={})
    runtime = await _start(tmp_path)
    try:
        runtime.publish(theme.package(tmp_path / "theme"))
        runtime.publish(plain.package(tmp_path / "plain"))
        ws = SimpleNamespace(closed=False)
        theme.calls.extend([_apply({"accent": "red"}), _apply({"tokens": {"--color-text-primary": "#000"}})])
        await _ready(runtime, ws, THEME)
        assert all(isinstance(result, ValueError) for result in theme.results)

        # A package without an application slot cannot contribute appearance.
        plain.calls.append(_apply({"accent": "#b45309"}))
        await runtime.bus.trigger(Event("ui.ready", _ready_input(PLAIN, "browser-1")),
                                  SessionContext("gateway:browser-1", None, {"connection_id": "browser-1"}))
        assert len(plain.results) == 1 and isinstance(plain.results[0], ValueError)

        # Without a browser connection there is nothing to apply the appearance to.
        theme.calls.append(_apply({"accent": "#b45309"}))
        await runtime.bus.trigger(Event("ui.ready", _ready_input(THEME, "")), SessionContext("gateway:", None, {}))
        assert isinstance(theme.results[-1], ValueError)
        assert _frames(runtime) == []
    finally:
        await runtime.shutdown()


async def test_release_revokes_appearance_and_clears_views(tmp_path):
    theme = Theme(THEME)
    runtime = await _start(tmp_path)
    try:
        package = theme.package(tmp_path / "theme")
        runtime.publish(package)
        ws = SimpleNamespace(closed=False)
        theme.calls.extend([_render_background, _apply({"accent": "#b45309"})])
        await _ready(runtime, ws, THEME)
        assert theme.results[0].status == RenderStatus.DELIVERED
        rendered = theme.results[0].revision

        runtime.publish(package)  # reload: the old activation is released first
        await runtime.browser.settle()
        clears = _frames(runtime, "webui.render.clear")
        assert [payload["revision"] for _, payload in clears] == [rendered + 1]
        assert clears[0][1]["reason"] == "released"
        assert _frames(runtime)[-1] == ("browser-1", {"package_id": THEME, "revision": 2, "appearance": None})
        assert runtime.browser.appearance.provider("browser-1") is None

        # The re-render of the new activation starts above the clear the browser accepted.
        theme.calls.append(_render_background)
        await _ready(runtime, ws, THEME)
        assert theme.results[-1].revision == rendered + 2

        theme.calls.append(_apply({"accent": "#b45309"}))
        await _ready(runtime, ws, THEME)
        runtime.unpublish(THEME)  # uninstall
        await runtime.browser.settle()
        assert _frames(runtime)[-1][1] == {"package_id": THEME, "revision": 4, "appearance": None}
        assert _frames(runtime, "webui.render.clear")[-1][1]["revision"] == rendered + 3
    finally:
        await runtime.shutdown()


async def test_release_during_the_first_send_still_revokes_the_appearance(tmp_path):
    """The provider is only known after its first frame is sent; a release in between must still revoke it."""
    theme = Theme(THEME)
    runtime = await _start(tmp_path)
    channel = runtime.channel
    sent = channel.send_event
    started, resume = asyncio.Event(), asyncio.Event()

    async def slow_send(ws, event, payload):
        if event == "webui.appearance" and payload["appearance"] is not None:
            started.set()
            await resume.wait()
        await sent(ws, event, payload)

    channel.send_event = slow_send
    try:
        runtime.publish(theme.package(tmp_path / "theme"))
        ws = SimpleNamespace(closed=False)
        theme.calls.append(_apply({"accent": "#b45309"}))
        ready = asyncio.create_task(_ready(runtime, ws, THEME))
        await started.wait()
        runtime.unpublish(THEME)  # uninstall while the first appearance frame is in flight
        resume.set()
        await ready
        await runtime.browser.settle()
        frames = [payload for _, payload in _frames(runtime)]
        assert frames == [
            {"package_id": THEME, "revision": 1, "appearance": {"accent": "#b45309"}},
            {"package_id": THEME, "revision": 2, "appearance": None},
        ]
        assert runtime.browser.appearance.provider("browser-1") is None
    finally:
        await runtime.shutdown()


async def test_closed_connection_forgets_its_provider(tmp_path):
    theme = Theme(THEME)
    runtime = await _start(tmp_path)
    try:
        runtime.publish(theme.package(tmp_path / "theme"))
        ws = SimpleNamespace(closed=False)
        theme.calls.append(_apply({"accent": "#b45309"}))
        await _ready(runtime, ws, THEME)
        await runtime.on_disconnect(ws)
        assert runtime.browser.appearance.provider("browser-1") is None
        runtime.unpublish(THEME)
        await runtime.browser.settle()
        assert len(_frames(runtime)) == 1
    finally:
        await runtime.shutdown()
