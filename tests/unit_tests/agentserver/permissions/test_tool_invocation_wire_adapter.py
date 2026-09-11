from __future__ import annotations

import pytest
from openjiuwen.core.single_agent.interrupt.response import InterruptRequest

from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue import (
    RootPermissionQueue,
    RootPermissionQueueError,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.agent_adapter.permission_dispatch import RootPermissionDispatch


def _smart_owner():
    """Use the actual Smart consumer, not stricter rules in the manual parser."""
    adapter = JiuWenSwarmDeepAdapter()
    adapter.mark_as_session_scoped("wire-session")
    adapter._enable_auto_permission = True
    queue = RootPermissionQueue(id_factory=lambda: "batch-invocation-1")
    adapter._root_permission_queue = queue
    adapter._permission_dispatch = RootPermissionDispatch(queue)
    card = queue.begin(
        root_session_id="wire-session", request_id="wire-request",
        execution_session_id="wire-session", tool_call_id="batch-call-1", tool_name="bash",
    )
    card = queue.mark_pending(
        card.key, request=InterruptRequest(
            message="Approve?", metadata={"tool_invocation_key": card.key.to_wire()},
        ), auto_manual=True,
        root_context={"request_id": "wire-request"},
    )
    queue.reconcile(
        {"result_type": "interrupt", "interrupt_ids": [card.key.tool_call_id],
         "state": [{"id": card.key.tool_call_id, "value": card.request}]},
        root_session_id="wire-session",
    )
    request = AgentRequest(
        request_id="answer-request", channel_id="web", session_id="wire-session",
        req_method=ReqMethod.CHAT_SEND, params={},
    )
    return adapter, request, card


def test_permission_answer_uses_only_opaque_card_id() -> None:
    interactive = JiuWenSwarm._build_interactive_input_from_answers(
        "batch-call-1",
        [
            {
                "selected_options": ["本次允许"],
                "card_id": " batch-invocation-1 ",
            },
        ],
        source="permission_interrupt",
    )

    assert interactive.user_inputs == {
        "batch-invocation-1": {
            "approved": True,
            "auto_confirm": False,
            "feedback": "",
        },
    }


def test_permission_multi_answer_keeps_manual_fallback_but_smart_consumer_rejects() -> None:
    interactive = JiuWenSwarm._build_interactive_input_from_answers(
        "batch-call-1",
        [
            {
                "selected_options": ["本次允许"],
                "card_id": "batch-invocation-1",
            },
            {
                "selected_options": ["拒绝"],
                "card_id": "batch-invocation-2",
            },
        ],
        source="permission_interrupt",
    )

    assert list(interactive.user_inputs) == ["batch-call-1"]
    adapter, request, card = _smart_owner()
    with pytest.raises(RootPermissionQueueError, match="non_head_answer"):
        adapter._prepare_permission_resume_dispatch(request, {"query": interactive})
    assert adapter._root_permission_queue.get(card.key) == card


@pytest.mark.parametrize(
    "answers",
    [
        [
            {
                "selected_options": ["本次允许"],
                "card_id": "batch-invocation-1",
            },
            {
                "selected_options": ["本次允许"],
                "card_id": "batch-invocation-2",
            },
        ],
        [
            {
                "selected_options": ["本次允许"],
                "card_id": "",
            }
        ],
        [
            {
                "selected_options": ["本次允许"],
                "tool_invocation_id": "batch-invocation-1",
            }
        ],
    ],
)
def test_malformed_permission_locator_cannot_resume_smart_head(
    answers: list[dict],
) -> None:
    interactive = JiuWenSwarm._build_interactive_input_from_answers(
        "batch-call-1",
        answers,
        source="permission_interrupt",
    )

    # Blank/missing card locators and multiple answers preserve develop's
    # tool-call fallback for ordinary manual interrupts. Smart requires the
    # current opaque card at the actual queue consumer, before SDK dispatch.
    assert list(interactive.user_inputs) == ["batch-call-1"]
    adapter, request, card = _smart_owner()
    with pytest.raises(RootPermissionQueueError, match="non_head_answer"):
        adapter._prepare_permission_resume_dispatch(request, {"query": interactive})
    assert adapter._root_permission_queue.get(card.key) == card


@pytest.mark.parametrize("source", ["", "permission_interrupt", "confirm_interrupt"])
def test_manual_interrupt_keeps_its_independent_request_protocol(source: str) -> None:
    interactive = JiuWenSwarm._build_interactive_input_from_answers(
        "confirm-call",
        [{"selected_options": ["本次允许"]}],
        source=source,
    )

    assert interactive.user_inputs == {
        "confirm-call": {
            "approved": True,
            "auto_confirm": False,
            "feedback": "",
        }
    }


def test_unknown_source_does_not_build_approval_input() -> None:
    result = JiuWenSwarm._build_interactive_input_from_answers(
        "call-1",
        [{"selected_options": ["本次允许"]}],
        source="unknown",
    )

    assert result is None


def test_smart_wire_card_round_trip_dispatches_exact_tool_call_once() -> None:
    adapter, request, card = _smart_owner()
    incoming = JiuWenSwarm._build_interactive_input_from_answers(
        card.key.tool_call_id,
        [{"selected_options": ["本次允许"], "card_id": card.key.invocation_id}],
        source="permission_interrupt",
    )
    prepared = adapter._prepare_permission_resume_dispatch(request, {"query": incoming})
    assert list(prepared["query"].user_inputs) == [card.key.tool_call_id]
    assert card.key.invocation_id != card.key.tool_call_id
    claimed = adapter._root_permission_queue.claim_answer_for_call(
        root_session_id="wire-session", execution_session_id="wire-session",
        tool_call_id=card.key.tool_call_id, tool_name="bash",
    )
    assert claimed.card.key == card.key
    assert adapter._root_permission_queue.finish(card.key)
    with pytest.raises(RootPermissionQueueError):
        adapter._prepare_permission_resume_dispatch(request, {"query": incoming})


@pytest.mark.parametrize("locator", ["foreign-card", "batch-call-1"])
def test_wrong_smart_card_locator_is_rejected_without_consuming_head(locator: str) -> None:
    adapter, request, card = _smart_owner()
    incoming = JiuWenSwarm._build_interactive_input_from_answers(
        card.key.tool_call_id, [{"selected_options": ["本次允许"], "card_id": locator}],
        source="permission_interrupt",
    )
    with pytest.raises(RootPermissionQueueError, match="non_head_answer"):
        adapter._prepare_permission_resume_dispatch(request, {"query": incoming})
    assert adapter._root_permission_queue.get(card.key) == card


def test_non_permission_answer_does_not_forward_locator() -> None:
    interactive = JiuWenSwarm._build_interactive_input_from_answers(
        "question-1",
        [
            {
                "question": "Continue?",
                "selected_options": ["Yes"],
                "tool_invocation_id": "invocation-1",
            }
        ],
        source="ask_user_interrupt",
    )

    assert interactive.user_inputs == {"question-1": {"answers": {"Continue?": "Yes"}}}


@pytest.mark.parametrize(
    ("request_id", "answers", "expected"),
    [
        ("", [{"question": "Continue?", "selected_options": ["Yes"]}], {"Continue?": "Yes"}),
        ("question-1", [{"selected_options": ["Yes"]}], {"__free_text__": "Yes"}),
        ("question-1", [{"custom_input": "Take the train"}], {"__free_text__": "Take the train"}),
    ],
)
def test_ordinary_ask_user_preserves_develop_answer_conversion(
    request_id: str,
    answers: list[dict],
    expected: dict,
) -> None:
    interactive = JiuWenSwarm._build_interactive_input_from_answers(
        request_id,
        answers,
        source="ask_user_interrupt",
    )

    assert interactive.raw_inputs is None
    assert interactive.user_inputs == {request_id: {"answers": expected}}


def test_ask_user_does_not_accept_original_request_legacy_argument() -> None:
    with pytest.raises(TypeError):
        JiuWenSwarm._build_interactive_input_from_answers(
            "question-1",
            [{"question": "Continue?", "selected_options": ["Yes"]}],
            source="ask_user_interrupt",
            original_request="legacy authorization text",
        )
