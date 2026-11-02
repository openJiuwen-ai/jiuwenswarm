from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.events import OPS_BUILD_EVENT_TYPE, Event, ExtensionEventBus, HandlerLevel

from jiuwenswarm.extensions_v2.webui.action_validation import validate_upstream
from jiuwenswarm.extensions_v2.webui.component_rules import COMPONENT_RULES, validate_event_data, validate_props
from jiuwenswarm.extensions_v2.webui.constants import SESSION_SLOTS, UPSTREAM_EVENTS
from jiuwenswarm.extensions_v2.webui.models import (
    RenderFrame, RenderMode, RenderNode, RenderTarget, UiActionInput, UiRenderInput, node_to_payload,
)
from jiuwenswarm.extensions_v2.webui.registry import ActiveRenderRegistry
from jiuwenswarm.extensions_v2.webui.runtime import WebUIRuntime
from jiuwenswarm.extensions_v2.webui.schema import validate_request, validate_root, validate_target

TARGET = RenderTarget("s1", "conversation.input.below", "pkg.demo", "main")


def _button(*, args=None, disabled=False):
    return RenderNode("Button", {"label": "Refresh", "disabled": disabled}, events={
        "ui.click": {"id": "refresh", "args": {} if args is None else args},
    })


def _action(**changes):
    payload = {"revision": 1, "node_key": "0", "action_id": "refresh", "args": {}, "data": {}}
    payload.update(changes)
    return UiActionInput("ui.click", TARGET, payload)


async def _registered(root):
    registry = ActiveRenderRegistry()
    async with registry.claim(TARGET) as claim:
        registry.commit(claim.instance, UiRenderInput(TARGET, root))
    return registry


@pytest.mark.parametrize("field,value", [
    ("session_id", 1), ("session_id", ""), ("slot_id", "application.entry"),
    ("package_id", "a|b"), ("instance_key", " trailing "), ("instance_key", "x" * 201),
])
def test_target_rejects_invalid_identity(field, value):
    assert not validate_target(replace(TARGET, **{field: value})).ok


def test_target_accepts_every_registered_slot():
    for slot_id in SESSION_SLOTS:
        assert validate_target(replace(TARGET, slot_id=slot_id)).ok, slot_id


@pytest.mark.asyncio
async def test_event_for_static_node_is_rejected_without_raising():
    root = RenderNode("Text", {"value": "static"})
    registry = await _registered(root)
    receipt = validate_upstream(_action(), registry)
    assert receipt is not None and receipt.accepted is False


@pytest.mark.parametrize("root", [
    RenderNode("Input", {"value": 1}), RenderNode("Input", {"maxLength": 1, "value": "😀"}),
    RenderNode("Input", {"maxLength": True}), RenderNode("Box", {"padding": "sm"}),
    RenderNode("Text", {"value": None}), RenderNode("Switch", {"checked": None}),
    RenderNode("Select", {"options": [{"value": "x", "label": "A"}, {"value": 1, "label": "B"}]}),
    RenderNode("Slider", {"step": 3}), RenderNode("Slider", {"value": float("nan")}),
    RenderNode("Slider", {"value": 10**400}),
    RenderNode("Image", {"src": "javascript:alert(1)"}), RenderNode("Iframe", {"title": "X", "src": "///example.com"}),
    RenderNode("Button", events={"ui.change": {"id": "bad"}}),
    RenderNode("Button", events={"ui.click": {"id": "bad", "args": None}}),
    RenderNode("Button", events={"ui.click": {"id": "bad", "args": {"bad": float("inf")}}}),
    RenderNode("Button", events={"ui.click": {"id": "bad", "args": {"bad": 10**400}}}),
    RenderNode("Button", events={"ui.click": {"id": "bad", "extra": 1}}),
    RenderNode("Text", events={"ui.click": {"id": "bad"}}), RenderNode("Button", events={}),
    RenderNode("Row", {"gap": "sm", "density": "compact"}),
    RenderNode("FileTree", {"treeData": [
        {"key": "a", "title": "a", "path": "../a", "kind": "file"},
    ]}),
    RenderNode("FileTree", {"treeData": [
        {"key": "a", "title": "a", "path": "a", "kind": "file", "loaded": False},
    ]}),
    RenderNode("Box", children=(RenderNode("Panel", {"title": "bad"}, children=(RenderNode("Text"),)),)),
    RenderNode("Tabs", {"items": [{"key": "a", "label": "A", "children": [{"component": "Text"}]}]}),
    RenderNode("Divider", children=(RenderNode("Button"),)), RenderNode("Panel", {"title": "Empty"}),
])
def test_tree_rejects_invalid_component_contracts(root):
    assert not validate_root(root).ok


def test_request_enforces_slot_root_components():
    panel = RenderNode("Panel", {"title": "Files"}, children=(RenderNode("Text", {"value": "body"}),))
    iframe = RenderNode("Iframe", {"title": "Web", "src": "https://example.com"})
    text = RenderNode("Text", {"value": "body"})
    right = replace(TARGET, slot_id="conversation.panel.right")
    menu_main = replace(TARGET, slot_id="application.menu.main")

    assert validate_request(UiRenderInput(right, panel)).ok
    assert validate_request(UiRenderInput(right, iframe)).ok
    assert not validate_request(UiRenderInput(right, text)).ok
    assert validate_request(UiRenderInput(menu_main, iframe)).ok
    assert validate_request(UiRenderInput(menu_main, text)).ok
    assert not validate_request(UiRenderInput(menu_main, panel)).ok
    assert not validate_request(UiRenderInput(TARGET, panel)).ok
    assert not validate_request(UiRenderInput(TARGET, iframe)).ok


def test_tab_content_serialization_and_hidden_node_budget():
    root = RenderNode("Tabs", {"items": [{"key": "a", "label": "A", "children": [_button()]}]})
    assert validate_root(root).ok
    payload = node_to_payload(root)
    assert payload["props"]["items"][0]["children"][0]["events"] == {"ui.click": {"id": "refresh", "args": {}}}
    hidden = RenderNode("Tabs", {"items": [
        {"key": str(i), "label": "Hidden", "children": [RenderNode("Text")] * 100} for i in range(6)
    ]})
    assert not validate_root(hidden).ok
    deep = RenderNode("Tabs", {"items": [{"key": "a", "label": "A", "children": [RenderNode("Unknown")]}]})
    assert not validate_root(deep).ok


def test_tree_select_accepts_eight_levels_and_rejects_nine():
    items = [{"value": "leaf", "title": "Leaf"}]
    for i in range(7):
        items = [{"value": str(i), "title": "Parent", "children": items}]
    assert validate_root(RenderNode("TreeSelect", {"treeData": items})).ok
    assert not validate_root(RenderNode("TreeSelect", {"treeData": [{"value": "ninth", "title": "Ninth", "children": items}]})).ok


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"revision": 0}, {"revision": True}, {"node_key": "0.0"}, {"node_key": "0.00"},
    {"node_key": "0.__proto__"}, {"node_key": "0." + "9" * 5000}, {"node_key": 0},
    {"action_id": "forged"}, {"args": {"forged": 1}}, {"args": None},
    {"data": {"unexpected": True}}, {"data": None}, {"data": {"bad": float("nan")}},
    {"extra": True}, {"action_id": 1},
])
async def test_rejected_interactions_never_enter_business_handler(changes):
    bus = ExtensionEventBus()
    registry = await _registered(_button())
    runtime = WebUIRuntime(bus=bus, transport=AsyncMock(), registry=registry)
    runtime.register_events()
    observed = []

    async def side_effect(event, ops, next_handler):
        observed.append(event.event_input)
        return await next_handler(event)

    bus.register_event_handler("ui.click", side_effect, {}, HandlerLevel.USER)
    await bus.trigger(Event(OPS_BUILD_EVENT_TYPE, None))
    output = await runtime.dispatch(_action(**changes))
    assert output is not None and not output.accepted
    assert observed == []
    valid = await runtime.dispatch(_action())
    assert valid is not None and valid.accepted
    assert len(observed) == 1


@pytest.mark.asyncio
async def test_ready_needs_no_instance_but_rejects_business_payload():
    runtime = WebUIRuntime(bus=ExtensionEventBus(), transport=AsyncMock())
    runtime.register_events()
    await runtime.bus.trigger(Event(OPS_BUILD_EVENT_TYPE, None))
    first = await runtime.dispatch(UiActionInput("ui.ready", RenderTarget("s1", TARGET.slot_id)))
    assert first.accepted and first.status == "ready"
    invalid = await runtime.dispatch(UiActionInput("ui.ready", TARGET, {"action_id": "forged"}))
    assert not invalid.accepted


@pytest.mark.asyncio
async def test_dispatch_resolves_tab_contents_and_checks_identity_event_and_revision():
    registry = await _registered(RenderNode("Tabs", {"items": [{"key": "a", "label": "A", "children": [_button()]}]}))
    action = _action(node_key="0.items.0.children.0")
    assert validate_upstream(action, registry) is None
    for field in ("session_id", "package_id", "instance_key"):
        rejected = validate_upstream(replace(action, target=replace(TARGET, **{field: "other"})), registry)
        assert rejected is not None and not rejected.accepted
    assert validate_upstream(replace(action, event_type="ui.change"), registry) is not None
    registry.drop_instance(TARGET)
    assert validate_upstream(action, registry).status == "inactive"


@pytest.mark.asyncio
async def test_registry_keeps_delivered_event_declarations_after_plugin_mutation():
    args = {"enabled": True, "items": [1.0, {"code": "original"}]}
    root = _button(args=args)
    registry = await _registered(root)
    args["items"][1]["code"] = "mutated"
    root.events["ui.click"]["id"] = "mutated"
    original = {"enabled": True, "items": [1, {"code": "original"}]}
    assert validate_upstream(_action(args=original), registry) is None
    assert validate_upstream(_action(args={"enabled": 1, "items": [1, {"code": "original"}]}), registry) is not None
    assert validate_upstream(_action(action_id="mutated", args=args), registry) is not None
    instance = registry.get(TARGET)
    assert RenderFrame(TARGET, 1, RenderMode.REPLACE, instance.root).to_payload()["root"]["events"]["ui.click"]["id"] == "refresh"


@pytest.mark.asyncio
@pytest.mark.parametrize("component,props,event,data,accepted", [
    ("Input", {}, "ui.change", {"value": "x", "isComposing": False}, True),
    ("Input", {}, "ui.change", {"value": "x"}, False),
    ("Input", {"maxLength": 1}, "ui.press_enter", {"value": "😀"}, False),
    ("Input", {"readOnly": True}, "ui.change", {"value": "x", "isComposing": False}, False),
    ("Input", {"readOnly": True}, "ui.click", {"value": "x"}, True),
    ("Input", {"disabled": True}, "ui.click", {"value": "x"}, False),
    ("Switch", {}, "ui.change", {"checked": False}, True),
    ("Switch", {}, "ui.change", {"checked": 0}, False),
    ("Switch", {"loading": True}, "ui.change", {"checked": True}, False),
    ("Switch", {}, "ui.click", {"checked": True}, True),
    ("Switch", {}, "ui.click", {"checked": 0}, False),
    ("Switch", {"loading": True}, "ui.click", {"checked": True}, False),
    ("Slider", {"min": 0, "max": 10, "step": 2}, "ui.change", {"value": 4}, True),
    ("Slider", {"min": 0, "max": 10, "step": 2}, "ui.change", {"value": 3}, False),
    ("Slider", {}, "ui.change", {"value": True}, False),
    ("Select", {"options": [{"value": "a", "label": "A"}]}, "ui.select", {"value": "a"}, True),
    ("Select", {"options": []}, "ui.select", {"value": "forged"}, False),
    ("Select", {"options": [{"value": "a", "label": "A", "disabled": True}]}, "ui.select", {"value": "a"}, False),
    ("Select", {"options": []}, "ui.clear", {}, False),
    ("Select", {"options": [], "allowClear": True}, "ui.clear", {}, True),
    ("Select", {"options": [], "showSearch": False}, "ui.search", {"searchValue": "x"}, False),
    ("Select", {"options": [], "showSearch": True}, "ui.search", {"searchValue": "Beijing"}, True),
    ("Select", {"options": [], "mode": "tags"}, "ui.select", {"value": "custom"}, True),
    ("TreeSelect", {"treeData": [{"value": "a", "title": "A"}], "multiple": True}, "ui.change", {"value": ["a"]}, True),
    ("TreeSelect", {"treeData": [], "multiple": True}, "ui.change", {"value": ["forged"]}, False),
    ("Tabs", {"items": [{"key": "a", "label": "A"}]}, "ui.change", {"activeKey": "a"}, True),
    ("Tabs", {"items": [{"key": "a", "label": "A", "disabled": True}]}, "ui.change", {"activeKey": "a"}, False),
    ("Button", {"disabled": True}, "ui.click", {}, False),
])
async def test_component_event_data_gate(component, props, event, data, accepted):
    root = RenderNode(component, props, events={event: {"id": "refresh"}})
    assert validate_root(root).ok
    registry = await _registered(root)
    action = replace(_action(data=data), event_type=event)
    assert (validate_upstream(action, registry) is None) is accepted


@pytest.mark.asyncio
async def test_valid_handler_can_replace_and_reply_and_old_revision_is_rejected():
    bus = ExtensionEventBus()
    transport = AsyncMock()
    transport.send_frame.return_value = True
    runtime = WebUIRuntime(bus=bus, transport=transport)
    runtime.register_events()
    observed = []

    async def update(event, ops, next_handler):
        observed.append(event.event_input.payload["data"])
        await runtime.render(UiRenderInput(TARGET, _button()))
        return await next_handler(event)

    bus.register_event_handler("ui.click", update, {}, HandlerLevel.USER)
    await bus.trigger(Event(OPS_BUILD_EVENT_TYPE, None))
    await runtime.render(UiRenderInput(TARGET, _button()))
    receipt = await runtime.dispatch(_action())
    assert receipt.accepted and observed == [{}]
    assert transport.send_frame.call_count == 2
    assert runtime.registry.get(TARGET).revision == 2
    assert (await runtime.dispatch(_action())).status == "stale"
    await runtime.drop_package(TARGET.package_id)
    assert (await runtime.dispatch(_action(revision=2))).status == "inactive"


def test_component_event_catalog_excludes_deferred_features():
    assert len(COMPONENT_RULES) == 20
    assert {"Avatar", "FileTree", "FilePreview", "Dropdown"} <= COMPONENT_RULES.keys()
    assert {name for rule in COMPONENT_RULES.values() for name in rule.get("events", {})} <= UPSTREAM_EVENTS


@pytest.mark.asyncio
async def test_application_entry_ready_accepts_first_visit_reason():
    runtime = WebUIRuntime(bus=ExtensionEventBus(), transport=AsyncMock())
    runtime.register_events()
    await runtime.bus.trigger(Event(OPS_BUILD_EVENT_TYPE, None))
    target = RenderTarget("", "application.entry", scope="application", connection_id="sock-1")
    first = await runtime.dispatch(
        UiActionInput("ui.ready", target, {"reason": "first_visit"}),
        connection_id="sock-1",
    )
    assert first.accepted and first.status == "ready"
    forged = await runtime.dispatch(
        UiActionInput("ui.ready", target, {"reason": "other"}),
        connection_id="sock-1",
    )
    assert not forged.accepted


@pytest.mark.parametrize("root", [
    RenderNode("Card", {"tone": "info", "padding": "md", "density": "compact"}),
    RenderNode("Divider", {"axis": "horizontal", "spacing": "sm"}),
    RenderNode("Tabs", {"items": [{"key": "a", "label": "A"}], "selected": "a"}, children=(RenderNode("Text"),)),
    RenderNode("Tabs", {"items": [{"key": "a", "label": "A"}]}, children=(RenderNode("Text"),)),
    RenderNode("Slider", {"name": "amount", "value": 50}),
])
def test_legacy_component_props_are_rejected(root):
    assert not validate_root(root).ok


def test_file_tree_accepts_a_bounded_snapshot():
    root = RenderNode("FileTree", {
        "treeData": [{
            "key": "src", "title": "src", "path": "src", "kind": "directory", "loaded": True,
            "children": [{
                "key": "src/main.ts", "title": "main.ts", "path": "src/main.ts", "kind": "file",
            }],
        }],
        "expandedKeys": ["src"], "selectedKey": "src/main.ts", "status": "ready",
    }, events={"ui.select": {"id": "open"}})

    assert validate_root(root).ok


def test_file_tree_event_data_requires_known_nodes():
    props = {
        "treeData": [
            {"key": "src", "title": "src", "path": "src", "kind": "directory", "loaded": True,
             "children": [{"key": "src/main.ts", "title": "main.ts", "path": "src/main.ts", "kind": "file"}]},
        ],
    }

    assert validate_event_data("FileTree", "ui.select", {"key": "src/main.ts"}, props)
    assert not validate_event_data("FileTree", "ui.select", {"key": "src"}, props)
    assert validate_event_data("FileTree", "ui.change", {"key": "src", "expanded": True, "expandedKeys": ["src"]}, props)
    assert not validate_event_data("FileTree", "ui.change", {"key": "src", "expanded": False, "expandedKeys": ["src"]}, props)


def test_file_preview_accepts_plugin_supplied_content_and_rejects_bad_paths():
    good = RenderNode("FilePreview", {
        "file": {
            "name": "main.ts", "path": "src/main.ts", "mimeType": "text/plain",
            "size": 3, "content": "abc",
        },
    })
    assert validate_root(good).ok
    assert validate_root(RenderNode("FilePreview", {"file": None})).ok
    assert not validate_root(RenderNode("FilePreview", {
        "file": {"name": "main.ts", "path": "../main.ts", "mimeType": "text/plain", "size": 3, "content": "abc"},
    })).ok


_CASES = json.loads((Path(__file__).resolve().parents[4] / "jiuwenswarm/channels/web/frontend/tests/fixtures/webuiComponentCases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name,props", _CASES["valid"])
def test_shared_browser_valid_property_cases(name, props):
    assert validate_props(name, props) is None


@pytest.mark.parametrize("name,props", _CASES["invalid"])
def test_shared_browser_invalid_property_cases(name, props):
    assert validate_props(name, props) is not None


_EVENT_CASES = json.loads((Path(__file__).resolve().parents[4] / "jiuwenswarm/channels/web/frontend/tests/fixtures/webuiEventCases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", _EVENT_CASES)
def test_shared_browser_event_cases(case):
    assert validate_event_data(case["component"], case["event"], case["data"], case["props"]) is case["allowed"]
