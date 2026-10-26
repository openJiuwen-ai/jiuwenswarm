# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for ``TeamRuntimeManager.reset_session``.

``reset_session`` is the interrupt+new-query reset for a persistent team:
it clears ONLY the unfinished task board (``team_task_<hash>`` rows, cascading
``team_task_dependency_<hash>``) while KEEPING the checkpoint (team memory:
spec/context/allocator), the ``team_message_<hash>`` deliberation history,
``message_read_status_<hash>``, the session worktrees, ``team_info`` row and
member roster — so the next ``chat.send`` routes through ``COLD_RECOVER``
(``recover_from_session`` restores the leader with full team memory), with an
empty task board so members idle and the leader re-plans the new query.

These tests pin the key invariants:
- the task board is cleared (rows), the checkpoint is NOT released;
- ``team_message_<hash>`` is NOT dropped (no ``drop_session_tables_by_id``);
- ``team_info`` is NOT deleted, ``team_home`` is NOT rmtree'd, session
  worktrees are NOT removed;
- idempotent when the checkpoint is already gone (no clear, no release);
- an active runtime is force-stopped before the clear.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_mgr() -> MagicMock:
    mgr = MagicMock()
    mgr._pool = MagicMock()
    mgr._pool.has_active = AsyncMock(return_value=False)
    mgr._pool.get = AsyncMock(return_value=None)
    mgr.stop_team = AsyncMock()
    mgr._resolve_any_team_session_release_info = AsyncMock(
        return_value=MagicMock(db_config=None)
    )
    return mgr


def _teardown_patchers(db: MagicMock):
    """Return the patch objects for every teardown primitive reset_session
    reaches. Enter them in the test with `with p[0] as CF, p[1], p[2]:`.

    Note: reset_session no longer calls ``remove_session_worktrees`` (worktrees
    are preserved, same as ``stop_team``/``finalize``), so no worktree patcher.
    """
    return [
        patch("openjiuwen.agent_teams.runtime.manager.CheckpointerFactory"),
        patch(
            "openjiuwen.agent_teams.spawn.shared_resources.get_shared_db",
            return_value=db,
        ),
        patch(
            "openjiuwen.agent_teams.external.cli_agent.session_cleanup."
            "cleanup_external_cli_backend_sessions",
            new=AsyncMock(),
        ),
    ]


def _make_db() -> MagicMock:
    db = MagicMock()
    db.initialize = AsyncMock()
    db.clear_session_task_board_by_id = AsyncMock(return_value=0)
    db.drop_session_tables_by_id = AsyncMock()  # must NEVER be called
    db.team = MagicMock()
    db.team.delete_team = AsyncMock()  # must NEVER be called
    return db


@pytest.mark.asyncio
async def test_reset_session_clears_task_board_and_keeps_checkpoint():
    """reset_session clears the task board rows but does NOT release the
    checkpoint (so the next chat.send -> COLD_RECOVER, not NEW_TEAM_IN_SESSION)
    and does NOT drop the per-session tables (keeps team_message_ history)."""
    mgr = _make_mgr()
    ckpt = MagicMock()
    ckpt.session_exists = AsyncMock(return_value=True)
    ckpt.release = AsyncMock()
    db = _make_db()

    p = _teardown_patchers(db)
    with p[0] as CF, p[1], p[2]:
        CF.get_checkpointer.return_value = ckpt
        from openjiuwen.agent_teams.runtime.manager import TeamRuntimeManager

        ok = await TeamRuntimeManager.reset_session(mgr, "oc_team_x__h", "sess_1")

    assert ok is True
    db.clear_session_task_board_by_id.assert_awaited_once_with("sess_1")
    ckpt.release.assert_not_awaited()  # checkpoint kept -> COLD_RECOVER
    db.drop_session_tables_by_id.assert_not_awaited()  # team_message_ preserved


@pytest.mark.asyncio
async def test_reset_session_idempotent_when_no_checkpoint():
    """No checkpoint -> nothing to reset; no clear, no release (idempotent)."""
    mgr = _make_mgr()
    ckpt = MagicMock()
    ckpt.session_exists = AsyncMock(return_value=False)  # checkpoint already gone
    ckpt.release = AsyncMock()
    db = _make_db()

    with patch("openjiuwen.agent_teams.runtime.manager.CheckpointerFactory") as CF:
        CF.get_checkpointer.return_value = ckpt
        from openjiuwen.agent_teams.runtime.manager import TeamRuntimeManager

        ok = await TeamRuntimeManager.reset_session(mgr, "oc_team_x__h", "sess_1")

    assert ok is True
    ckpt.release.assert_not_awaited()
    db.clear_session_task_board_by_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_reset_session_preserves_team_info_home_worktrees_and_messages():
    """reset_session must NOT: delete team_info, rmtree team_home, remove
    session worktrees, or drop per-session tables (which would wipe
    team_message_ deliberation history)."""
    mgr = _make_mgr()
    ckpt = MagicMock()
    ckpt.session_exists = AsyncMock(return_value=True)
    ckpt.release = AsyncMock()
    db = _make_db()

    p = _teardown_patchers(db)
    with p[0] as CF, p[1], p[2], \
            patch("openjiuwen.agent_teams.runtime.manager.team_home"), \
            patch("openjiuwen.agent_teams.runtime.manager.shutil") as sh, \
            patch(
                "openjiuwen.agent_teams.runtime.manager.remove_session_worktrees",
                new=AsyncMock(return_value=True),
            ) as rm_wt:
        CF.get_checkpointer.return_value = ckpt
        from openjiuwen.agent_teams.runtime.manager import TeamRuntimeManager

        ok = await TeamRuntimeManager.reset_session(mgr, "oc_team_x__h", "sess_1")

    assert ok is True
    db.team.delete_team.assert_not_awaited()      # keeps team_info + roster
    sh.rmtree.assert_not_called()                 # keeps team_home
    rm_wt.assert_not_awaited()                     # keeps session worktrees
    db.drop_session_tables_by_id.assert_not_awaited()  # keeps team_message_


@pytest.mark.asyncio
async def test_reset_session_force_stops_active_runtime_first():
    """When the pool has an active runtime, reset_session force-stops it
    (mirrors delete_team:743-762) before clearing the task board."""
    mgr = _make_mgr()
    mgr._pool.has_active = AsyncMock(return_value=True)
    entry = MagicMock()
    entry.current_session_id = "active_sess"
    mgr._pool.get = AsyncMock(return_value=entry)
    mgr.stop_team = AsyncMock()
    ckpt = MagicMock()
    ckpt.session_exists = AsyncMock(return_value=False)  # short-circuit after stop
    ckpt.release = AsyncMock()
    db = _make_db()

    p = _teardown_patchers(db)
    with p[0] as CF, p[1], p[2]:
        CF.get_checkpointer.return_value = ckpt
        from openjiuwen.agent_teams.runtime.manager import TeamRuntimeManager

        ok = await TeamRuntimeManager.reset_session(
            mgr, "oc_team_x__h", "sess_1", force=True
        )

    assert ok is True
    mgr.stop_team.assert_awaited_once_with(
        team_name="oc_team_x__h", session_id="active_sess"
    )


@pytest.mark.asyncio
async def test_reset_session_force_clears_inprocess_member_inflight_round():
    """reset_session(force=True) aborts each in-process member's in-flight
    round BEFORE the stop_team/force_kill fallback, so the interrupted
    round's incomplete tool_call is sanitized (dropped via the abort path's
    ``_cleanup_context_on_cancel``) and COLD_RECOVER no longer resumes it.

    Pins the contract: enumerate entry.agent.spawn_manager.spawned_handles;
    per handle with agent_ref -> stream_controller.cancel_agent() (=
    ``harness.abort(immediate=True)``); handles without agent_ref (subprocess
    members) are skipped, not raised on. stop_team is still the fallback.
    """
    mgr = _make_mgr()
    mgr._pool.has_active = AsyncMock(return_value=True)

    # in-process member with a live handle + agent_ref
    member_sc = MagicMock()
    member_sc.cancel_agent = AsyncMock()
    member_ref = MagicMock()
    member_ref.stream_controller = member_sc
    inproc_handle = MagicMock()
    inproc_handle.agent_ref = member_ref

    # subprocess member: handle without agent_ref -> must be skipped, not raise
    subprocess_handle = MagicMock()
    subprocess_handle.agent_ref = None

    entry = MagicMock()
    entry.current_session_id = "active_sess"
    entry.agent.spawn_manager.spawned_handles = {
        "client-engineer": inproc_handle,
        "remote-cli": subprocess_handle,
    }
    mgr._pool.get = AsyncMock(return_value=entry)
    mgr.stop_team = AsyncMock()
    ckpt = MagicMock()
    ckpt.session_exists = AsyncMock(return_value=False)  # short-circuit after stop
    ckpt.release = AsyncMock()
    db = _make_db()

    p = _teardown_patchers(db)
    with p[0] as CF, p[1], p[2]:
        CF.get_checkpointer.return_value = ckpt
        from openjiuwen.agent_teams.runtime.manager import TeamRuntimeManager

        ok = await TeamRuntimeManager.reset_session(
            mgr, "oc_team_x__h", "sess_1", force=True
        )

    assert ok is True
    # member in-flight aborted BEFORE stop_team fallback
    member_sc.cancel_agent.assert_awaited_once()
    # stop_team still the fallback, once, with the active session id
    mgr.stop_team.assert_awaited_once_with(
        team_name="oc_team_x__h", session_id="active_sess"
    )
