"""An answer addressed by interrupt id counts as an answer to the paused call.

agent-core lets a host answer an interrupt by its interrupt_id
(``InteractiveInput.answer``) instead of by tool call id (``update``).
Such an input has empty ``user_inputs``. Every Host reader that decided
"is this an answer to the pause" from ``user_inputs`` alone would take it for
stale input: the manual-mode boundary would discard the pause and cancel the
round, the Smart queue would refuse the question's answer.
"""

# Imported pytest fixtures intentionally share test parameter names.
# ruff: noqa: F811

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.foundation.llm import AssistantMessage
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY

from jiuwenswarm.agents.harness.common.rails.permissions.auto_permission_rail import AutoPermissionInterruptRail
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue import RootPermissionQueueError
from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from tests.unit_tests.agentserver.permissions.test_permission_answer_cutover import (
    _interrupt,
    _request,
    answer_host,  # noqa: F401
)
from tests.unit_tests.agentserver.permissions.test_permission_cold_build import cold  # noqa: F401


pytestmark = pytest.mark.asyncio


def _pending_interrupt_id(h) -> str:
    state = h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY)
    entry = next(iter(state.interrupted_tools.values()))
    request = next(iter(entry.interrupt_requests.values()))
    assert request.interrupt_id
    return request.interrupt_id


def _answer_by_interrupt_id(h, value) -> InteractiveInput:
    query = InteractiveInput()
    query.answer(_pending_interrupt_id(h), value)
    assert not query.user_inputs
    return query


async def test_manual_permission_answer_by_interrupt_id_resumes_instead_of_being_dropped(answer_host, monkeypatch):
    h = answer_host
    await _interrupt(h, monkeypatch, mode="manual", kind="permission")
    discard = AsyncMock(return_value=False)
    monkeypatch.setattr(interface_deep, "discard_pending_permission_interrupt_for_target", discard)
    cancel = AsyncMock()
    monkeypatch.setattr(h.adapter._instance, "cancel_round", cancel)

    result = await _request(
        h, query=_answer_by_interrupt_id(h, {"approved": True, "auto_confirm": False, "feedback": ""}),
    )

    assert result.ok, result.payload
    assert h.executions == [{"value": "once"}]
    assert len(h.script.calls) == 2
    discard.assert_not_awaited()
    cancel.assert_not_awaited()
    state = h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY)
    assert not state or not state.interrupted_tools


async def test_smart_question_answer_by_interrupt_id_resumes(answer_host, monkeypatch):
    h = answer_host
    await _interrupt(h, monkeypatch, mode="auto", kind="ask")

    result = await _request(h, query=_answer_by_interrupt_id(h, {"answers": {"Which colour?": "blue"}}))

    assert result.ok, result.payload
    assert len(h.script.calls) == 2
    assert "blue" in repr(h.script.calls[-1])
    state = h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY)
    assert not state or not state.interrupted_tools


async def test_manual_answer_by_interrupt_id_finishes_before_smart_cutover(answer_host, monkeypatch):
    h = answer_host
    await _interrupt(h, monkeypatch, mode="manual", kind="permission")
    h.raw["permissions"]["mode"] = "auto"
    h.change()

    result = await _request(
        h, query=_answer_by_interrupt_id(h, {"approved": True, "auto_confirm": False, "feedback": ""}),
    )

    assert result.ok, result.payload
    assert h.executions == [{"value": "once"}]
    assert not isinstance(h.adapter._permission_rail, AutoPermissionInterruptRail)
    h.script.responses = [AssistantMessage(content="new task completed")]
    assert (await _request(h, query="Start the next task")).ok
    assert isinstance(h.adapter._permission_rail, AutoPermissionInterruptRail)


async def test_an_answer_to_an_unknown_interrupt_id_is_refused_during_smart_cutover(answer_host, monkeypatch):
    h = answer_host
    await _interrupt(h, monkeypatch, mode="manual", kind="permission")
    h.raw["permissions"]["mode"] = "auto"
    h.change()
    query = InteractiveInput()
    query.answer("intr_unknown", {"approved": True, "auto_confirm": False, "feedback": ""})

    with pytest.raises(RootPermissionQueueError, match="interaction_resume_identity_mismatch"):
        await _request(h, query=query)

    assert h.executions == []
    assert h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY).interrupted_tools
