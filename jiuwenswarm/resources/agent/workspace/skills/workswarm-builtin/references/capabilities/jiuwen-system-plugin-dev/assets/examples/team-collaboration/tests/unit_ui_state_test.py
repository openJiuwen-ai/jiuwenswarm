"""Unit tests: ``/ui-state`` whitelist, merge semantics and limits (§5.3)."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from team_collab_viz import ui_state  # noqa: E402


def test_defaults_cover_all_nine_keys():
    state = ui_state.default_state()
    assert tuple(state) == ui_state.STATE_KEYS
    assert state["active_tab"] == "team-info"
    assert state["member_page"] == 1
    assert state["theme"] == "system"
    assert state["canvas"]["layout_mode"] == "horizontal"


def test_get_view_always_returns_complete_state():
    """§5.3.1 U2/U3: never-write still yields nine keys; ``empty`` says "unwritten"."""
    store = ui_state.UiStateStore()
    state, updated_at, empty = store.get_view("nobody")
    assert empty is True
    assert updated_at is None
    assert state == ui_state.default_state()
    assert tuple(state) == ui_state.STATE_KEYS

    store.patch("s1", {"selected_task_id": "t1"})
    state, updated_at, empty = store.get_view("s1")
    assert empty is False
    assert updated_at > 0
    assert state["selected_task_id"] == "t1"
    assert tuple(state) == ui_state.STATE_KEYS

    store.patch("s1", {"selected_task_id": None})
    state, _, _ = store.get_view("s1")
    assert "selected_task_id" in state and state["selected_task_id"] is None


def test_put_null_means_default():
    """§5.3.1 U5: ``null`` in a PUT body keeps the default for that key."""
    store = ui_state.UiStateStore()
    state, _, _ = store.put("s1", {"theme": "dark", "canvas": {"zoom": 3.0}, "active_tab": None})
    assert state["theme"] == "dark"
    assert state["canvas"]["zoom"] == 3.0
    assert state["active_tab"] == "team-info"  # null -> default, no STATE_INVALID
    assert tuple(state) == ui_state.STATE_KEYS

    everything_null, _, _ = store.put("s2", {key: None for key in ui_state.STATE_KEYS})
    assert everything_null == ui_state.default_state()


def test_put_normalises_and_fills_defaults():
    store = ui_state.UiStateStore()
    state, updated_at, size = store.put("s1", {"member_page": 2, "theme": "dark"})
    assert state["member_page"] == 2
    assert state["theme"] == "dark"
    assert state["active_tab"] == "team-info"
    assert updated_at > 0
    assert size < ui_state.MAX_BYTES
    assert store.get("s1")[0] == state


def test_put_rejects_team_data_keys():
    store = ui_state.UiStateStore()
    for key in ("team", "members", "tasks", "graph"):
        with pytest.raises(ui_state.UiStateError) as error:
            store.put("s1", {key: {"anything": 1}})
        assert error.value.code == "STATE_KEY_NOT_ALLOWED"
    assert store.get("s1") == (None, None)


def test_patch_rejects_unknown_key_without_partial_write():
    store = ui_state.UiStateStore()
    store.put("s1", {"member_page": 3})
    with pytest.raises(ui_state.UiStateError) as error:
        store.patch("s1", {"theme": "dark", "tasks": []})
    assert error.value.code == "STATE_KEY_NOT_ALLOWED"
    assert store.get("s1")[0]["member_page"] == 3
    assert store.get("s1")[0]["theme"] == "system"


def test_patch_merges_only_supplied_keys():
    store = ui_state.UiStateStore()
    store.put("s1", {"member_page": 3, "theme": "dark"})
    state, _, _ = store.patch("s1", {"expanded_member_ids": ["dev"]})
    assert state["member_page"] == 3
    assert state["theme"] == "dark"
    assert state["expanded_member_ids"] == ["dev"]


def test_patch_arrays_replace_whole():
    store = ui_state.UiStateStore()
    store.put("s1", {"status_filter": ["pending", "blocked"]})
    state, _, _ = store.patch("s1", {"status_filter": ["completed"]})
    assert state["status_filter"] == ["completed"]


def test_patch_deep_merges_canvas_and_search():
    store = ui_state.UiStateStore()
    store.put("s1", {"canvas": {"layout_mode": "vertical", "zoom": 1.5, "pan": {"x": 10.0, "y": 20.0}}})
    state, _, _ = store.patch("s1", {"canvas": {"zoom": 2.5}, "search": {"team_info": "后端"}})
    assert state["canvas"]["layout_mode"] == "vertical"
    assert state["canvas"]["zoom"] == 2.5
    assert state["canvas"]["pan"] == {"x": 10.0, "y": 20.0}
    assert state["search"] == {"team_info": "后端", "task_graph": ""}


def test_patch_null_resets_one_key():
    store = ui_state.UiStateStore()
    store.put("s1", {"member_page": 4, "theme": "dark", "selected_task_id": "t-1"})
    state, _, _ = store.patch("s1", {"member_page": None, "selected_task_id": None})
    assert state["member_page"] == 1
    assert state["selected_task_id"] is None
    assert state["theme"] == "dark"


def test_patch_on_empty_scope_starts_from_defaults():
    store = ui_state.UiStateStore()
    state, _, _ = store.patch("fresh", {"detail_open": True})
    assert state["detail_open"] is True
    assert state["active_tab"] == "team-info"


def test_zoom_is_clamped():
    store = ui_state.UiStateStore()
    assert store.put("s1", {"canvas": {"zoom": 99}})[0]["canvas"]["zoom"] == ui_state.ZOOM_MAX
    assert store.patch("s1", {"canvas": {"zoom": 0.01}})[0]["canvas"]["zoom"] == ui_state.ZOOM_MIN


def test_invalid_values_are_rejected():
    store = ui_state.UiStateStore()
    for payload in (
        {"active_tab": "nope"},
        {"member_page": 0},
        {"detail_open": "yes"},
        {"theme": "blue"},
        {"status_filter": ["unknown"]},
        {"canvas": {"pan": {"x": "1", "y": 2}}},
        {"expanded_member_ids": [1, 2]},
    ):
        with pytest.raises(ui_state.UiStateError) as error:
            store.put("s1", payload)
        assert error.value.code == "STATE_INVALID"


def test_size_limit_applies_to_put_and_patch():
    store = ui_state.UiStateStore()
    huge = {"expanded_member_ids": ["x" * 200 for _ in range(200)]}
    with pytest.raises(ui_state.UiStateError) as error:
        store.put("s1", huge)
    assert error.value.code in ("STATE_TOO_LARGE", "STATE_INVALID")
    with pytest.raises(ui_state.UiStateError) as error:
        store.patch("s1", {"search": {"team_info": "y" * (ui_state.MAX_BYTES + 1)}})
    assert error.value.code == "STATE_TOO_LARGE"


def test_nested_unknown_subkeys_are_rejected_without_partial_write():
    """§5.3.1 U7: unknown sub-keys 400 and nothing is written."""
    store = ui_state.UiStateStore()
    store.put("s1", {"canvas": {"layout_mode": "vertical"}, "search": {"team_info": "keep"}})

    bad_payloads = (
        {"canvas": {"layout_mode": "horizontal", "team": {"secret": 1}}},
        {"canvas": {"pan": {"x": 1.0, "y": 2.0, "z": 3.0}}},
        {"search": {"evil": "<团队数据>"}},
        {"search": {"team_info": "x", "members": []}},
    )
    for payload in bad_payloads:
        with pytest.raises(ui_state.UiStateError) as error:
            store.put("s1", payload)
        assert error.value.code == "STATE_KEY_NOT_ALLOWED", payload
        with pytest.raises(ui_state.UiStateError) as patch_error:
            store.patch("s1", payload)
        assert patch_error.value.code == "STATE_KEY_NOT_ALLOWED", payload

    # no partial write: the previous state is untouched and the shape is fixed
    state, _, _ = store.get_view("s1")
    assert state["canvas"]["layout_mode"] == "vertical"
    assert state["search"]["team_info"] == "keep"
    assert set(state["search"]) == ui_state.SEARCH_SUBKEYS
    assert set(state["canvas"]) == ui_state.CANVAS_SUBKEYS
    assert set(state["canvas"]["pan"]) == ui_state.PAN_SUBKEYS


def test_nested_null_subkeys_are_invalid():
    """§5.3.1 U6: ``null`` resets top-level keys only; a nested null is a 400."""
    store = ui_state.UiStateStore()
    for payload in ({"search": {"team_info": None}}, {"canvas": {"zoom": None}},
                    {"canvas": {"pan": None}}):
        with pytest.raises(ui_state.UiStateError) as error:
            store.patch("s1", payload)
        assert error.value.code == "STATE_INVALID", payload


def test_lru_eviction_respects_max_scopes():
    store = ui_state.UiStateStore(max_scopes=2)
    store.put("a", {"member_page": 1})
    store.put("b", {"member_page": 2})
    store.put("c", {"member_page": 3})
    assert store.scope_count() == 2
    assert store.get("a") == (None, None)
    assert store.get("c")[0]["member_page"] == 3


def test_ttl_expiry():
    store = ui_state.UiStateStore(ttl_seconds=0)
    store.put("a", {"member_page": 2})
    assert store.get("a") == (None, None)


def test_concurrent_patches_do_not_lose_writes():
    store = ui_state.UiStateStore()
    store.put("s1", {})
    barrier = threading.Barrier(8)

    def worker(index: int) -> None:
        barrier.wait()
        for step in range(20):
            store.patch("s1", {"member_page": index * 100 + step + 1})

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    state, _ = store.get("s1")
    assert isinstance(state["member_page"], int)
    assert state["member_page"] >= 1


def test_concurrent_patches_of_different_keys_keep_both():
    store = ui_state.UiStateStore()
    store.put("s1", {})
    done = threading.Event()

    def patcher(payload: dict) -> None:
        for _ in range(50):
            store.patch("s1", payload)
        done.set()

    first = threading.Thread(target=patcher, args=({"member_page": 7},))
    second = threading.Thread(target=patcher, args=({"canvas": {"zoom": 3.0}},))
    first.start()
    second.start()
    first.join()
    second.join()
    state, _ = store.get("s1")
    assert state["member_page"] == 7
    assert state["canvas"]["zoom"] == 3.0


def test_validate_state_returns_size():
    state, size = ui_state.validate_state({"theme": "light"})
    assert state["theme"] == "light"
    assert 0 < size < ui_state.MAX_BYTES
