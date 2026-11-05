# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Startup splash: declarations, providers, platform frames and ui.closed (F_12)."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.gateway.extensions_v2.config import GatewayExtensionConfig
from jiuwenswarm.gateway.extensions_v2.loader import LoadedPackage
from jiuwenswarm.gateway.extensions_v2.models import GatewayRequestOutput
from jiuwenswarm.gateway.extensions_v2.registry import HandlerDeclaration
from jiuwenswarm.gateway.extensions_v2.runtime import GatewayRuntime
from jiuwenswarm.gateway.extensions_v2.webui import bridge as bridge_module
from jiuwenswarm.gateway.extensions_v2.webui.models import (
    RenderMode, RenderNode, RenderTarget, UiActionOutput, UiRenderInput,
)

SURFACE = RenderNode(component="MediaSurface", props={"kind": "html", "src": "/extension-assets/p/g/boot.html",
                                                      "title": "Boot"})
READY = {"launch_id": "launch-1", "first_visit": True, "reduced_motion": False}


def _channel():
    responses = []
    sockets = {}

    async def ensure_ws_id(ws):
        ws._jiuwen_ws_id = "browser-1"
        sockets[ws._jiuwen_ws_id] = ws
        return ws._jiuwen_ws_id

    async def send_event(ws, event, payload):
        return None

    async def send_response(ws, req_id, *, ok, payload=None, error=None, code=None):
        responses.append({"ok": ok, "payload": payload, "error": error})

    return SimpleNamespace(channel_id="web", responses=responses, ensure_ws_id=ensure_ws_id,
                           connection_user_id=lambda ws: None, ws_by_id=sockets.get,
                           send_event=send_event, send_response=send_response)


class Provider:
    """A splash package that plays, skips or stalls on ready and records ui.closed."""

    def __init__(self, package_id: str, splash=None, *, mode: str = "play") -> None:
        self.package_id = package_id
        self.splash = splash
        self.mode = mode
        self.readies: list = []
        self.closed: list = []

    async def ready(self, event, ops, next_handler):
        self.readies.append(dict(event.event_input.payload))
        target = event.event_input.target
        if self.mode == "stall":
            await asyncio.sleep(1)
        if self.mode in ("play", "clear"):
            await ops.ui.render(UiRenderInput(
                target=RenderTarget(session_id="", slot_id=target.slot_id, instance_key=target.instance_key),
                root=SURFACE if self.mode == "play" else None,
                mode=RenderMode.REPLACE if self.mode == "play" else RenderMode.CLEAR,
            ))
        return UiActionOutput(accepted=True, status="ready")

    async def on_closed(self, event, ops, next_handler):
        self.closed.append(dict(event.event_input.payload))
        return UiActionOutput(accepted=True, status="closed")

    def package(self, root: Path, slots=("application.splash",)) -> LoadedPackage:
        root.mkdir(parents=True, exist_ok=True)
        items = []
        for slot in slots:
            item = {"type": "web.slot.contribution", "slot": slot}
            if slot == "application.splash" and self.splash is not None:
                item["splash"] = self.splash
            items.append(item)
        match = {"target.package_id": self.package_id}
        return LoadedPackage(
            package_id=self.package_id, version="1.0.0", root=root, client={},
            ui={"apiVersion": 1, "items": items},
            handlers=(HandlerDeclaration("ui.ready", self.ready, match, "ready"),
                      HandlerDeclaration("ui.closed", self.on_closed, match, "closed")),
        )


def _deny_everything(root: Path) -> LoadedPackage:
    async def deny(event, ops, next_handler):
        return GatewayRequestOutput(handled=True, route="deny", error={"code": "AUTH_REQUIRED", "message": "login"})
    root.mkdir(parents=True, exist_ok=True)
    return LoadedPackage(package_id="test.policy", version="1.0.0", root=root, client={}, ui={},
                         handlers=(HandlerDeclaration("gateway.request", deny, {}, "deny"),))


async def _start(tmp_path: Path) -> GatewayRuntime:
    runtime = GatewayRuntime(channel=_channel(), config=GatewayExtensionConfig(
        install_dir=tmp_path / "install", data_dir=tmp_path / "data"))
    await runtime.start()
    return runtime


async def _frame(runtime, ws, method, params):
    visited, _ = await runtime.intercept(ws, "req", method, params)
    response = runtime.channel.responses[-1]
    return visited, response


def _dispatch(slot, event, package_id="", payload=None):
    target = {"scope": "application", "slot_id": slot, "instance_key": "main"}
    if package_id:
        target["package_id"] = package_id
    return {"event": event, "target": target, "payload": payload if payload is not None else {}}


@pytest.mark.parametrize("splash", [
    {"skippable": "yes"}, {"maxDurationMs": 999}, {"maxDurationMs": 1.5e3}, {"maxDurationMs": True},
    {"loop": True}, "fast",
])
async def test_invalid_splash_declarations_fail_publication(tmp_path, splash):
    runtime = await _start(tmp_path)
    try:
        with pytest.raises(ValueError):
            runtime.publish(Provider("test.bad", splash).package(tmp_path / "bad"))
        misplaced = Provider("test.misplaced").package(tmp_path / "misplaced", slots=("application.background",))
        misplaced.ui["items"][0]["splash"] = {"skippable": True}
        with pytest.raises(ValueError):
            runtime.publish(misplaced)
        assert not runtime.active_packages()
    finally:
        await runtime.shutdown()


async def test_providers_are_listed_in_publication_order_with_capped_durations(tmp_path, monkeypatch):
    warnings = []
    monkeypatch.setattr(bridge_module.logger, "warning", lambda message, *args: warnings.append(message % args))
    runtime = await _start(tmp_path)
    try:
        runtime.publish(Provider("test.first", {"skippable": True, "maxDurationMs": 9500}).package(tmp_path / "a"))
        runtime.publish(Provider("test.second", {"maxDurationMs": 90000}).package(tmp_path / "b"))
        runtime.publish(Provider("test.plain").package(tmp_path / "c", slots=("application.background",)))
        runtime.publish(Provider("test.third").package(tmp_path / "d"))
        assert warnings == ["[GatewayExtensions] test.second declares a 90000 ms splash; it runs for at most 60000 ms"]
        visited, response = await _frame(runtime, SimpleNamespace(closed=False), "webui.splash.providers", {})
        assert visited is True and response["ok"] is True
        assert response["payload"] == {"providers": [
            {"package_id": "test.first", "skippable": True, "max_duration_ms": 9500},
            {"package_id": "test.second", "skippable": False, "max_duration_ms": 60000},
            {"package_id": "test.third", "skippable": False, "max_duration_ms": 60000},
        ], "customized": False}
    finally:
        await runtime.shutdown()


async def test_lifecycle_frames_bypass_a_request_policy_that_denies_everything(tmp_path):
    playing, skipping = Provider("test.play"), Provider("test.skip", mode="clear")
    background = Provider("test.background")
    runtime = await _start(tmp_path)
    try:
        runtime.publish(_deny_everything(tmp_path / "policy"))
        runtime.publish(playing.package(tmp_path / "play"))
        runtime.publish(skipping.package(tmp_path / "skip"))
        runtime.publish(background.package(tmp_path / "bg", slots=("application.background", "application.entry")))
        ws = SimpleNamespace(closed=False)

        _, response = await _frame(runtime, ws, "webui.dispatch", _dispatch("application.splash", "ui.ready", "test.play", READY))
        assert response["payload"] == {"accepted": True, "status": "playing"}
        assert playing.readies == [READY]
        _, response = await _frame(runtime, ws, "webui.dispatch", _dispatch("application.splash", "ui.ready", "test.skip", READY))
        assert response["payload"] == {"accepted": True, "status": "skipped"}

        _, response = await _frame(runtime, ws, "webui.dispatch", _dispatch("application.background", "ui.ready"))
        assert response["payload"]["accepted"] is True and background.readies == [{}]

        # Other application frames still go through the request policy.
        _, response = await _frame(runtime, ws, "webui.dispatch", _dispatch("application.entry", "ui.ready"))
        assert response["ok"] is False and len(background.readies) == 1
    finally:
        await runtime.shutdown()


async def test_splash_ready_rejects_untargeted_or_malformed_frames(tmp_path):
    provider = Provider("test.play")
    runtime = await _start(tmp_path)
    try:
        runtime.publish(provider.package(tmp_path / "play"))
        ws = SimpleNamespace(closed=False)
        for params in (
            _dispatch("application.splash", "ui.ready", "", READY),
            _dispatch("application.splash", "ui.ready", "test.play", {**READY, "extra": 1}),
            _dispatch("application.splash", "ui.ready", "test.play", {**READY, "launch_id": ""}),
            _dispatch("application.splash", "ui.ready", "test.play", {**READY, "first_visit": "yes"}),
            _dispatch("application.background", "ui.closed", "test.play", {"reason": "complete", "revision": 1}),
        ):
            _, response = await _frame(runtime, ws, "webui.dispatch", params)
            assert response["payload"]["accepted"] is False, params
        _, response = await _frame(runtime, ws, "webui.dispatch", _dispatch("application.splash", "ui.ready", "test.other", READY))
        assert response["payload"] == {"accepted": False, "status": "unhandled"}
        assert provider.readies == []
    finally:
        await runtime.shutdown()


async def test_closed_is_delivered_once_for_the_current_revision(tmp_path):
    provider = Provider("test.play")
    runtime = await _start(tmp_path)
    try:
        runtime.publish(provider.package(tmp_path / "play"))
        ws = SimpleNamespace(closed=False)
        await _frame(runtime, ws, "webui.dispatch", _dispatch("application.splash", "ui.ready", "test.play", READY))
        target = RenderTarget(session_id="", slot_id="application.splash", package_id="test.play", instance_key="main",
                              scope="application", connection_id="browser-1")
        revision = runtime.registry.get(target).revision

        for payload in ({"reason": "complete", "revision": revision + 1}, {"reason": "bored", "revision": revision},
                        {"reason": "skip", "revision": True}, {"reason": "skip"}):
            _, response = await _frame(runtime, ws, "webui.dispatch",
                                       _dispatch("application.splash", "ui.closed", "test.play", payload))
            assert response["payload"]["accepted"] is False, payload
        _, response = await _frame(runtime, ws, "webui.dispatch",
                                   _dispatch("application.splash", "ui.closed", "test.play", {"reason": "skip", "revision": revision}))
        assert response["payload"] == {"accepted": True, "status": "closed"}
        assert provider.closed == [{"reason": "skip"}]
        assert runtime.registry.get(target) is None
        _, response = await _frame(runtime, ws, "webui.dispatch",
                                   _dispatch("application.splash", "ui.closed", "test.play", {"reason": "skip", "revision": revision}))
        assert response["payload"] == {"accepted": False, "status": "inactive", "code": "TARGET_INACTIVE"}
        assert len(provider.closed) == 1
    finally:
        await runtime.shutdown()


async def test_slow_ready_handlers_time_out(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_module, "PLATFORM_TIMEOUT_S", 0.05)
    provider = Provider("test.slow", mode="stall")
    runtime = await _start(tmp_path)
    try:
        runtime.publish(provider.package(tmp_path / "slow"))
        _, response = await _frame(runtime, SimpleNamespace(closed=False), "webui.dispatch",
                                   _dispatch("application.splash", "ui.ready", "test.slow", READY))
        assert response["payload"] == {"accepted": False, "status": "timeout"}
    finally:
        await runtime.shutdown()
