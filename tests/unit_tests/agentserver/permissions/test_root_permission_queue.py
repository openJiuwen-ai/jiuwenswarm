# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openjiuwen.core.runner.callback import AbortError
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.interrupt.response import InterruptRequest
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    ToolCallInputs,
)

from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue import (
    RootPermissionQueue,
    RootPermissionQueueError,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
    ROOT_NON_PERMISSION_RESUME_DTO_KEY,
    ROOT_PERMISSION_QUEUE_USER_INPUT_KEY,
    RootNonPermissionResume,
    RootPermissionCompletionRail,
    RootPermissionQueueRail,
    bind_root_permission_request,
    root_nonpermission_resume_from_context,
    reset_root_permission_request,
    root_permission_resume_from_context,
)


def _begin(queue: RootPermissionQueue, tool_call_id: str):
    return queue.begin(
        root_session_id="root-session",
        request_id="root-request",
        execution_session_id="root-session",
        tool_call_id=tool_call_id,
        tool_name="bash",
    )


def _pending(queue: RootPermissionQueue, tool_call_id: str):
    card = _begin(queue, tool_call_id)
    request = InterruptRequest(
        message=f"approve {tool_call_id}",
        metadata={"tool_invocation_key": card.key.to_wire()},
    )
    return queue.mark_pending(
        card.key,
        request=request,
        auto_manual=True,
        root_context={"request_id": "root-request"},
    )


def _interaction(card):
    return {
        "id": card.key.tool_call_id,
        "value": card.request,
    }


def _answer(card, *, approved: bool = True) -> InteractiveInput:
    incoming = InteractiveInput()
    incoming.update(
        card.key.invocation_id,
        {
            "approved": approved,
            "auto_confirm": False,
            "feedback": "",
        },
    )
    return incoming


def _context(tool_call_id: str, *, extra: dict | None = None) -> AgentCallbackContext:
    tool_call = SimpleNamespace(
        id=tool_call_id,
        name="bash",
        arguments={"command": "pwd"},
    )
    return AgentCallbackContext(
        agent=SimpleNamespace(),
        inputs=ToolCallInputs(
            tool_call=tool_call,
            tool_name="bash",
            tool_args=tool_call.arguments,
        ),
        session=SimpleNamespace(session_id="root-session"),
        extra=extra or {},
    )


def _nonpermission_context(
    tool_call_id: str,
    tool_name: str,
    *,
    marker: object,
) -> AgentCallbackContext:
    ctx = _context(
        tool_call_id,
        extra={
            ROOT_PERMISSION_QUEUE_USER_INPUT_KEY: {
                tool_call_id: {"approved": True, "auto_confirm": True}
            }
        },
    )
    ctx.inputs.tool_name = tool_name
    ctx.inputs.tool_call.name = tool_name
    ctx.inputs.run_context = {"extra": {ROOT_NON_PERMISSION_RESUME_DTO_KEY: marker}}
    return ctx


def test_sparse_resume_dispatches_only_head_then_keeps_sibling_pending() -> None:
    ids = iter(("invocation-1", "invocation-2"))
    queue = RootPermissionQueue(id_factory=lambda: next(ids))
    first = _pending(queue, "call-1")
    second = _pending(queue, "call-2")
    initial = queue.reconcile(
        {
            "result_type": "interrupt",
            "interrupt_ids": ["call-1", "call-2"],
            "state": [_interaction(first), _interaction(second)],
        },
        root_session_id="root-session",
    )

    assert initial is not None
    assert initial.cards == (first, second)
    reserved = queue.reserve_answer("root-session", _answer(first))
    assert reserved.interactive_input.user_inputs == {
        "call-1": dict(reserved.payload)
    }

    claimed = queue.claim_answer_for_call(
        root_session_id="root-session",
        execution_session_id="root-session",
        tool_call_id="call-1",
        tool_name="bash",
    )
    assert claimed.quarantined is False
    assert queue.finish(claimed.card.key) is True

    remaining = queue.reconcile(
        {
            "result_type": "interrupt",
            "interrupt_ids": ["call-2"],
            "state": [_interaction(second)],
        },
        root_session_id="root-session",
    )

    assert remaining is not None
    assert [card.key.tool_call_id for card in remaining.cards] == ["call-2"]
    assert queue.get(first.key) is None
    assert queue.get(second.key) == second


def test_non_head_multi_and_foreign_answers_fail_closed() -> None:
    ids = iter(("invocation-1", "invocation-2"))
    queue = RootPermissionQueue(id_factory=lambda: next(ids))
    first = _pending(queue, "call-1")
    second = _pending(queue, "call-2")
    queue.reconcile(
        {
            "result_type": "interrupt",
            "interrupt_ids": ["call-1", "call-2"],
            "state": [_interaction(first), _interaction(second)],
        },
        root_session_id="root-session",
    )

    with pytest.raises(RootPermissionQueueError, match="non_head_answer"):
        queue.reserve_answer("root-session", _answer(second))

    multiple = _answer(first)
    multiple.update(
        second.key.invocation_id,
        dict(_answer(second).user_inputs[second.key.invocation_id]),
    )
    with pytest.raises(RootPermissionQueueError, match="one_answer_required"):
        queue.reserve_answer("root-session", multiple)

    forged = InteractiveInput()
    forged.update(
        "foreign",
        {"approved": True, "auto_confirm": False, "feedback": ""},
    )
    with pytest.raises(RootPermissionQueueError, match="non_head_answer"):
        queue.reserve_answer("root-session", forged)


@pytest.mark.asyncio
async def test_missing_sparse_sibling_rethrows_frozen_card_before_routing() -> None:
    queue = RootPermissionQueue(id_factory=lambda: "invocation-1")
    pending = _pending(queue, "call-1")
    rail = RootPermissionQueueRail(queue)
    token = bind_root_permission_request(
        root_session_id="root-session",
        request_id="resume-request",
        enabled=True,
        queue=queue,
    )
    try:
        ctx = _context("call-1")
        with pytest.raises(AbortError) as raised:
            await rail.before_tool_call(ctx)
    finally:
        reset_root_permission_request(token)

    assert raised.value.cause.request == pending.request
    assert raised.value.cause.request is not pending.request


@pytest.mark.asyncio
async def test_reserved_head_binds_exact_queue_resume() -> None:
    queue = RootPermissionQueue(id_factory=lambda: "invocation-1")
    pending = _pending(queue, "call-1")
    queue.reconcile(
        {
            "result_type": "interrupt",
            "interrupt_ids": ["call-1"],
            "state": [_interaction(pending)],
        },
        root_session_id="root-session",
    )
    answer = queue.reserve_answer("root-session", _answer(pending))
    claimed = []
    rail = RootPermissionQueueRail(queue, answer_claimed=claimed.append)
    token = bind_root_permission_request(
        root_session_id="root-session",
        request_id="resume-request",
        enabled=True,
        queue=queue,
    )
    try:
        ctx = _context(
            "call-1",
            extra={ROOT_PERMISSION_QUEUE_USER_INPUT_KEY: answer.interactive_input},
        )
        await rail.before_tool_call(ctx)
    finally:
        reset_root_permission_request(token)

    resume = root_permission_resume_from_context(ctx)
    assert resume is not None
    assert resume.card.key == answer.card.key
    assert resume.card.state == "active"
    assert resume.user_input == answer.payload
    assert claimed == [answer.card.key]

    completion = RootPermissionCompletionRail(queue)
    await completion.before_tool_call(ctx)
    assert queue.get(answer.card.key) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["ask_user", "exit_plan_mode"])
async def test_typed_nonpermission_resume_skips_queue_for_current_control_tools(
    tool_name: str,
) -> None:
    queue = RootPermissionQueue()
    rail = RootPermissionQueueRail(queue)
    marker = RootNonPermissionResume("root-session", "call-1", tool_name)
    token = bind_root_permission_request(
        root_session_id="root-session",
        request_id="resume-request",
        enabled=True,
        queue=queue,
    )
    try:
        ctx = _nonpermission_context("call-1", tool_name, marker=marker)
        await rail.before_tool_call(ctx)
    finally:
        reset_root_permission_request(token)

    assert root_nonpermission_resume_from_context(ctx) == marker
    assert queue.has_live(root_session_id="root-session") is False
    assert ctx.inputs.run_context["extra"] == {}
    assert ctx.extra[ROOT_PERMISSION_QUEUE_USER_INPUT_KEY]["call-1"] == {
        "approved": True,
        "auto_confirm": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("marker", "error"),
    [
        ({"tool_call_id": "call-1"}, "nonpermission_resume_marker_invalid"),
        (
            RootNonPermissionResume("foreign-session", "call-1", "exit_plan_mode"),
            "nonpermission_resume_identity_mismatch",
        ),
        (
            RootNonPermissionResume("root-session", "foreign-call", "exit_plan_mode"),
            "nonpermission_resume_identity_mismatch",
        ),
        (
            RootNonPermissionResume("root-session", "call-1", "foreign-tool"),
            "nonpermission_resume_identity_mismatch",
        ),
    ],
)
async def test_nonpermission_resume_marker_mismatch_fails_closed(
    marker: object,
    error: str,
) -> None:
    queue = RootPermissionQueue()
    rail = RootPermissionQueueRail(queue)
    token = bind_root_permission_request(
        root_session_id="root-session",
        request_id="resume-request",
        enabled=True,
        queue=queue,
    )
    try:
        ctx = _nonpermission_context("call-1", "exit_plan_mode", marker=marker)
        with pytest.raises(RootPermissionQueueError, match=error):
            await rail.before_tool_call(ctx)
    finally:
        reset_root_permission_request(token)

    assert ctx.inputs.run_context["extra"] == {}
    assert root_nonpermission_resume_from_context(ctx) is None


@pytest.mark.asyncio
async def test_nonpermission_resume_conflicts_with_live_permission_scope() -> None:
    queue = RootPermissionQueue()
    _begin(queue, "permission-call")
    rail = RootPermissionQueueRail(queue)
    token = bind_root_permission_request(
        root_session_id="root-session",
        request_id="resume-request",
        enabled=True,
        queue=queue,
    )
    try:
        ctx = _nonpermission_context(
            "call-1",
            "exit_plan_mode",
            marker=RootNonPermissionResume("root-session", "call-1", "exit_plan_mode"),
        )
        with pytest.raises(
            RootPermissionQueueError,
            match="nonpermission_resume_permission_conflict",
        ):
            await rail.before_tool_call(ctx)
    finally:
        reset_root_permission_request(token)


@pytest.mark.asyncio
async def test_nonpermission_resume_marker_is_single_use() -> None:
    queue = RootPermissionQueue()
    rail = RootPermissionQueueRail(queue)
    token = bind_root_permission_request(
        root_session_id="root-session",
        request_id="resume-request",
        enabled=True,
        queue=queue,
    )
    try:
        ctx = _nonpermission_context(
            "call-1",
            "exit_plan_mode",
            marker=RootNonPermissionResume("root-session", "call-1", "exit_plan_mode"),
        )
        await rail.before_tool_call(ctx)
        with pytest.raises(
            RootPermissionQueueError,
            match="permission_queue_answer_not_reserved",
        ):
            await rail.before_tool_call(ctx)
    finally:
        reset_root_permission_request(token)


@pytest.mark.asyncio
async def test_nonpermission_marker_without_core_resume_input_fails_closed() -> None:
    queue = RootPermissionQueue()
    rail = RootPermissionQueueRail(queue)
    token = bind_root_permission_request(
        root_session_id="root-session",
        request_id="resume-request",
        enabled=True,
        queue=queue,
    )
    try:
        ctx = _context("call-1")
        ctx.inputs.run_context = {
            "extra": {
                ROOT_NON_PERMISSION_RESUME_DTO_KEY: RootNonPermissionResume(
                    "root-session", "call-1", "bash"
                )
            }
        }
        with pytest.raises(
            RootPermissionQueueError,
            match="nonpermission_resume_input_missing",
        ):
            await rail.before_tool_call(ctx)
    finally:
        reset_root_permission_request(token)

    assert queue.has_live(root_session_id="root-session") is False


@pytest.mark.asyncio
async def test_quarantined_late_callback_consumes_exact_answer_before_routing() -> None:
    queue = RootPermissionQueue(id_factory=lambda: "invocation-1")
    pending = _pending(queue, "call-1")
    queue.reconcile(
        {
            "result_type": "interrupt",
            "interrupt_ids": ["call-1"],
            "state": [_interaction(pending)],
        },
        root_session_id="root-session",
    )
    answer = queue.reserve_answer("root-session", _answer(pending))
    assert queue.begin_cutover(root_session_id="root-session") is True
    rail = RootPermissionQueueRail(queue)
    token = bind_root_permission_request(
        root_session_id="root-session",
        request_id="resume-request",
        enabled=True,
        queue=queue,
    )
    try:
        ctx = _context(
            "call-1",
            extra={ROOT_PERMISSION_QUEUE_USER_INPUT_KEY: answer.interactive_input},
        )
        with pytest.raises(
            RootPermissionQueueError,
            match="permission_queue_resume_superseded",
        ):
            await rail.before_tool_call(ctx)
    finally:
        reset_root_permission_request(token)

    assert queue.get(answer.card.key) is None


def test_invalid_mixed_snapshot_quarantines_root() -> None:
    queue = RootPermissionQueue(id_factory=lambda: "invocation-1")
    pending = _pending(queue, "call-1")
    with pytest.raises(RootPermissionQueueError, match="mixed_interrupt"):
        queue.reconcile(
            {
                "result_type": "interrupt",
                "interrupt_ids": ["call-1", "ask-1"],
                "state": [
                    _interaction(pending),
                    {"id": "ask-1", "value": {"questions": []}},
                ],
            },
            root_session_id="root-session",
        )

    with pytest.raises(RootPermissionQueueError, match="quarantined"):
        queue.begin(
            root_session_id="root-session",
            request_id="new-request",
            execution_session_id="root-session",
            tool_call_id="call-2",
            tool_name="bash",
        )


def test_cutover_consumes_only_exact_accepted_handoff() -> None:
    ids = iter(("invocation-1", "invocation-2", "invocation-3"))
    queue = RootPermissionQueue(id_factory=lambda: next(ids))
    first = _pending(queue, "call-1")
    second = _pending(queue, "call-2")
    queue.reconcile(
        {
            "result_type": "interrupt",
            "interrupt_ids": ["call-1", "call-2"],
            "state": [_interaction(first), _interaction(second)],
        },
        root_session_id="root-session",
    )
    answer = queue.reserve_answer("root-session", _answer(first))

    assert queue.begin_cutover(root_session_id="root-session") is True
    assert queue.get(first.key).state == "resuming"
    assert queue.get(second.key) == second
    with pytest.raises(RootPermissionQueueError, match="quarantined"):
        queue.reserve_answer("root-session", _answer(second))

    assert queue.consume_accepted_answer_if_quarantined(answer) is True
    frozen = queue.cutover_continuation_scope(root_session_id="root-session")
    assert frozen == (second.key,)
    assert queue.discard_continuation(frozen, root_session_id="root-session") == 1
    replacement = _begin(queue, "call-3")
    assert replacement.state == "active"


def test_resuming_answer_cannot_be_marked_pending_without_callback_claim() -> None:
    queue = RootPermissionQueue(id_factory=lambda: "invocation-1")
    pending = _pending(queue, "call-1")
    queue.reconcile(
        {
            "result_type": "interrupt",
            "interrupt_ids": ["call-1"],
            "state": [_interaction(pending)],
        },
        root_session_id="root-session",
    )
    answer = queue.reserve_answer("root-session", _answer(pending))

    with pytest.raises(RootPermissionQueueError, match="pending_transition"):
        queue.mark_pending(
            answer.card.key,
            request=pending.request,
            auto_manual=pending.auto_manual,
            root_context=pending.root_context,
        )


def test_cutover_waits_for_active_callback_cleanup() -> None:
    ids = iter(("invocation-1", "invocation-2", "invocation-3"))
    queue = RootPermissionQueue(id_factory=lambda: next(ids))
    first = _begin(queue, "call-1")
    second = _begin(queue, "call-2")

    assert queue.begin_cutover(root_session_id="root-session") is True
    with pytest.raises(RootPermissionQueueError, match="quarantined"):
        _begin(queue, "call-3")
    with pytest.raises(RootPermissionQueueError, match="active_cleanup_pending"):
        queue.cutover_continuation_scope(root_session_id="root-session")
    queue.finish(first.key)
    queue.finish(second.key)
    frozen = queue.cutover_continuation_scope(root_session_id="root-session")
    assert frozen == ()
    assert queue.discard_continuation(frozen, root_session_id="root-session") == 0
    assert _begin(queue, "call-3").state == "active"


def test_finish_active_cannot_consume_pending_or_resuming_cards() -> None:
    ids = iter(("invocation-1", "invocation-2"))
    queue = RootPermissionQueue(id_factory=lambda: next(ids))
    active = _begin(queue, "call-1")
    pending = _pending(queue, "call-2")

    assert queue.finish_active(active.key) is True
    assert queue.get(active.key) is None
    assert queue.finish_active(pending.key) is False
    assert queue.get(pending.key) == pending

    queue.reconcile(
        {
            "result_type": "interrupt",
            "interrupt_ids": ["call-2"],
            "state": [_interaction(pending)],
        },
        root_session_id="root-session",
    )
    answer = queue.reserve_answer("root-session", _answer(pending))

    assert queue.finish_active(answer.card.key) is False
    assert queue.get(answer.card.key) == answer.card
