"""Workbench round menus preserve per-request snapshots and reject unknown selections."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.events import Event, ExtensionEventBus, SessionContext

from jiuwenswarm.extensions_v2.event_types.context_usage import (
    CONTEXT_USAGE_EVENT,
    ContextUsageOutput,
    ContextUsageUpdate,
    register_context_usage_events,
)
from jiuwenswarm.extensions_v2.management import ExtensionManager
from jiuwenswarm.extensions_v2.webui.contributions import WebUICatalogService
from jiuwenswarm.extensions_v2.webui.runtime import WebUIRuntime
from jiuwenswarm.extensions_v2.examples.webui_workbench_v2.webui_workbench_v2.handlers import WorkbenchHandlers
from jiuwenswarm.extensions_v2.examples.webui_workbench_v2.webui_workbench_v2.state import SessionState
from jiuwenswarm.extensions_v2.examples.webui_workbench_v2.webui_workbench_v2.views import (
    TURN_SELECT_ACTION,
    USER_LOGIN_ACTION,
    USER_SELECT_ACTION,
    build_view,
)
from jiuwenswarm.extensions_v2.webui.models import RenderTarget, UiActionInput, UiRenderOutput
from jiuwenswarm.extensions_v2.webui.schema import validate_root


def test_workbench_header_declares_its_refresh_icon_and_text_variant() -> None:
    root = build_view("conversation.header.actions", SessionState(), "workbench")
    assert validate_root(root).ok
    assert root.component == "Button"
    assert root.props == {
        "type": "text", "size": "md",
        "icon": "refresh", "aria-label": "刷新工作台 / Refresh workbench",
    }
    assert root.events["ui.click"] == {"id": "workbench.refresh", "args": {}}


@pytest.mark.asyncio
async def test_workbench_user_menu_combines_profile_logout_and_login_per_connection() -> None:
    handler = WorkbenchHandlers()
    render = AsyncMock(return_value=UiRenderOutput(status="delivered"))
    ops = SimpleNamespace(ui=SimpleNamespace(render=render))
    next_handler = AsyncMock()
    target = RenderTarget("", "application.sidebar.footer", "webui.workbench-v2", "user-menu",
                          connection_id="browser-a")

    async def send(event_type: str, payload: dict | None = None, owner: RenderTarget = target):
        request = UiActionInput(event_type, owner, payload or {})
        return await handler.on_event(Event(event_type, request), ops, next_handler)

    assert (await send("ui.ready")).accepted
    menu = render.call_args.args[0].root
    assert validate_root(menu).ok
    assert [item["key"] for item in menu.props["items"]] == ["profile", "logout"]
    assert menu.children[0].props["src"] == "/logo.svg"
    render.reset_mock()
    result = await send("ui.select", {"action_id": USER_SELECT_ACTION, "data": {"key": "profile"}})
    assert result.accepted and result.status == "selected"
    render.assert_not_awaited()
    assert handler._signed_in == {}
    assert not (await send("ui.select", {"action_id": USER_SELECT_ACTION, "data": {"key": "unknown"}})).accepted
    assert (await send("ui.select", {"action_id": USER_SELECT_ACTION, "data": {"key": "logout"}})).accepted
    signed_out = render.call_args.args[0].root
    assert validate_root(signed_out).ok
    assert signed_out.component == "Button"
    assert signed_out.props["value"] == "登录 / Sign in"
    assert (await send("ui.ready")).accepted
    assert render.call_args.args[0].root.component == "Button"
    assert (await send("ui.ready", owner=replace(target, connection_id="browser-b"))).accepted
    assert render.call_args.args[0].root.component == "Dropdown"
    assert (await send("ui.click", {"action_id": USER_LOGIN_ACTION})).accepted
    assert render.call_args.args[0].root.component == "Dropdown"
    next_handler.assert_not_awaited()


def test_workbench_round_menu_places_a_symbol_button_before_the_icon_trigger() -> None:
    root = build_view("conversation.turn.footer.actions", SessionState(), "request-1")
    assert validate_root(root).ok
    assert root.component == "Box"
    assert root.props["direction"] == "row"
    assert root.props["wrap"] is False
    button, dropdown = root.children
    assert button.component == "Button"
    assert button.props == {"value": "👍", "type": "text", "size": "md"}
    assert not button.events
    assert dropdown.component == "Dropdown"
    assert dropdown.props["icon"] == "ellipsis"
    assert dropdown.props["ariaLabel"] == "本轮操作 / Round actions"
    assert [item["key"] for item in dropdown.props["items"]] == ["refresh", "details"]
    assert dropdown.events["ui.select"]["id"] == TURN_SELECT_ACTION


@pytest.mark.parametrize("key", ["refresh", "details"])
def test_workbench_round_selection_only_updates_the_addressed_round(key: str) -> None:
    state = SessionState()
    current = state.turn("request-1")
    other = state.turn("request-2")
    previous = (other.summary, other.result, other.refreshes)
    state.query = "协议"
    request = UiActionInput(
        event_type="ui.select",
        target=RenderTarget(session_id="session-1", slot_id="conversation.turn.footer.actions",
                            package_id="webui.workbench-v2", instance_key="request-1"),
        payload={"action_id": TURN_SELECT_ACTION, "data": {"key": key}},
    )
    assert WorkbenchHandlers._apply(request, state) == "updated"
    if key == "refresh":
        assert current.refreshes == 1
        assert current.summary == state.summary()
    else:
        assert current.result == f"request-1 · {current.summary}"
    assert (other.summary, other.result, other.refreshes) == previous
    root = build_view("conversation.session.content.after", state, "request-1")
    assert validate_root(root).ok
    assert root.component == "Box"
    assert [node.component for node in root.children] == ["Button", "Text"]
    assert root.children[0].props["value"] == current.usage_label()
    assert root.children[1].props["value"] == current.result
    other_root = build_view("conversation.session.content.after", state, "request-2")
    assert other_root.component == "Button"
    assert other_root.props["value"] == other.usage_label()


@pytest.mark.asyncio
async def test_workbench_ready_restores_snapshot_and_round_action_publishes_visible_result() -> None:
    handler = WorkbenchHandlers()
    render = AsyncMock(return_value=UiRenderOutput(status="delivered"))
    ops = SimpleNamespace(ui=SimpleNamespace(render=render), store=SimpleNamespace(get=AsyncMock(return_value=None)))
    next_handler = AsyncMock()
    target = RenderTarget("session-1", "conversation.session.content.after", "webui.workbench-v2", "request-1")

    async def ready(render_target: RenderTarget) -> None:
        request = UiActionInput("ui.ready", render_target)
        result = await handler.on_event(Event("ui.ready", request), ops, next_handler)
        assert result.accepted

    await ready(target)
    original = render.call_args.args[0].root
    assert original.component == "Button"
    assert original.props["value"] == "用量：未上报 / Usage: not reported"
    assert not original.events
    state = handler._sessions["session-1"]
    state.query = "协议"
    state.last_result = "Later session result"
    other_round = replace(target, instance_key="request-2")
    await ready(other_round)
    other_snapshot = render.call_args.args[0].root
    await ready(replace(target, session_id="session-2"))
    await ready(target)
    assert render.call_args.args[0].root == original

    menu_target = replace(target, slot_id="conversation.turn.footer.actions")
    await ready(menu_target)
    render.reset_mock()
    request = UiActionInput("ui.select", menu_target, {
        "action_id": TURN_SELECT_ACTION, "data": {"key": "refresh"},
    })
    output = await handler.on_event(Event("ui.select", request), ops, next_handler)
    assert output.accepted
    requests = [call.args[0] for call in render.call_args_list]
    assert {item.target for item in requests} == {target, menu_target}
    snapshot = next(item.root for item in requests if item.target == target)
    assert validate_root(snapshot).ok
    assert snapshot.children[0].props["value"] == state.turn("request-1").usage_label()
    assert snapshot.children[1].props["value"] == "本轮刷新 / Round refresh: 1"
    await ready(other_round)
    assert render.call_args.args[0].root == other_snapshot
    next_handler.assert_not_awaited()


@pytest.mark.parametrize("total,label", [
    (None, "用量：未上报 / Usage: not reported"),
    (0, "用量：0 tokens"),
    (82, "用量：82 tokens"),
    (1_250, "用量：1.2k tokens"),
    (22_800_000, "用量：22.8M tokens"),
])
def test_workbench_usage_is_read_only_and_formats_reported_total(total, label: str) -> None:
    state = SessionState()
    state.turn("request-1").total_tokens = total
    root = build_view("conversation.session.content.after", state, "request-1")
    assert validate_root(root).ok
    assert root.component == "Button"
    assert root.props == {"value": label, "type": "text", "size": "md", "icon": "database"}
    assert not root.events


@pytest.mark.asyncio
@pytest.mark.parametrize("notify_first", [True, False])
async def test_workbench_usage_notification_and_ready_order_preserves_round_isolation(notify_first: bool) -> None:
    handler = WorkbenchHandlers()
    assert (CONTEXT_USAGE_EVENT, handler.on_usage, {}) in handler.handlers()
    render = AsyncMock(return_value=UiRenderOutput(status="delivered"))
    usage = AsyncMock(return_value=None)
    ops = SimpleNamespace(
        ui=SimpleNamespace(render=render), store=SimpleNamespace(get=usage, set=AsyncMock(return_value=True)),
        session_context=SimpleNamespace(session_id="session-1"),
    )
    receipt = ContextUsageOutput(accepted=True, request_id="request-1")
    next_handler = AsyncMock(return_value=receipt)
    target = RenderTarget("session-1", "conversation.session.content.after", "webui.workbench-v2", "request-1")
    request = UiActionInput("ui.ready", target)
    # The notification owns the round total; session.usage exposes context occupancy.
    event = Event(CONTEXT_USAGE_EVENT, ContextUsageUpdate("session-1", "request-1", total_tokens=22_800_000))
    handler._sessions["session-2"] = SessionState()
    handler._sessions["session-2"].turn("request-1").total_tokens = 55
    handler._sessions["session-1"] = SessionState()
    handler._sessions["session-1"].turn("request-2").total_tokens = 77

    async def notify() -> None:
        usage.return_value = {
            "session_id": "session-1", "turn": {"request_id": "request-1", "total_tokens": 22_800_000},
        }
        assert await handler.on_usage(event, ops, next_handler) is receipt

    if notify_first:
        await notify()
        render.assert_not_awaited()
        # Ready must retain the cached total even if history still reports the prior round.
        usage.return_value = {"session_id": "session-1", "turn": {"request_id": "old", "total_tokens": 1}}
    await handler.on_event(Event("ui.ready", request), ops, next_handler)
    if not notify_first:
        assert render.call_args.args[0].root.props["value"] == "用量：未上报 / Usage: not reported"
        await notify()
    await notify()
    assert render.call_args.args[0].root.props["value"] == "用量：22.8M tokens"
    assert all(call.args[0].target == target for call in render.call_args_list)
    assert handler._sessions["session-1"].turn("request-2").total_tokens == 77
    assert handler._sessions["session-2"].turn("request-1").total_tokens == 55
    assert next_handler.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot,expected", [
    (None, None),
    ({"session_id": "session-2", "turn": {"request_id": "request-1", "total_tokens": 99}}, None),
    ({"session_id": "session-1", "turn": {"request_id": "request-2", "total_tokens": 99}}, None),
    ({"session_id": "session-1", "turn": {"total_tokens": 99}}, None),
    ({"session_id": "session-1", "turn": None}, None),
    ({"session_id": "session-1", "turn": {"request_id": "request-1", "input_tokens": 5, "output_tokens": 8}}, None),
    ({"session_id": "session-1", "turn": {"request_id": "request-1", "total_tokens": 0}}, 0),
    ({"session_id": "session-1", "turn": {"request_id": "request-1", "total_tokens": 42}}, 42),
])
async def test_workbench_ready_recovers_only_matching_stored_usage(snapshot, expected) -> None:
    handler = WorkbenchHandlers()
    render = AsyncMock(return_value=UiRenderOutput(status="delivered"))
    usage = AsyncMock(return_value=snapshot)
    ops = SimpleNamespace(ui=SimpleNamespace(render=render), store=SimpleNamespace(get=usage, set=AsyncMock(return_value=True)))
    target = RenderTarget("session-1", "conversation.session.content.after", "webui.workbench-v2", "request-1")
    output = await handler.on_event(Event("ui.ready", UiActionInput("ui.ready", target)), ops, AsyncMock())
    assert output.accepted
    usage.assert_awaited_once_with(WorkbenchHandlers._usage_key("session-1", "request-1"))
    state = handler._sessions["session-1"]
    assert state.turn("request-1").total_tokens == expected
    assert render.call_args.args[0].root == build_view(target.slot_id, state, target.instance_key)


@pytest.mark.asyncio
async def test_workbench_refresh_reads_stored_usage_and_rejects_mismatched_notification_context() -> None:
    handler = WorkbenchHandlers()
    render = AsyncMock(return_value=UiRenderOutput(status="delivered"))
    usage = AsyncMock(return_value={
        "session_id": "session-1", "turn": {"request_id": "request-1", "total_tokens": 800},
    })
    ops = SimpleNamespace(
        ui=SimpleNamespace(render=render), store=SimpleNamespace(get=usage, set=AsyncMock(return_value=True)),
        session_context=SimpleNamespace(session_id="session-2"),
    )
    next_handler = AsyncMock(return_value=ContextUsageOutput(accepted=True))
    await handler.on_usage(Event(CONTEXT_USAGE_EVENT, ContextUsageUpdate("session-1", "request-1")), ops, next_handler)
    assert not handler._sessions
    usage.assert_not_awaited()
    next_handler.assert_awaited_once()
    state = handler._sessions.setdefault("session-1", SessionState())
    state.turn("request-1").total_tokens = 400
    state.mounted.add(("conversation.session.content.after", "request-1"))
    target = RenderTarget("session-1", "conversation.turn.footer.actions", "webui.workbench-v2", "request-1")
    request = UiActionInput("ui.select", target, {"action_id": TURN_SELECT_ACTION, "data": {"key": "refresh"}})
    assert (await handler.on_event(Event("ui.select", request), ops, next_handler)).accepted
    usage.assert_awaited_once_with(WorkbenchHandlers._usage_key("session-1", "request-1"))
    assert state.turn("request-1").total_tokens == 800
    root = render.call_args.args[0].root
    assert root.children[0].props["value"] == "用量：800 tokens"
    assert root.children[1].props["value"] == "本轮刷新 / Round refresh: 1"


@pytest.mark.asyncio
async def test_installed_workbench_persists_round_usage_and_recovers_after_reload(tmp_path: Path) -> None:
    bus = ExtensionEventBus()
    manager = ExtensionManager(tmp_path / "store", store_dir=tmp_path / "package-data")
    transport = SimpleNamespace(
        send_frame=AsyncMock(return_value=True), send_clear=AsyncMock(return_value=True),
        send_error=AsyncMock(return_value=True),
    )
    runtime = WebUIRuntime(bus=bus, transport=transport, catalog=WebUICatalogService(manager.active_packages))
    manager.set_inactive_callback(runtime.drop_package)

    def event_types(target):
        runtime.register_event_types(target)
        register_context_usage_events(target)

    def handlers(target):
        runtime.register_handlers(target)

    await manager.init(bus, host_event_types=event_types, host_handlers=handlers)
    source = Path(__file__).resolve().parents[4] / "jiuwenswarm/extensions_v2/examples/webui_workbench_v2"
    try:
        assert (await manager.install([source]))[0].status == "SUCCESS"
        target = RenderTarget("s1", "conversation.session.content.after", "webui.workbench-v2", "r1")
        ready = Event("ui.ready", UiActionInput("ui.ready", target))
        context = SessionContext("s1")
        assert (await bus.trigger(ready, context)).accepted
        output = await bus.trigger(Event(CONTEXT_USAGE_EVENT, ContextUsageUpdate("s1", "r1", total_tokens=250)), context)
        assert output.accepted
        frame = transport.send_frame.call_args.args[0]
        assert frame.target == replace(target, scope="session")
        assert frame.root.component == "Button"
        assert frame.root.props["value"] == "用量：250 tokens"
        assert frame.root.props["icon"] == "database"
        assert not frame.root.events

        assert (await manager.reload("webui.workbench-v2")).status == "SUCCESS"
        assert (await bus.trigger(ready, context)).accepted
        assert transport.send_frame.call_args.args[0].root.props["value"] == "用量：250 tokens"
        old = replace(target, instance_key="older-round")
        assert (await bus.trigger(Event("ui.ready", UiActionInput("ui.ready", old)), context)).accepted
        assert transport.send_frame.call_args.args[0].root.props["value"] == "用量：未上报 / Usage: not reported"
        transport.send_error.assert_not_awaited()
    finally:
        await manager.shutdown()


@pytest.mark.parametrize("event,key,slot", [
    ("ui.click", "refresh", "conversation.turn.footer.actions"),
    ("ui.select", "unknown", "conversation.turn.footer.actions"),
    ("ui.select", "refresh", "conversation.session.content.after"),
])
def test_workbench_round_menu_rejects_invalid_selections(event: str, key: str, slot: str) -> None:
    state = SessionState()
    request = UiActionInput(
        event_type=event,
        target=RenderTarget(session_id="session-1", slot_id=slot,
                            package_id="webui.workbench-v2", instance_key="request-1"),
        payload={"action_id": TURN_SELECT_ACTION, "data": {"key": key}},
    )
    assert WorkbenchHandlers._apply(request, state) == "invalid"
    assert state.turns == {}
