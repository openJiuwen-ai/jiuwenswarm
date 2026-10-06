from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.duplex_router import (
    ControlSnapshot, InboundMessage, Observation, observe, prompt_for,
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
    result = await observe(s, MESSAGES, classify=AsyncMock(return_value=decision(s)))
    assert result.proposed_action == "INTERRUPT"
    assert result.effective_action == "UNCHANGED"
    assert result.message_ids == ("m204",)
    assert result.status == "ok"






@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, "INTERRUPT", {},
    dict(action="DELETE"),
    dict(action="INTERRUPT", context_version="v0", round_id="r1", checkpoint_id="c1")])
async def test_invalid_response_produces_no_decision(response):
    s = snapshot()
    result = await observe(s, MESSAGES, classify=AsyncMock(return_value=response))
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
                            timeout_seconds=0.01)
    assert result.status == "timeout"
    assert cancelled.is_set()
    task = asyncio.create_task(observe(s, MESSAGES, classify=blocked))
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


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["jev", "clef"])
@pytest.mark.parametrize("proposed,status,interrupt_result,expected", [
    ("INTERRUPT", "ok", "INTERRUPT", "action=INTERRUPT"),
    ("APPEND", "ok", None, "action=APPEND reason=model_append"),
    ("UNDECIDED", "timeout", None, "action=APPEND reason=timeout"),
    ("INTERRUPT", "ok", "UNAVAILABLE", "action=APPEND reason=inactive_or_pending"),
])
async def test_choice_route_logs_observation_and_effective_action(
        monkeypatch, backend, proposed, status, interrupt_result, expected):
    from jiuwenswarm.agents.harness.team import duplex_native

    class FakeNative:
        _duplex_received = set()

        def __init__(self):
            self.interrupt = AsyncMock(return_value=interrupt_result)

    native = FakeNative()
    monkeypatch.setattr(duplex_native, "DuplexNativeHarness", FakeNative)
    monkeypatch.setattr(shadow, "native_from_runtime", lambda _: native)
    monkeypatch.setattr(shadow, "snapshot_from_native", lambda _: snapshot())
    monkeypatch.setattr(shadow, "observe", AsyncMock(return_value=Observation(
        ("m204",), "v1", "r1", "c1", proposed, "UNCHANGED", status, 12.5, 1)))
    log = Mock()
    monkeypatch.setattr(shadow.logger, "info", log)
    original = AsyncMock(return_value="sent")
    content = shadow.RoutedInput("Customer forbids Kafka", MESSAGES[0])

    await shadow.deliver_routed(NS(harness=native), content, use_steer=True,
                                original=original, settings={"mode": "active", "policy": "model",
                                                             "backend": backend, "timeout_seconds": 2})

    rendered = [call.args[0] % call.args[1:] for call in log.call_args_list]
    assert any(f"duplex route observation backend={backend} message_id=m204" in line for line in rendered)
    assert any(expected in line for line in rendered)
    assert all("Customer forbids Kafka" not in line for line in rendered)
    if expected == "action=INTERRUPT":
        native.interrupt.assert_awaited_once()
        original.assert_not_awaited()
    else:
        original.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision_result,expected", [
    ({"action": "INTERRUPT"}, "INTERRUPT"),
    ({"action": "APPEND"}, "APPEND"),
    (RuntimeError("Clef unavailable"), "APPEND"),
])
async def test_clef_backend_routes_and_falls_back(
        monkeypatch, decision_result, expected):
    from jiuwenswarm.agents.harness.team import duplex_native
    from jiuwenswarm.common import duplex_clef

    class FakeNative:
        _duplex_received = set()

        def __init__(self):
            self.interrupt = AsyncMock(return_value="INTERRUPT")

    native = FakeNative()
    classify = AsyncMock(side_effect=decision_result if isinstance(decision_result, Exception) else None,
                         return_value=decision_result if isinstance(decision_result, dict) else None)
    monkeypatch.setattr(duplex_native, "DuplexNativeHarness", FakeNative)
    monkeypatch.setattr(shadow, "native_from_runtime", lambda _: native)
    monkeypatch.setattr(shadow, "snapshot_from_native", lambda _: snapshot())
    monkeypatch.setattr(duplex_clef, "classify_clef", classify)
    original = AsyncMock(return_value="sent")
    config = {"mode": "active", "policy": "model", "backend": "clef",
              "timeout_seconds": 1.5, "clef": {"model": "clef-flash"}}

    result = await shadow.deliver_routed(
        NS(harness=native), shadow.RoutedInput("Customer forbids Kafka", MESSAGES[0]),
        use_steer=True, original=original, settings=config)

    classify.assert_awaited_once()
    assert classify.await_args.kwargs == {"settings": config["clef"], "timeout_seconds": 1.5}
    if expected == "INTERRUPT":
        assert result == "INTERRUPT"
        native.interrupt.assert_awaited_once()
        original.assert_not_awaited()
    else:
        assert result == "sent"
        native.interrupt.assert_not_awaited()
        original.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,use_steer,policy,has_snapshot,reason", [
    ("off", True, "model", True, "mode_not_active"),
    ("active", False, "model", True, "use_steer_false"),
    ("active", True, "steer", True, "policy_bypass"),
    ("active", True, "model", False, "no_snapshot"),
])
async def test_route_bypass_logs_reason_without_content(monkeypatch, mode, use_steer, policy, has_snapshot, reason):
    from jiuwenswarm.agents.harness.team import duplex_native

    class FakeNative:
        def __init__(self):
            self._duplex_received = set()

    native = FakeNative()
    monkeypatch.setattr(duplex_native, "DuplexNativeHarness", FakeNative)
    monkeypatch.setattr(shadow, "native_from_runtime", lambda _: native)
    monkeypatch.setattr(shadow, "snapshot_from_native", lambda _: snapshot() if has_snapshot else None)
    log = Mock()
    monkeypatch.setattr(shadow.logger, "info", log)
    original = AsyncMock(return_value="sent")
    content = shadow.RoutedInput("private-message-never-log", MESSAGES[0])
    result = await shadow.deliver_routed(NS(harness=native, blueprint=NS(member_name="A2")), content,
                                        use_steer=use_steer, original=original,
                                        settings={"mode": mode, "policy": policy, "backend": "clef"})
    assert result == "sent"
    original.assert_awaited_once()
    logged = "\n".join(call.args[0] % call.args[1:] for call in log.call_args_list)
    assert f"reason={reason}" in logged
    assert "recipient=A2" in logged
    assert "private-message-never-log" not in logged
