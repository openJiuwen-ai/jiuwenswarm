"""Workbench round menus preserve per-request snapshots and reject unknown selections."""

import pytest

from jiuwenswarm.extensions_v2.examples.webui_workbench_v2.webui_workbench_v2.handlers import WorkbenchHandlers
from jiuwenswarm.extensions_v2.examples.webui_workbench_v2.webui_workbench_v2.state import SessionState
from jiuwenswarm.extensions_v2.examples.webui_workbench_v2.webui_workbench_v2.views import (
    TURN_SELECT_ACTION,
    build_view,
)
from jiuwenswarm.extensions_v2.webui.models import RenderTarget, UiActionInput
from jiuwenswarm.extensions_v2.webui.schema import validate_root


def test_workbench_round_menu_places_a_symbol_button_before_the_icon_trigger() -> None:
    root = build_view("conversation.turn.footer.actions", SessionState(), "request-1")
    assert validate_root(root).ok
    assert root.component == "Box"
    assert root.props["direction"] == "row"
    assert root.props["wrap"] is False
    button, dropdown = root.children
    assert button.component == "Button"
    assert button.props == {"label": "👍", "variant": "ghost", "size": "md"}
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
