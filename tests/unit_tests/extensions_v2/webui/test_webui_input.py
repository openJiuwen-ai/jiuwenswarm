from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.events import OPS_BUILD_EVENT_TYPE, Event, ExtensionEventBus, HandlerLevel

import jiuwenswarm.extensions_v2.assembly as assembly
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions_v2.webui.adapter import WebUIDispatchAdapter
from jiuwenswarm.extensions_v2.webui.contributions import UiCatalog, UiContribution
from jiuwenswarm.extensions_v2.webui.models import RenderNode, RenderTarget, UiRenderInput
from jiuwenswarm.extensions_v2.webui.runtime import WebUIRuntime
from jiuwenswarm.extensions_v2.webui.transport import RenderTransport


TARGET = RenderTarget("s1", "conversation.input.above", "pkg.demo", "main")


def _input(props):
    return RenderNode("Input", props, events={
        "ui.change": {"id": "edit", "args": {"field": "city"}},
    })


def _request(data):
    return AgentRequest(
        request_id="edit-1",
        channel_id="web",
        session_id=TARGET.session_id,
        user_id="user",
        req_method=ReqMethod.WEBUI_DISPATCH,
        params={
            "event": "ui.change",
            "session_id": TARGET.session_id,
            "target": TARGET.to_payload(),
            "payload": {
                "node_key": "0", "revision": 1, "action_id": "edit",
                "args": {"field": "city"}, "data": data,
            },
        },
    )


async def _setup(monkeypatch, props):
    catalog = UiCatalog(items=(UiContribution(TARGET.package_id, "web.slot.contribution", "main", TARGET.slot_id),))
    push = SimpleNamespace(send_push=AsyncMock(return_value=True))
    runtime = WebUIRuntime(
        bus=ExtensionEventBus(),
        transport=RenderTransport(push=push),
        catalog=SimpleNamespace(current=lambda: catalog),
    )
    runtime.register_events()
    observed = []

    async def edit(event, ops, next_handler):
        data = event.event_input.payload["data"]
        observed.append((data, ops.session_context.metadata))
        if not data["isComposing"]:
            await ops.ui.render(UiRenderInput(TARGET, _input({"value": data["value"]})))
        return await next_handler(event)

    runtime.bus.register_event_handler("ui.change", edit, {}, HandlerLevel.USER)
    await runtime.bus.trigger(Event(OPS_BUILD_EVENT_TYPE, None))
    await runtime.render(UiRenderInput(TARGET, _input(props)))
    monkeypatch.setattr(assembly, "get_webui_runtime", lambda: runtime)
    return runtime, push, observed


@pytest.mark.asyncio
async def test_input_change_reaches_handler_ops_render_and_returns_correlated_receipt(monkeypatch):
    runtime, push, observed = await _setup(monkeypatch, {"value": "Initial"})
    for value in ("北", "北京"):
        draft = await WebUIDispatchAdapter().handle(_request({"value": value, "isComposing": True}))
        assert draft.ok and draft.payload["accepted"]
        assert runtime.registry.get(TARGET).revision == 1
    request = _request({"value": "北京", "isComposing": False})
    response = await WebUIDispatchAdapter().handle(request)
    assert response.ok
    assert response.request_id == request.request_id and response.channel_id == "web"
    assert response.payload == {"event": "ui.change", "accepted": True, "status": "handled"}
    assert [data for data, _ in observed] == [
        {"value": "北", "isComposing": True}, {"value": "北京", "isComposing": True},
        {"value": "北京", "isComposing": False},
    ]
    assert observed[-1][1] == {"user_id": "user", "channel_id": "web"}
    assert push.send_push.await_count == 2
    frame = push.send_push.call_args.args[0]
    assert frame["session_id"] == TARGET.session_id
    assert frame["payload"]["event_type"] == "webui.render"
    assert frame["payload"]["revision"] == 2
    assert frame["payload"]["root"]["props"]["value"] == "北京"
    stale = await WebUIDispatchAdapter().handle(request)
    assert stale.payload["status"] == "stale" and len(observed) == 3
    request.params["payload"].update(revision=2, data={"value": "", "isComposing": False})
    cleared = await WebUIDispatchAdapter().handle(request)
    assert cleared.payload["accepted"]
    assert runtime.registry.get(TARGET).root.props["value"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("props,section,changes,code", [
    ({"disabled": True}, "payload", {}, "SCHEMA_ERROR"),
    ({"readOnly": True}, "payload", {}, "SCHEMA_ERROR"),
    ({}, "payload", {"data": {"value": "x"}}, "SCHEMA_ERROR"),
    ({}, "payload", {"data": {"value": 1, "isComposing": False}}, "SCHEMA_ERROR"),
    ({}, "payload", {"data": {"value": "x", "isComposing": 0}}, "SCHEMA_ERROR"),
    ({}, "payload", {"data": {"value": "x", "isComposing": False, "extra": True}}, "SCHEMA_ERROR"),
    ({}, "payload", {"data": {"value": "北" * 6000, "isComposing": False}}, "SCHEMA_ERROR"),
    ({"maxLength": 1}, "payload", {"data": {"value": "😀", "isComposing": False}}, "SCHEMA_ERROR"),
    ({}, "payload", {"action_id": "forged"}, "SCHEMA_ERROR"),
    ({}, "payload", {"args": {"field": "other"}}, "SCHEMA_ERROR"),
    ({}, "payload", {"revision": 0}, "TARGET_INACTIVE"),
    ({}, "payload", {"node_key": "0.1"}, "TARGET_INACTIVE"),
    ({}, "params", {"event": "ui.press_enter"}, "SCHEMA_ERROR"),
    ({}, "params", {"session_id": "other"}, "SESSION_MISMATCH"),
    ({}, "target", {"package_id": "other"}, "NO_CONTRIBUTION"),
    ({}, "target", {"instance_key": "other"}, "TARGET_INACTIVE"),
])
async def test_invalid_input_changes_never_reach_business_handler(monkeypatch, props, section, changes, code):
    runtime, push, observed = await _setup(monkeypatch, props)
    request = _request({"value": "x", "isComposing": False})
    selected = request.params if section == "params" else request.params[section]
    selected.update(deepcopy(changes))
    response = await WebUIDispatchAdapter().handle(request)
    assert response.payload["code"] == code
    assert observed == []
    assert push.send_push.await_count == 1
    assert runtime.registry.get(TARGET).revision == 1
