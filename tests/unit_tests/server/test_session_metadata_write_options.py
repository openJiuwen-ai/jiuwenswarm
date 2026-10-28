# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Gateway metadata write policies across queued and synchronous paths."""

from pathlib import Path
import queue

import pytest

from jiuwenswarm.server.runtime.session import lifecycle
from jiuwenswarm.server.runtime.session import session_metadata as sm


@pytest.fixture(autouse=True)
def metadata_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(sm, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(lifecycle, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(sm, "_METADATA_CACHE", {})
    monkeypatch.setattr(sm, "_METADATA_CACHE_GENERATIONS", {})
    monkeypatch.setattr(sm, "_SESSION_REBIND_GEN", {})
    monkeypatch.setattr(sm, "_METADATA_QUEUE", queue.Queue(maxsize=1))
    # Control worker admission so a competing write can run before queue drain.
    monkeypatch.setattr(sm, "_ensure_worker_started", lambda: None)


def _initial_metadata() -> dict:
    return {
        "session_id": "write-moa",
        "title": "before",
        "channel_id": "web",
        "mode": "agent.work.normal",
        "work_mode": "work",
        "project_id": "project-old",
        "project_dir": "/old",
        "channel_metadata": {"cwd": "/old"},
        "moa_enabled": False,
        "pinned": False,
        "pin_order": 0,
        "message_count": 9,
    }


def _prepare_queue(mode: str) -> None:
    if mode == "full":
        sm._METADATA_QUEUE.put_nowait(("occupied", {}, None))


def _drain_write() -> None:
    session_id, metadata, options = sm._METADATA_QUEUE.get_nowait()
    try:
        sm._write_metadata_sync(session_id, metadata, options)
    finally:
        sm._METADATA_QUEUE.task_done()


def test_owner_stop_persists_without_changing_session_activity_or_other_metadata():
    from jiuwenswarm.server.runtime.organization_execution import owner_is_stopped, set_owner_stopped

    sm.update_session_metadata(
        session_id="owner-stop", mode="team", team_name="owner", title="original", sync_write=True,
    )
    original = sm.get_session_metadata("owner-stop")
    set_owner_stopped("owner-stop", True)
    assert sm.get_session_metadata("owner-stop", cache_bust=True)["organization_owner_stopped"] is True
    assert owner_is_stopped("owner-stop", "owner")
    assert not owner_is_stopped("owner-stop", "child")
    stored = sm.get_session_metadata("owner-stop")
    assert stored["last_message_at"] == original["last_message_at"]
    assert stored["title"] == "original"
    set_owner_stopped("owner-stop", False)
    assert not owner_is_stopped("owner-stop", "owner")


@pytest.mark.parametrize("mode", ["sync", "queued", "full"])
def test_stale_write_preserves_concurrent_moa_selection_pins_and_rebind(
    monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    original = _initial_metadata()
    session_id = original["session_id"]
    sm._write_metadata_sync(session_id, original)
    write = sm._write_metadata_sync
    selected = {"type": "model_group", "id": "group-new"}

    def concurrent_write(sid: str, metadata: dict, options: sm._MetadataWriteOptions) -> dict:
        # The competing update happens after the stale writer captured its version.
        sm._bump_rebind_gen(sid)
        write(
            sid,
            {
                **original,
                "moa_enabled": True,
                "model_selection": selected,
                "pinned": True,
                "pin_order": 2,
                "project_id": "project-new",
                "project_dir": "/new",
                "channel_metadata": {"cwd": "/new"},
            },
            sm._MetadataWriteOptions(
                owns_moa_enabled=True,
                merge_fields=frozenset({
                    "moa_enabled", "model_selection", "pinned", "pin_order",
                    "project_id", "project_dir", "channel_metadata",
                }),
            ),
        )
        return write(sid, metadata, options)

    monkeypatch.setattr(sm, "_write_metadata_sync", concurrent_write)
    _prepare_queue(mode)
    sm._enqueue_write(
        session_id,
        {**original, "title": "late title"},
        sync_write=mode == "sync",
        preserve_pin_fields=True,
    )
    if mode == "queued":
        assert lifecycle.raw_metadata(session_id)["title"] == "before"
        _drain_write()

    saved = lifecycle.raw_metadata(session_id)
    assert saved["title"] == "late title"
    assert saved["moa_enabled"] is True
    assert saved["model_selection"] == selected
    assert saved["pinned"] is True
    assert saved["pin_order"] == 2
    assert saved["project_id"] == "project-new"
    assert saved["project_dir"] == "/new"
    assert saved["channel_metadata"] == {"cwd": "/new"}
    if mode != "queued":
        assert sm._METADATA_CACHE[session_id] == saved


@pytest.mark.parametrize("mode", ["sync", "queued", "full"])
@pytest.mark.parametrize("enabled", [False, True])
def test_explicit_moa_update_owns_the_persisted_flag(mode: str, enabled: bool) -> None:
    original = {**_initial_metadata(), "moa_enabled": not enabled, "pinned": True, "pin_order": 3}
    session_id = original["session_id"]
    sm._write_metadata_sync(session_id, original)
    _prepare_queue(mode)

    sm.update_session_metadata(
        session_id=session_id,
        moa_enabled=enabled,
        touch_last_message_at=False,
        sync_write=mode == "sync",
    )
    if mode == "queued":
        assert lifecycle.raw_metadata(session_id)["moa_enabled"] is not enabled
        _drain_write()

    saved = lifecycle.raw_metadata(session_id)
    assert saved["moa_enabled"] is enabled
    assert saved["pinned"] is True
    assert saved["pin_order"] == 3


@pytest.mark.parametrize("mode", ["sync", "queued", "full"])
def test_partial_write_keeps_unowned_fields(mode: str) -> None:
    original = {**_initial_metadata(), "moa_enabled": True}
    session_id = original["session_id"]
    sm._write_metadata_sync(session_id, original)
    _prepare_queue(mode)

    sm._enqueue_write(
        session_id,
        {**original, "title": "migrated", "message_count": 1, "moa_enabled": False},
        sync_write=mode == "sync",
        merge_fields=frozenset({"title"}),
    )
    if mode == "queued":
        _drain_write()

    assert lifecycle.raw_metadata(session_id) == {**original, "title": "migrated"}


def test_queued_write_cannot_cross_a_lifecycle_generation_change() -> None:
    original = _initial_metadata()
    session_id = original["session_id"]
    sm._write_metadata_sync(session_id, original)
    with lifecycle.resource_lock("session", session_id):
        lifecycle.save_locked("session", session_id, {"generation": 3})

    sm._enqueue_write(session_id, {**original, "title": "accepted"})
    _drain_write()
    sm._enqueue_write(session_id, {**original, "title": "stale"})
    with lifecycle.resource_lock("session", session_id):
        lifecycle.save_locked("session", session_id, {"generation": 4})

    with pytest.raises(lifecycle.LifecycleError, match="stale session writer generation"):
        _drain_write()

    assert lifecycle.raw_metadata(session_id)["title"] == "accepted"
