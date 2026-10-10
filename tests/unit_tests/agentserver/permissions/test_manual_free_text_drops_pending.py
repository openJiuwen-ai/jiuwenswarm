"""Free-text user input must drop pending manual-mode permission continuation
without re-executing the old tool (DTS.md Issue #4851 Section 5.1).

Before the fix, a free-text chat sent while a manual-mode tool interrupt is
pending reaches the SDK unchanged. The SDK still sees the persisted
``INTERRUPTION_KEY`` on the loop session and either re-prompts or auto-runs
the old tool. After the fix, the Host input boundary discards the Core
permission continuation first and the free text becomes a new turn.

These tests are the red-light contract for that boundary.
"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.foundation.llm import AssistantMessage, Model, ToolCall
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY

from gateway_protocol.e2a.agent_models import AgentRequest
from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter import interface
from tests.unit_tests.agentserver.permissions.test_permission_answer_cutover import (
    _interrupt, _request, _Script, answer_host,
)
from tests.unit_tests.agentserver.permissions.test_permission_cold_build import cold  # noqa: F401


pytestmark = pytest.mark.asyncio


async def _send_free_text(h, text):
    """Send a plain chat string with no answer payload at all."""
    return await _request(h, query=text)


def _pending_state(h):
    instance = h.adapter._instance
    if instance is None:
        return None
    loop_session = getattr(instance, "loop_session", None)
    if loop_session is None:
        return None
    return loop_session.get_state(INTERRUPTION_KEY)


def _pending_tool_ids(h):
    state = _pending_state(h)
    if state is None:
        return ()
    interrupted = getattr(state, "interrupted_tools", None) or {}
    return tuple(sorted(interrupted.keys()))


@pytest.mark.parametrize("mode", ["manual", "auto"])
async def test_free_text_clears_pending_and_becomes_new_turn(answer_host, monkeypatch, mode):
    h = answer_host
    await _interrupt(h, monkeypatch, mode=mode, kind="permission")
    assert _pending_tool_ids(h), "interrupt must leave INTERRUPTION_KEY populated"
    pending_snapshot = _pending_state(h)

    h.script.responses = [AssistantMessage(content="new task handled via talk")]

    result = await _send_free_text(h, "I changed my mind — let's just talk.")

    assert result.ok, (result.payload, h.script.calls, h.executions)
    assert h.executions == [], (
        "old pending tool must NOT execute when user sends free text"
    )
    assert h.script.calls, "free text must reach the model as a new turn"
    last_call_args, last_call_kwargs = h.script.calls[-1]
    rendered = repr(last_call_args) + repr(last_call_kwargs)
    assert "I changed my mind" in rendered, (
        "free text must remain visible in the new turn history"
    )
    after = _pending_state(h)
    assert (after is None) or (not after.interrupted_tools), (
        "pending Core INTERRUPTION_KEY must be discarded before the free text "
        "reaches Core; otherwise the SDK resumes the old tool. "
        f"before={pending_snapshot!r} after={after!r}"
    )


@pytest.mark.parametrize("mode", ["manual", "auto"])
async def test_free_text_keeps_rail_and_epoch_intact(answer_host, monkeypatch, mode):
    h = answer_host
    await _interrupt(h, monkeypatch, mode=mode, kind="permission")
    old_permission = h.adapter._permission_rail
    old_epoch = h.adapter._permission_state.permission_epoch

    h.script.responses = [AssistantMessage(content="handled")]
    result = await _send_free_text(h, "never mind, just chat")

    assert result.ok
    assert h.executions == []
    assert h.adapter._permission_rail is old_permission
    assert h.adapter._permission_state.permission_epoch == old_epoch


@pytest.mark.asyncio
async def test_pending_manual_answer_with_unrelated_card_id_drops_to_free_text(
    answer_host, monkeypatch
):
    """A manual answer that points at a stale card (no live card) must not be
    re-bound to a pending Core INTERRUPTION_KEY. Either the stale answer is
    rejected, or the free text is preserved as new chat. Never silently
    auto-execute the old tool."""
    h = answer_host
    await _interrupt(h, monkeypatch, mode="manual", kind="permission")
    h.script.responses = [AssistantMessage(content="ok")]

    foreign = {
        "source": "permission_interrupt",
        "request_id": "foreign-call-id",
        "answers": [{"selected_options": ["allow_once"], "card_id": "foreign-card"}],
    }
    try:
        result = await _request(h, **foreign)
    except Exception:
        result = None

    if result is not None:
        assert h.executions == [], (
            "stale manual answer must not auto-execute the pending tool"
        )
        assert _pending_state(h) is None or not _pending_state(h).interrupted_tools
    assert h.adapter._permission_state.permission_epoch is not None or _pending_state(h) is None