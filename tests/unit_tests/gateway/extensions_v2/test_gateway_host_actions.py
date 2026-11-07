# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host actions run as event chains: ui.appearance, ui.download and ui.splash.preview (S_01 section 4)."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from openjiuwen.core.events import Event, SessionContext

from jiuwenswarm.gateway.extensions_v2.config import GatewayExtensionConfig
from jiuwenswarm.gateway.extensions_v2.loader import LoadedPackage
from jiuwenswarm.gateway.extensions_v2.models import EVENT_UI_APPEARANCE
from jiuwenswarm.gateway.extensions_v2.registry import HandlerDeclaration
from jiuwenswarm.gateway.extensions_v2.runtime import GatewayRuntime
from jiuwenswarm.gateway.extensions_v2.webui.models import HostActionOutput, UiActionOutput, UiAppearanceInput

THEME = "test.theme"
UI = {"apiVersion": 1, "items": [
    {"type": "web.slot.contribution", "slot": "application.background"},
    {"type": "web.slot.contribution", "slot": "application.splash"},
]}
ACTIONS = {
    "ui.appearance": (lambda ops: ops.appearance.apply({"accent": "#b45309"}), "webui.appearance"),
    "ui.download": (lambda ops: ops.ui.download("exports/backup.zip"), "webui.download"),
    "ui.splash.preview": (lambda ops: ops.splash.preview(), "webui.splash.play"),
}


def _channel():
    events, sockets = [], {}

    async def ensure_ws_id(ws):
        ws._jiuwen_ws_id = "browser-1"
        sockets[ws._jiuwen_ws_id] = ws
        return ws._jiuwen_ws_id

    async def send_event(ws, event, payload):
        events.append((event, payload))

    async def send_response(ws, req_id, *, ok, payload=None, error=None, code=None):
        return None

    return SimpleNamespace(channel_id="web", events=events, ensure_ws_id=ensure_ws_id, connection_user_id=lambda ws: None,
                           ws_by_id=sockets.get, send_event=send_event, send_response=send_response)


class Theme:
    """Runs one action from its ready handler and keeps the answer."""

    def __init__(self, action) -> None:
        self.action = action
        self.answers: list = []

    async def ready(self, event, ops, next_handler):
        try:
            self.answers.append(await self.action(ops))
        except ValueError as exc:
            self.answers.append(exc)
        return UiActionOutput(accepted=True, status="ready")

    def package(self, root: Path) -> LoadedPackage:
        root.mkdir(parents=True, exist_ok=True)
        return LoadedPackage(package_id=THEME, version="1.0.0", root=root, client={}, ui=UI,
                             handlers=(HandlerDeclaration("ui.ready", self.ready, {"target.package_id": THEME}, "ready"),))


def _policy(event_name: str, *, deny: bool, seen: list) -> LoadedPackage:
    """Another package watching one host action; it denies or passes the call on."""

    async def watch(event, ops, next_handler):
        seen.append(event.event_input.package_id)
        if deny:
            return HostActionOutput(status="denied", code="POLICY_DENIED")
        return await next_handler(event)

    return LoadedPackage(package_id="test.policy", version="1.0.0", root=Path("."), client={}, ui={},
                         handlers=(HandlerDeclaration(event_name, watch, {}, "watch"),))


async def _start(tmp_path: Path) -> GatewayRuntime:
    runtime = GatewayRuntime(channel=_channel(), config=GatewayExtensionConfig(
        install_dir=tmp_path / "install", data_dir=tmp_path / "data"))
    await runtime.start()
    return runtime


async def _ready(runtime: GatewayRuntime) -> None:
    visited, _ = await runtime.intercept(SimpleNamespace(closed=False), "ready", "webui.dispatch", {
        "event": "ui.ready", "payload": {},
        "target": {"scope": "application", "slot_id": "application.background", "instance_key": "main",
                   "package_id": THEME},
    })
    assert visited is True


def _prepare(runtime: GatewayRuntime) -> None:
    exports = runtime.package_data_dir(THEME) / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    (exports / "backup.zip").write_bytes(b"backup")


@pytest.mark.parametrize("event_name", sorted(ACTIONS))
async def test_a_user_policy_can_deny_a_host_action(tmp_path, event_name):
    action, frame = ACTIONS[event_name]
    theme, seen = Theme(action), []
    runtime = await _start(tmp_path)
    try:
        runtime.publish(theme.package(tmp_path / "theme"))
        runtime.publish(_policy(event_name, deny=True, seen=seen))
        _prepare(runtime)
        await _ready(runtime)
        assert seen == [THEME], "the policy sees which package asked"
        assert theme.answers == [{"status": "denied", "code": "POLICY_DENIED"}]
        assert [name for name, _ in runtime.channel.events if name == frame] == []
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize("event_name", sorted(ACTIONS))
async def test_a_host_action_passes_through_another_packages_handler(tmp_path, event_name):
    """The CORE checks the caller of the Ops method, not the scope of whichever handler forwarded the call."""
    action, frame = ACTIONS[event_name]
    theme, seen = Theme(action), []
    runtime = await _start(tmp_path)
    try:
        runtime.publish(theme.package(tmp_path / "theme"))
        runtime.publish(_policy(event_name, deny=False, seen=seen))
        _prepare(runtime)
        await _ready(runtime)
        assert seen == [THEME]
        [answer] = theme.answers
        assert isinstance(answer, dict) and answer["status"] in {"applied", "offered", "requested"}, answer
        assert [name for name, _ in runtime.channel.events if name == frame] == [frame]
    finally:
        await runtime.shutdown()


async def test_the_core_rejects_a_chain_started_without_a_caller(tmp_path):
    runtime = await _start(tmp_path)
    try:
        runtime.publish(Theme(ACTIONS["ui.appearance"][0]).package(tmp_path / "theme"))
        context = SessionContext("gateway:test", metadata={"connection_id": "browser-1"})
        answer = await runtime.bus.trigger(Event(EVENT_UI_APPEARANCE, UiAppearanceInput(THEME, {"accent": "#b45309"})),
                                           context)
        assert answer.status == "rejected" and answer.code == "INVALID_ACTION"
        assert runtime.channel.events == []
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize("event_name", sorted(ACTIONS))
async def test_a_handler_cannot_rewrite_whose_action_it_is(tmp_path, event_name):
    """A USER handler that renames the caller in the input is refused; the caller travels out of band."""
    action, frame = ACTIONS[event_name]
    theme = Theme(action)

    async def rewrite(event, ops, next_handler):
        return await next_handler(Event(event.event_type, replace(event.event_input, package_id="test.other")))

    other = LoadedPackage(package_id="test.other", version="1.0.0", root=tmp_path, client={}, ui=UI,
                          handlers=(HandlerDeclaration(event_name, rewrite, {}, "rewrite"),))
    runtime = await _start(tmp_path)
    try:
        runtime.publish(theme.package(tmp_path / "theme"))
        runtime.publish(other)
        _prepare(runtime)
        await _ready(runtime)
        [answer] = theme.answers
        assert isinstance(answer, ValueError) and "does not belong to its caller" in str(answer)
        assert [name for name, _ in runtime.channel.events if name == frame] == []
    finally:
        await runtime.shutdown()


async def test_a_chain_started_inside_a_host_action_does_not_inherit_its_caller(tmp_path):
    """The caller is the chain's trigger_context: a nested chain started without one acts for nobody."""
    theme, nested = Theme(ACTIONS["ui.appearance"][0]), []
    runtime = await _start(tmp_path)

    async def nest(event, ops, next_handler):
        nested.append(await runtime.bus.trigger(Event(EVENT_UI_APPEARANCE, UiAppearanceInput(
            THEME, {"accent": "#0f766e"})), ops.session_context))
        return await next_handler(event)

    try:
        runtime.publish(theme.package(tmp_path / "theme"))
        runtime.publish(LoadedPackage(package_id="test.policy", version="1.0.0", root=Path("."), client={}, ui={},
                                      handlers=(HandlerDeclaration(EVENT_UI_APPEARANCE, nest, {}, "nest"),)))
        await _ready(runtime)
        [outer] = theme.answers
        assert outer["status"] == "applied", "the outer call still acts for its caller"
        [inner] = nested
        assert inner.status == "rejected" and inner.code == "INVALID_ACTION"
        frames = [payload for name, payload in runtime.channel.events if name == "webui.appearance"]
        assert [frame["appearance"]["accent"] for frame in frames] == ["#b45309"]
    finally:
        await runtime.shutdown()


async def test_ops_of_a_released_activation_start_no_host_action_chain(tmp_path):
    """The activation is checked before the chain starts, so other packages never see a call that cannot succeed."""
    captured, seen = [], []

    async def keep(ops):
        captured.append(ops)
        return {"status": "kept"}

    theme = Theme(keep)
    runtime = await _start(tmp_path)
    try:
        runtime.publish(theme.package(tmp_path / "theme"))
        runtime.publish(_policy("ui.appearance", deny=False, seen=seen))
        await _ready(runtime)
        runtime.unpublish(THEME)
        with pytest.raises(ValueError, match="inactive activation"):
            await captured[0].appearance.apply({"accent": "#b45309"})
        assert seen == [], "no USER handler observed the dead call"
        assert [name for name, _ in runtime.channel.events if name == "webui.appearance"] == []
    finally:
        await runtime.shutdown()
