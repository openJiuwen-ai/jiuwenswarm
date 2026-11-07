"""Real Host/Core approval round trips with a scripted model and harmless tool."""
from __future__ import annotations

import json
import pytest
from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall
from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY
from jiuwenswarm.agents.harness.common.rails.interrupt import interrupt_helpers
from tests.unit_tests.agentserver.permissions.test_permission_answer_cutover import _request, answer_host
from tests.unit_tests.agentserver.permissions.test_permission_cold_build import cold

def project(h):
    state = h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY)
    assert state and state.interrupted_tools
    interactions = []
    for interrupted in state.interrupted_tools.values():
        interactions.extend({"id": key, "value": value} for key, value in interrupted.interrupt_requests.items())
    queue = h.adapter._root_permission_queue
    if queue is not None and queue.snapshot_scope(root_session_id=h.adapter._parent_session_id):
        snapshot = queue.reconcile(
            {"result_type": "interrupt", "state": interactions, "interrupt_ids": [x["id"] for x in interactions]},
            root_session_id=h.adapter._parent_session_id,
        )
        assert snapshot and snapshot.cards
        first = snapshot.cards[0]
        matching = next(x for x in interactions if x["id"] == first.key.tool_call_id)
        return interrupt_helpers.build_verified_permission_ask_user_question(matching, first)
    return interrupt_helpers.convert_interactions_to_ask_user_question(interactions)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["manual", "auto"])
async def test_actual_host_core_two_cards_then_next_turn_reusing_call_id(answer_host, monkeypatch, mode):
    h = answer_host
    h.raw["permissions"]["mode"] = mode
    await h.create()
    await h.adapter.start_interaction(h.adapter._parent_session_id)
    trace = []
    for turn, calls in [(1, ["call_A", "call_B"]), (2, ["call_A"])]:
        h.script.responses = [
            AssistantMessage(content="", tool_calls=[ToolCall(
                id=call_id, type="function", name=h.tool.card.name,
                arguments=json.dumps({"value": f"turn-{turn}-{call_id}"}),
            ) for call_id in calls]),
            AssistantMessage(content="completed"),
        ]
        result = await _request(h, query=f"Run turn {turn}")
        assert result.ok
        for call_id in calls:
            payload = project(h)
            assert payload["request_id"] == call_id
            question = payload["questions"][0]
            if mode == "auto":
                assert question["card_id"] == call_id
            else:
                assert "card_id" not in question
            trace.append({"turn": turn, "request_id": payload["request_id"], "card_id": question.get("card_id")})
            answer = {"selected_options": ["allow_once"]}
            if mode == "auto":
                answer["card_id"] = question["card_id"]
            result = await _request(h, source=payload["source"], request_id=payload["request_id"], answers=[answer])
            assert result.ok
        state = h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY)
        assert not state or not state.interrupted_tools
    assert h.executions == [{"value": "turn-1-call_A"}, {"value": "turn-1-call_B"}, {"value": "turn-2-call_A"}]
    assert [item["request_id"] for item in trace] == ["call_A", "call_B", "call_A"]


