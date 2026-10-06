from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.duplex_router import (
    ControlSnapshot, InboundMessage, observe, prompt_for,
)
from jiuwenswarm.agents.harness.team import duplex_shadow as shadow


def snapshot():
    return ControlSnapshot("v1", "r1", "c1", "model", goal="Order event system",
                           next_action="Use Kafka")


def decision(s, action="INTERRUPT"):
    return {"action": action}


MESSAGES = (InboundMessage("m204", "A1", "Customer forbids Kafka"),)


@pytest.mark.asyncio
async def test_interrupt_is_observed_only():
    s = snapshot()
    result = await observe(s, MESSAGES, classify=AsyncMock(return_value=decision(s)),
                           current_snapshot=lambda: s)
    assert result.proposed_action == "INTERRUPT"
    assert result.effective_action == "UNCHANGED"
    assert result.message_ids == ("m204",)
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_stale_decision_falls_back_without_reclassification():
    old = snapshot()
    new = replace(old, context_version="v2", round_id="r2", checkpoint_id="c2")
    classify = AsyncMock(side_effect=[decision(old), decision(new, "APPEND")])
    result = await observe(old, MESSAGES, classify=classify, current_snapshot=lambda: new)
    assert result.attempts == 1
    assert result.status == "stale"
    assert result.proposed_action == "UNDECIDED"
    classify.assert_awaited_once()


@pytest.mark.asyncio
async def test_continually_changing_context_is_discarded():
    s = snapshot()
    newer = replace(s, context_version="v2")
    newest = replace(s, context_version="v3")
    values = iter([newer, newest])
    classify = AsyncMock(side_effect=[decision(s), decision(newer)])
    result = await observe(s, MESSAGES, classify=classify,
                           current_snapshot=lambda: next(values))
    assert result.status == "stale"
    assert result.proposed_action == "UNDECIDED"


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, "INTERRUPT", {},
    dict(action="DELETE"),
    dict(action="INTERRUPT", context_version="v0", round_id="r1", checkpoint_id="c1")])
async def test_invalid_response_produces_no_decision(response):
    s = snapshot()
    result = await observe(s, MESSAGES, classify=AsyncMock(return_value=response),
                           current_snapshot=lambda: s)
    assert result.status == "error"
    assert result.proposed_action == "UNDECIDED"


@pytest.mark.asyncio
async def test_timeout_and_external_cancellation():
    s = snapshot()
    cancelled = asyncio.Event()

    async def blocked(*_):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    result = await observe(s, MESSAGES, classify=blocked,
                           current_snapshot=lambda: s, timeout_seconds=0.01)
    assert result.status == "timeout"
    assert cancelled.is_set()
    task = asyncio.create_task(observe(s, MESSAGES, classify=blocked,
                                      current_snapshot=lambda: s))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def native_state():
    checkpoint = NS(iteration_index=2, context_messages=["SECRET_REASONING"],
                    deep_agent_state={"task_plan": {"goal": "Order system",
                        "current_task_id": "t1", "tasks": [
                            {"id": "t1", "content": "Implement Kafka"}]}})
    active = NS(round_id=3, original_query="Original task", last_iter_snapshot=checkpoint,
                pre_round_snapshot=None, iter_phase="model", model_call_in_flight=True,
                tool_started=False, pause_requested=False)
    return NS(active_round=active, session_id="session1")


def test_native_snapshot_excludes_history_and_tracks_phase_and_checkpoint():
    harness = native_state()
    before = shadow.snapshot_from_native(harness)
    assert before.next_action == "Implement Kafka"
    assert "SECRET_REASONING" not in prompt_for(before, MESSAGES)
    payload = json.loads(prompt_for(before, MESSAGES))
    assert set(payload["snapshot"]) == {"goal", "next_action", "last_action", "phase"}
    harness.active_round.iter_phase = "tool"
    assert before.context_version != shadow.snapshot_from_native(harness).context_version
    harness.active_round = None
    assert shadow.snapshot_from_native(harness) is None


def test_actual_tool_action_reaches_router_and_changes_snapshot_version():
    harness = native_state()
    actual = "Running Bash: install Kafka"
    completed = ""
    harness._duplex_action_provider = lambda: actual
    harness._duplex_last_action_provider = lambda: completed
    before = shadow.snapshot_from_native(harness)
    assert actual in before.next_action
    actual = ""
    completed = "Completed Bash: write Redis configuration"
    after = shadow.snapshot_from_native(harness)
    assert after.context_version != before.context_version
    assert completed in after.last_action
    assert completed not in after.next_action


class Host:
    def __init__(self):
        self.harness = native_state()
        self.blueprint = NS(member_name="A2")




def make_handler(messages, *, fail_second=False):
    from openjiuwen.agent_teams.agent.coordination.handlers.message import MessageHandler
    from openjiuwen.agent_teams.schema.team import TeamRole

    host = Host()
    host.has_pending_interrupt = lambda: False
    delivered = []

    async def deliver(text, **kwargs):
        if fail_second and len(delivered) == 1:
            raise RuntimeError("delivery failed")
        delivered.append(text)

    host.deliver_input = deliver
    mm = NS(mark_messages_read=AsyncMock(), mark_message_read=AsyncMock())
    handler = MessageHandler(host, NS(role=TeamRole.LEADER, language="en", member_name="A2"),
                             NS(message_manager=mm, team_backend=None), NS())
    handler._read_all_unread = AsyncMock(side_effect=[messages, []])
    handler._expand = AsyncMock(side_effect=lambda msg: NS(body=msg.content, is_template=False))
    return handler, host, mm, delivered


def message(mid, broadcast=False):
    return NS(message_id=mid, from_member_name="A1", to_member_name="A2",
              broadcast=broadcast, protocol="text", content="Customer forbids Kafka", timestamp=0)


@pytest.mark.asyncio
async def test_real_sdk_drain_preserves_delivery_ack_and_broadcast_objects(monkeypatch):
    shadow.install_shadow_observer()
    messages = [message("direct"), message("broadcast", True)]
    handler, host, mm, delivered = make_handler(messages)
    await handler._process_unread_messages("A2")
    assert [item.message.message_id for item in delivered] == ["direct", "broadcast"]
    assert len(delivered) == 2
    mm.mark_messages_read.assert_awaited_once_with(messages, "A2")
    mm.mark_message_read.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_sdk_failed_delivery_leaves_undelivered_message_unread(monkeypatch):
    messages = [message("direct"), message("broadcast", True)]
    handler, host, mm, delivered = make_handler(messages, fail_second=True)
    with pytest.raises(RuntimeError, match="delivery failed"):
        await handler._process_unread_messages("A2")
    mm.mark_messages_read.assert_awaited_once_with([messages[0]], "A2")
    # A lost wake-up is repaired by the same SDK mailbox polling path.
    handler._read_all_unread = AsyncMock(side_effect=[[messages[1]], []])
    host.deliver_input = AsyncMock()
    await handler.on_poll_mailbox(None)
    mm.mark_messages_read.assert_awaited_with([messages[1]], "A2")






def test_install_is_idempotent():
    from openjiuwen.agent_teams.agent.coordination.handlers.message import MessageHandler
    shadow.install_shadow_observer()
    installed = MessageHandler._format_message
    assert shadow.install_shadow_observer()
    assert MessageHandler._format_message is installed




@pytest.mark.asyncio
async def test_replay_counts_failed_append_as_failure_and_never_sends_label():
    from pathlib import Path
    from jiuwenswarm.common.duplex_benchmark import load_cases, replay
    cases = load_cases(Path(__file__).parents[2] / "fixtures/duplex/routing_cases.jsonl")
    calls = 0

    async def classify(s, messages):
        nonlocal calls
        calls += 1
        assert "expected_action" not in prompt_for(s, messages)
        if calls == 2:
            raise RuntimeError("provider failed")
        return decision(s, cases[calls - 1]["expected_action"])

    report = await replay(cases, classify)
    assert report["runs"] == 6
    assert report["correct"] == 5
    assert report["failures"] == 1
    assert report["accuracy_including_failures"] == 5 / 6


def test_prompt_preserves_full_messages_and_tail_constraints():
    messages = tuple(InboundMessage(str(i), "A1", "x" * 13000 + "DO NOT SEND") for i in range(32))
    payload = json.loads(prompt_for(snapshot(), messages))
    assert len(payload["messages"]) == 32
    assert all(m["content"] == messages[i].content for i, m in enumerate(payload["messages"]))
    assert all(m["content"].endswith("DO NOT SEND") for m in payload["messages"])
