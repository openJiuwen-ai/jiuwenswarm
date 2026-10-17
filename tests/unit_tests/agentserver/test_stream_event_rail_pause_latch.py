# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""StreamEventRail pause-latch hardening.

Regression tests for the cron 59-minute hang (2026-09-17):
``before_model_call`` / ``before_tool_call`` used to await the pause event
with no timeout, so a lost resume parked the agent until the host's hard
kill. Covered fixes:

- P0-1: bounded wait with fail-open self-heal (``_wait_for_resume``)
- P1-1: ``cleanup_session`` releases a waiter still parked on the latch
- P1-2: adapter dispatches resume even when the session left the active
  counter (resume is idempotent; skipping it deadlocks the latch)
- P1-3: empty session_id no longer falls through to the shared
  ``"default"`` latch
- P0-2: adapter dispatches pause/resume with the normalized session id
- P3-1: every per-session key in the rail is normalized via ``_sid_key``
  (``before_invoke`` strips ``conversation_id``), so a whitespace-padded
  session id pauses/releases the latch the checkpoint actually waits on —
  previously execution keyed raw while control keyed stripped
- P3-2: ``JIUWEN_PAUSE_WAIT_TIMEOUT_SECONDS`` parsing never raises at import
  time and never silently disables pause (invalid/non-positive → default)
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from openjiuwen.core.single_agent.rail.base import InvokeInputs

from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
    _parse_pause_wait_timeout,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue import (
    RootPermissionQueue,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.server.runtime.agent_adapter.permission_dispatch import (
    RootPermissionDispatch,
)


def _rail() -> JiuSwarmStreamEventRail:
    return JiuSwarmStreamEventRail()


def _ctx(sid: str) -> SimpleNamespace:
    """Minimal AgentCallbackContext: session/context None keeps the
    checkpoints from running anything beyond the pause/abort gate."""
    return SimpleNamespace(
        context=None,
        inputs=SimpleNamespace(tools=[]),
        session=None,
        extra={JiuSwarmStreamEventRail._SID_KEY: sid},
    )


@pytest.mark.asyncio
async def test_unpaused_checkpoint_passes_immediately() -> None:
    rail = _rail()
    await asyncio.wait_for(rail.before_model_call(_ctx("s1")), timeout=5.0)
    await asyncio.wait_for(rail.before_tool_call(_ctx("s1")), timeout=5.0)


@pytest.mark.asyncio
async def test_pause_without_resume_fails_open_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lost resume must not park the checkpoint forever (P0-1)."""
    monkeypatch.setattr(JiuSwarmStreamEventRail, "_PAUSE_WAIT_TIMEOUT_SECONDS", 0.05)
    rail = _rail()
    rail.pause("s1")
    assert not rail._get_pause_event("s1").is_set()

    # Both checkpoints fail open instead of hanging until the host's hard kill.
    await asyncio.wait_for(rail.before_model_call(_ctx("s1")), timeout=5.0)
    await asyncio.wait_for(rail.before_tool_call(_ctx("s1")), timeout=5.0)

    # The latch self-heals: later checkpoints on this sid must not re-block.
    assert rail._get_pause_event("s1").is_set()
    await asyncio.wait_for(rail.before_model_call(_ctx("s1")), timeout=5.0)


@pytest.mark.asyncio
async def test_resume_unblocks_parked_checkpoint() -> None:
    rail = _rail()
    rail.pause("s1")
    task = asyncio.create_task(rail.before_model_call(_ctx("s1")))
    await asyncio.sleep(0.01)
    assert not task.done()

    rail.resume("s1")
    await asyncio.wait_for(task, timeout=5.0)


@pytest.mark.asyncio
async def test_cleanup_session_releases_parked_waiter() -> None:
    """cleanup must set the latch it removes, or the waiter never wakes (P1-1)."""
    rail = _rail()
    rail.pause("s1")
    parked_event = rail._get_pause_event("s1")

    task = asyncio.create_task(rail.before_model_call(_ctx("s1")))
    await asyncio.sleep(0.01)
    assert not task.done()

    rail.cleanup_session("s1")
    assert parked_event.is_set()
    await asyncio.wait_for(task, timeout=5.0)


def test_empty_session_id_does_not_touch_default_latch() -> None:
    """Empty session_id must not clear/set the shared "default" latch (P1-3)."""
    rail = _rail()
    default_event = rail._get_pause_event("default")

    rail.pause("")
    rail.resume("")

    assert default_event.is_set()
    assert "s-other" not in rail._pause_events


def _make_adapter(**state: object) -> JiuWenSwarmDeepAdapter:
    """Bare adapter with internal state set via setattr (same pattern as
    test_deep_adapter_interrupt._make_adapter)."""
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = True  # pylint: disable=protected-access
    adapter._parent_session_id = None  # pylint: disable=protected-access
    adapter._enable_auto_permission = False  # pylint: disable=protected-access
    adapter._root_permission_queue = RootPermissionQueue()
    adapter._permission_dispatch = RootPermissionDispatch(
        adapter._root_permission_queue
    )
    adapter._active_session_ids = {}
    adapter._session_agent_tasks = {}
    adapter._stream_event_rail = None
    adapter._instance = None
    for name, value in state.items():
        setattr(adapter, name, value)
    return adapter


def _interrupt_request(intent: str, session_id: str = "sess-latch") -> AgentRequest:
    return AgentRequest(
        request_id=f"req-{intent}",
        channel_id="web",
        session_id=session_id,
        req_method=ReqMethod.CHAT_CANCEL,
        params={"intent": intent, "mode": "agent"},
    )


@pytest.mark.asyncio
async def test_resume_dispatched_when_session_inactive() -> None:
    """Resume must reach the rail even if the session left the active
    counter — otherwise a cleared latch stays cleared forever (P1-2)."""
    rail = MagicMock()
    adapter = _make_adapter(_stream_event_rail=rail)
    assert not adapter._is_session_active("sess-latch")

    response = await adapter.process_interrupt(_interrupt_request("resume"))

    rail.resume.assert_called_once_with("sess-latch")
    rail.pause.assert_not_called()
    rail.abort.assert_not_called()
    assert response.payload["success"] is True
    assert response.payload["intent"] == "resume"


@pytest.mark.asyncio
async def test_resume_dispatch_uses_normalized_sid() -> None:
    """Guard checks the normalized sid but dispatch used to pass the raw
    request.session_id — the two must be the same key (P0-2)."""
    rail = MagicMock()
    adapter = _make_adapter(
        _stream_event_rail=rail,
        _active_session_ids={"sess-latch": 1},
    )

    await adapter.process_interrupt(
        _interrupt_request("resume", session_id="  sess-latch  ")
    )
    rail.resume.assert_called_once_with("sess-latch")

    await adapter.process_interrupt(
        _interrupt_request("pause", session_id="  sess-latch  ")
    )
    rail.pause.assert_called_once_with("sess-latch")


@pytest.mark.asyncio
async def test_pause_still_skipped_when_session_inactive() -> None:
    """Skipping pause is harmless, so the active-session guard stays."""
    rail = MagicMock()
    adapter = _make_adapter(_stream_event_rail=rail)
    assert not adapter._is_session_active("sess-latch")

    response = await adapter.process_interrupt(_interrupt_request("pause"))

    rail.pause.assert_not_called()
    rail.resume.assert_not_called()
    assert response.payload["success"] is True
    assert response.payload["intent"] == "pause"


# ---------------------------------------------------------------------------
# P3-1: session-key normalization (padded ids must not split the latch keys)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_padded_session_id_pauses_and_resumes_real_rail() -> None:
    """A whitespace-padded pause/resume must hit the latch the checkpoint
    actually waits on — no mock, real rail end to end."""
    rail = _rail()
    rail.pause("  sess-latch  ")
    assert not rail._get_pause_event("sess-latch").is_set()

    task = asyncio.create_task(rail.before_model_call(_ctx("sess-latch")))
    await asyncio.sleep(0.01)
    assert not task.done()

    rail.resume("  sess-latch  ")
    await asyncio.wait_for(task, timeout=5.0)
    assert rail._get_pause_event("sess-latch").is_set()


@pytest.mark.asyncio
async def test_before_invoke_normalizes_padded_conversation_id() -> None:
    """Execution keys come from conversation_id: it must be stripped so the
    checkpoint sid equals the control-side (pre-normalized) sid."""
    rail = _rail()
    ctx = SimpleNamespace(
        inputs=InvokeInputs(query="hi", conversation_id="  sess-latch  "),
        session=SimpleNamespace(get_state=lambda _key: False),
        extra={},
    )
    await rail.before_invoke(ctx)
    assert ctx.extra[JiuSwarmStreamEventRail._SID_KEY] == "sess-latch"

    # Control-side pause with the padded id clears the same latch.
    rail.pause("  sess-latch  ")
    assert not rail._get_pause_event("sess-latch").is_set()
    assert "  sess-latch  " not in rail._pause_events


def test_abort_and_cleanup_normalize_padded_session_id() -> None:
    rail = _rail()
    rail.abort("  sess-latch  ")
    assert "sess-latch" in rail._abort_requested
    assert "  sess-latch  " not in rail._abort_requested
    assert rail.is_abort_requested("sess-latch")

    rail.pause("  sess-latch  ")
    rail.cleanup_session("  sess-latch  ")
    assert "sess-latch" not in rail._pause_events
    assert "  sess-latch  " not in rail._pause_events


def test_collect_cancelled_tools_matches_padded_session_id() -> None:
    """In-flight tools are stored under the normalized sid; collection with
    a padded id must still find them."""
    rail = _rail()
    rail._inflight_tool_calls["tc-1"] = {
        "tool_call": SimpleNamespace(id="tc-1", name="shell"),
        "session": None,
        "session_id": "sess-latch",
    }
    rail.collect_cancelled_tool_updates("  sess-latch  ")
    results = rail.get_cancelled_tool_results("  sess-latch  ")
    assert len(results) == 1
    assert results[0]["tool_call_id"] == "tc-1"
    rail.clear_cancelled_tool_results("  sess-latch  ")
    assert rail.get_cancelled_tool_results("sess-latch") == []


def test_whitespace_only_session_id_ignored_for_pause_and_resume() -> None:
    """A whitespace-only id normalizes to the shared "default" latch — the
    empty-id guard must ignore it too."""
    rail = _rail()
    default_event = rail._get_pause_event("default")

    rail.pause("   ")
    rail.resume("   ")

    assert default_event.is_set()
    assert rail._pause_events == {"default": default_event}


# ---------------------------------------------------------------------------
# P3-2: timeout env parsing
# ---------------------------------------------------------------------------


def test_pause_wait_timeout_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIUWEN_PAUSE_WAIT_TIMEOUT_SECONDS", "1.5")
    assert _parse_pause_wait_timeout() == 1.5

    monkeypatch.setenv("JIUWEN_PAUSE_WAIT_TIMEOUT_SECONDS", "  42  ")
    assert _parse_pause_wait_timeout() == 42.0

    for invalid in ("abc", "", "0", "-5"):
        monkeypatch.setenv("JIUWEN_PAUSE_WAIT_TIMEOUT_SECONDS", invalid)
        assert _parse_pause_wait_timeout() == 300.0, invalid

    monkeypatch.delenv("JIUWEN_PAUSE_WAIT_TIMEOUT_SECONDS")
    assert _parse_pause_wait_timeout() == 300.0
