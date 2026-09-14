# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_task_guard import (
    SessionKVCacheTaskGuard,
)


def _finish_once(guard: SessionKVCacheTaskGuard, session_id: str) -> None:
    guard.set_foreground(
        session_id=session_id,
        view_id="view-1",
        visible=True,
        channel_id="web",
        is_team=False,
    )
    guard.task_started(
        session_id=session_id,
        channel_id="web",
        is_team=False,
    )
    assert guard.task_finished(session_id=session_id, succeeded=True) is None


def test_foreground_completed_session_offloads_only_after_last_view_leaves() -> None:
    guard = SessionKVCacheTaskGuard()
    session_id = "session-A"
    guard.set_foreground(
        session_id=session_id,
        view_id="view-1",
        visible=True,
        channel_id="web",
        is_team=False,
    )
    guard.set_foreground(
        session_id=session_id,
        view_id="view-2",
        visible=True,
        channel_id="web",
        is_team=False,
    )
    _finish_once(guard, session_id)

    assert guard.set_foreground(
        session_id=session_id,
        view_id="view-1",
        visible=False,
        channel_id="web",
        is_team=False,
    ) is None
    action = guard.set_foreground(
        session_id=session_id,
        view_id="view-2",
        visible=False,
        channel_id="web",
        is_team=False,
    )

    assert action is not None
    assert (action.action, action.session_id) == ("offload", session_id)


def test_background_running_session_offloads_after_last_task_finishes() -> None:
    guard = SessionKVCacheTaskGuard()
    session_id = "session-A"
    guard.task_started(session_id=session_id, channel_id="web", is_team=True)
    guard.task_started(session_id=session_id, channel_id="web", is_team=True)

    assert guard.task_finished(session_id=session_id, succeeded=True) is None
    action = guard.task_finished(session_id=session_id, succeeded=True)

    assert action is not None
    assert action.action == "offload"
    assert action.is_team is True


def test_view_navigation_never_prefetches_but_input_intent_does() -> None:
    guard = SessionKVCacheTaskGuard()
    session_id = "session-A"
    _finish_once(guard, session_id)
    offload = guard.set_foreground(
        session_id=session_id,
        view_id="view-1",
        visible=False,
        channel_id="web",
        is_team=False,
    )
    assert offload is not None and offload.action == "offload"

    assert guard.set_foreground(
        session_id=session_id,
        view_id="view-1",
        visible=True,
        channel_id="web",
        is_team=False,
    ) is None
    prefetch = guard.prepare(
        session_id=session_id,
        intent_id="intent-1",
        channel_id="web",
        is_team=False,
    )

    assert prefetch is not None and prefetch.action == "prefetch"
    assert guard.prepare(
        session_id=session_id,
        intent_id="intent-1",
        channel_id="web",
        is_team=False,
    ) is None


def test_task_start_prefetch_fallback_does_not_wait_or_hide_task_count() -> None:
    guard = SessionKVCacheTaskGuard()
    session_id = "session-A"
    _finish_once(guard, session_id)
    guard.set_foreground(
        session_id=session_id,
        view_id="view-1",
        visible=False,
        channel_id="web",
        is_team=False,
    )

    action = guard.task_started(
        session_id=session_id,
        channel_id="web",
        is_team=False,
    )

    assert action is not None and action.action == "prefetch"
    assert guard.snapshot(session_id).running_tasks == 1


def test_delete_blocks_stale_actions_and_failed_delete_can_be_retried() -> None:
    guard = SessionKVCacheTaskGuard()
    session_id = "session-A"
    _finish_once(guard, session_id)
    action = guard.delete(
        session_id=session_id,
        channel_id="web",
        is_team=False,
    )
    assert action is not None and action.action == "evict"
    assert guard.prepare(
        session_id=session_id,
        intent_id="stale-intent",
        channel_id="web",
        is_team=False,
    ) is None

    guard.restore_after_failed_delete(session_id)
    retry = guard.prepare(
        session_id=session_id,
        intent_id="retry-intent",
        channel_id="web",
        is_team=False,
    )
    assert retry is not None and retry.action == "prefetch"
