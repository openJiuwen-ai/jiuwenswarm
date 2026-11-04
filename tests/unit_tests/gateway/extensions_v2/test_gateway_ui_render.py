# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``ui.render`` event, CORE framing and ``ops.ui.render``."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.gateway.extensions_v2.config import GatewayExtensionConfig
from jiuwenswarm.gateway.extensions_v2.loader import LoadedPackage
from jiuwenswarm.gateway.extensions_v2.models import EVENT_GATEWAY_REQUEST, GatewayRequestOutput
from jiuwenswarm.gateway.extensions_v2.registry import HandlerDeclaration
from jiuwenswarm.gateway.extensions_v2.runtime import GatewayRuntime
from jiuwenswarm.gateway.extensions_v2.webui.models import (
    RenderMode,
    RenderNode,
    RenderTarget,
    UiRenderInput,
    UiRenderOutput,
)
from jiuwenswarm.gateway.extensions_v2.webui.schema import validate_root


def _contract_node(raw: dict) -> RenderNode:
    props = dict(raw.get("props", {}))
    content = "children" if raw["component"] == "Tabs" else "label" if raw["component"] == "Dropdown" else ""
    if content:
        props["items"] = [
            {**item, content: tuple(_contract_node(child) for child in item[content])}
            if isinstance(item.get(content), list) else dict(item)
            for item in props["items"]
        ]
    return RenderNode(
        component=raw["component"], props=props,
        children=tuple(_contract_node(child) for child in raw.get("children", [])), events=raw.get("events"),
    )


CONTRACT_CASES = json.loads((Path(__file__).parents[4] / (
    "jiuwenswarm/channels/web/frontend/tests/fixtures/webuiGatewayCases.json"
)).read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CONTRACT_CASES, ids=lambda case: case["name"])
def test_gateway_component_contract_matches_browser(case):
    assert validate_root(_contract_node(case["root"])).ok is case["valid"]


def test_dropdown_rejects_icon_properties():
    report = validate_root(RenderNode(
        component="Dropdown",
        props={"icon": "ellipsis", "ariaLabel": "Actions", "items": [{"key": "apply", "label": "Apply"}]},
        children=(RenderNode(component="Text", props={"value": "Actions"}),),
    ))
    assert not report.ok
    assert report.message == "props not allowed for Dropdown: ['icon']"

#: Header every authenticated frame carries; the gate only checks presence.
AUTH_HEADERS = {"authorization": "Bearer tk_1"}


class RenderChannel:
    """Stand-in for WebChannel that records downstream frames per socket."""

    channel_id = "web"

    def __init__(self) -> None:
        self.responses: list[dict] = []
        self.events: list[tuple[str, dict]] = []
        self._sockets: dict[str, SimpleNamespace] = {}
        self._ids: dict[int, str] = {}

    async def ensure_ws_id(self, ws) -> str:
        key = id(ws)
        if key not in self._ids:
            ws_id = f"ws-{len(self._ids) + 1}"
            self._ids[key] = ws_id
            self._sockets[ws_id] = ws
            setattr(ws, "_jiuwen_ws_id", ws_id)
        return self._ids[key]

    def ws_by_id(self, ws_id: str):
        return self._sockets.get(ws_id)

    def connection_user_id(self, ws):
        return getattr(ws, "_user", None)

    async def send_response(self, ws, req_id, *, ok, payload=None, error=None, code=None):
        self.responses.append(
            {"id": req_id, "ok": ok, "payload": payload or {}, "error": error, "code": code}
        )

    async def send_event(self, ws, event, payload):
        self.events.append((event, payload))


PANEL_TREE = RenderNode(
    component="Box",
    props={"direction": "column", "gap": "md"},
    children=(
        RenderNode(component="Text", props={"value": "Panel"}),
        RenderNode(component="Input", props={"placeholder": "name"}),
        RenderNode(
            component="Button",
            props={"label": "Send"},
            events={"ui.click": {"id": "demo.send"}},
        ),
    ),
)


def _ws() -> SimpleNamespace:
    return SimpleNamespace(closed=False)


def _package(handler) -> LoadedPackage:
    return LoadedPackage(
        package_id="demo",
        version="1.0.0",
        root=Path("/tmp/demo"),
        handlers=(HandlerDeclaration(EVENT_GATEWAY_REQUEST, handler, {}, "local-1"),),
        auth_provider=True,
    )


async def _runtime(tmp_path: Path) -> GatewayRuntime:
    runtime = GatewayRuntime(
        channel=RenderChannel(),
        config=GatewayExtensionConfig(install_dir=tmp_path),
    )
    await runtime.start()
    return runtime


async def _delegate(event, ops, next_handler):
    return await next_handler(event)


def _render_handler(record: dict):
    async def handler(event, ops, next_handler):
        record["output"] = await ops.ui.render(record["request"])
        return GatewayRequestOutput(handled=True, route="consume", response={})

    return handler


def _render_request(
    *,
    slot: str = "application.entry",
    root: RenderNode | None = None,
    mode: RenderMode = RenderMode.REPLACE,
) -> UiRenderInput:
    return UiRenderInput(
        target=RenderTarget("", slot, "demo", "panel"),
        root=PANEL_TREE if root is None else root,
        mode=mode,
    )


async def test_ops_ui_render_delivers_a_frame(tmp_path):
    runtime = await _runtime(tmp_path)
    record: dict = {"request": _render_request()}
    runtime.publish(_package(_render_handler(record)))
    ws = _ws()

    await runtime.intercept(ws, "r1", "chat.send", {}, AUTH_HEADERS)

    output = record["output"]
    assert isinstance(output, UiRenderOutput)
    assert output.status == "delivered"
    assert output.revision == 1

    event, payload = runtime.channel.events[0]
    assert event == "webui.render"
    assert payload["revision"] == 1
    assert payload["mode"] == "replace"
    assert payload["target"]["slot_id"] == "application.entry"
    assert payload["target"]["scope"] == "application"
    assert "connection_id" not in payload["target"]
    assert "session_id" not in payload["target"]
    assert payload["root"]["component"] == "Box"
    assert "activation_generation" not in payload, "ordinary UI packages do not require client declarations"


async def test_revision_increases_per_instance(tmp_path):
    runtime = await _runtime(tmp_path)
    record: dict = {"request": _render_request()}
    runtime.publish(_package(_render_handler(record)))
    ws = _ws()

    await runtime.intercept(ws, "r1", "chat.send", {}, AUTH_HEADERS)
    await runtime.intercept(ws, "r2", "chat.send", {}, AUTH_HEADERS)

    revisions = [payload["revision"] for _, payload in runtime.channel.events]
    assert revisions == [1, 2]


async def test_unknown_component_is_rejected(tmp_path):
    runtime = await _runtime(tmp_path)
    record: dict = {"request": _render_request(root=RenderNode(component="Script", props={"src": "x"}))}
    runtime.publish(_package(_render_handler(record)))

    await runtime.intercept(_ws(), "r1", "chat.send", {}, AUTH_HEADERS)

    assert record["output"].status == "rejected"
    event, payload = runtime.channel.events[0]
    assert event == "webui.render.error"
    assert payload["code"] == "SCHEMA_ERROR"


async def test_form_form_item_and_dropdown_are_in_the_catalog(tmp_path):
    runtime = await _runtime(tmp_path)
    root = RenderNode(
        component="Box",
        props={"direction": "column"},
        children=(
            RenderNode(
                component="Form",
                props={"name": "prefs", "layout": "vertical"},
                children=(
                    RenderNode(
                        component="Form.Item",
                        props={
                            "name": "nickname",
                            "label": "Nickname",
                            "rules": [{"required": True, "message": "required"}],
                        },
                        children=(RenderNode(component="Input", props={"placeholder": "nickname"}),),
                    ),
                ),
            ),
            RenderNode(
                component="Button",
                props={"label": "Save", "form": "prefs"},
                events={"ui.click": {"id": "demo.save"}},
            ),
            RenderNode(
                component="Dropdown",
                props={"items": [{"key": "logout", "label": "Logout"}]},
                children=(RenderNode(component="Text", props={"value": "User"}),),
            ),
        ),
    )
    record: dict = {"request": _render_request(root=root)}
    runtime.publish(_package(_render_handler(record)))

    await runtime.intercept(_ws(), "r1", "chat.send", {}, AUTH_HEADERS)

    assert record["output"].status == "delivered"


async def test_button_form_must_reference_a_form_in_the_tree(tmp_path):
    runtime = await _runtime(tmp_path)
    root = RenderNode(
        component="Box",
        children=(
            RenderNode(
                component="Button",
                props={"label": "Save", "form": "missing"},
                events={"ui.click": {"id": "demo.save"}},
            ),
        ),
    )
    record: dict = {"request": _render_request(root=root)}
    runtime.publish(_package(_render_handler(record)))

    await runtime.intercept(_ws(), "r1", "chat.send", {}, AUTH_HEADERS)

    assert record["output"].status == "rejected"
    assert runtime.channel.events[0][1]["code"] == "SCHEMA_ERROR"


async def test_slot_outside_the_gateway_catalog_is_rejected(tmp_path):
    runtime = await _runtime(tmp_path)
    record: dict = {"request": _render_request(slot="conversation.panel.right")}
    runtime.publish(_package(_render_handler(record)))

    await runtime.intercept(_ws(), "r1", "chat.send", {}, AUTH_HEADERS)

    assert record["output"].status == "rejected"
    assert record["output"].revision == 0


async def test_clear_mode_delivers_a_clear_frame_and_drops_the_instance(tmp_path):
    runtime = await _runtime(tmp_path)
    record: dict = {"request": _render_request()}
    runtime.publish(_package(_render_handler(record)))
    ws = _ws()

    await runtime.intercept(ws, "r1", "chat.send", {}, AUTH_HEADERS)
    record["request"] = UiRenderInput(
        target=RenderTarget("", "application.entry", "demo", "panel"),
        mode=RenderMode.CLEAR,
    )
    await runtime.intercept(ws, "r2", "chat.send", {}, AUTH_HEADERS)

    event, payload = runtime.channel.events[1]
    assert event == "webui.render.clear"
    assert payload["revision"] == 2
    assert runtime.registry.snapshot() == ()


async def test_only_the_target_connection_receives_the_frame(tmp_path):
    runtime = await _runtime(tmp_path)
    record: dict = {"request": _render_request()}
    runtime.publish(_package(_render_handler(record)))

    await runtime.intercept(_ws(), "r1", "chat.send", {}, AUTH_HEADERS)

    assert runtime.channel.ws_by_id("ws-1") is not None
    assert runtime.channel.ws_by_id("ws-2") is None
    assert len(runtime.channel.events) == 1


async def test_entry_can_reopen_after_clear_with_a_monotonic_revision(tmp_path):
    runtime = await _runtime(tmp_path)
    record = {"request": _render_request()}
    runtime.publish(_package(_render_handler(record)))
    ws = _ws()
    try:
        await runtime.intercept(ws, "open", "config.get", {}, AUTH_HEADERS)
        record["request"] = _render_request(mode=RenderMode.CLEAR)
        await runtime.intercept(ws, "clear", "config.get", {}, AUTH_HEADERS)
        assert runtime.registry.snapshot() == ()
        record["request"] = _render_request()
        await runtime.intercept(ws, "reopen", "config.get", {}, AUTH_HEADERS)
        assert [payload["revision"] for _, payload in runtime.channel.events] == [1, 2, 3]
        await runtime.on_disconnect(ws)
        assert runtime.registry.snapshot() == ()
        await runtime.intercept(ws, "new-lifecycle", "config.get", {}, AUTH_HEADERS)
        assert runtime.channel.events[-1][1]["revision"] == 1
    finally:
        await runtime.shutdown()


async def test_disconnect_drops_application_instances(tmp_path):
    runtime = await _runtime(tmp_path)
    record: dict = {"request": _render_request()}
    runtime.publish(_package(_render_handler(record)))
    ws = _ws()

    await runtime.intercept(ws, "r1", "chat.send", {}, AUTH_HEADERS)
    assert len(runtime.registry.snapshot()) == 1

    await runtime.on_disconnect(ws)

    assert runtime.registry.snapshot() == ()
