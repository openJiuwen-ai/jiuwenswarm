# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""OpenJiuwen permission-continuation operations, without a Host adapter owner."""
from __future__ import annotations

import copy
import logging
from collections.abc import Collection, Iterator, Mapping
from typing import Any, NamedTuple

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.foundation.tool import ClientToolResult, ClientToolStatus, default_client_output
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.interrupt.response import InterruptKind, interrupt_kind_of
from openjiuwen.core.single_agent.interrupt.state import (
    INTERRUPTION_KEY,
    PendingInterrupt,
    match_answers,
    pending_interrupts,
)

from jiuwenswarm.agents.harness.common.rails.permissions.permission_interrupt_rail import (
    JiuwenSwarmPermissionInterruptRail,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_ask_user import (
    ASK_USER_TOOL_NAME, ask_user_continuation, prepare_ask_user_resume,
    put_ask_user_resume_in_inputs,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue import (
    RootPermissionAnswer, RootPermissionQueue, RootPermissionQueueError,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
    RootNonPermissionResume, RootPermissionWrapperResume, put_root_nonpermission_resume_in_inputs,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_invocation_key import ToolInvocationKeyV1
from jiuwenswarm.common.tool_display import extract_call_goal
from jiuwenswarm.runtime.client_tools import ClientToolAnswerMismatch

logger = logging.getLogger(__name__)


def _is_client_tool_request(request: Any) -> bool:
    return interrupt_kind_of(request) == InterruptKind.CLIENT_TOOL


_SUPERSEDED = "[INTERRUPTED - Superseded by new user input]"


def _superseded_message(tool_call_id: str, tool_name: str, client_tool_ids: Collection[str]) -> ToolMessage:
    """The result of a paused call fresh user input dropped; the client may already have run a client tool call."""
    content = _SUPERSEDED
    if tool_call_id in client_tool_ids:
        cancelled = default_client_output(ClientToolResult(status=ClientToolStatus.CANCELLED), tool_name)
        content = f"{_SUPERSEDED} {cancelled.error}"
    return ToolMessage(tool_call_id=tool_call_id, content=content)


def _permission_entries(state: Any) -> Iterator[tuple[Any, ToolInvocationKeyV1]]:
    """Read inner identities from Core, retaining outer calls for context cleanup."""
    interrupted = getattr(state, "interrupted_tools", None)
    if not isinstance(interrupted, Mapping) or not interrupted:
        raise RootPermissionQueueError("permission_continuation_state_missing")
    for outer_id, entry in interrupted.items():
        call = getattr(entry, "tool_call", None)
        requests = getattr(entry, "interrupt_requests", None)
        if (
            not isinstance(requests, Mapping) or len(requests) != 1
            or getattr(call, "id", None) != outer_id
        ):
            raise RootPermissionQueueError("permission_continuation_state_invalid")
        inner_id, request = next(iter(requests.items()))
        if _is_client_tool_request(request):
            continue  # a client tool call of the same pause holds no card
        metadata = getattr(request, "metadata", None)
        key = ToolInvocationKeyV1.from_wire(
            metadata.get("tool_invocation_key") if isinstance(metadata, Mapping) else None
        )
        if key.tool_call_id != inner_id or (inner_id != outer_id and call.name != "tool_call"):
            raise RootPermissionQueueError("permission_continuation_identity_mismatch")
        yield call, key


def prepare_permission_wrappers(
    loop_session: Any, queue: RootPermissionQueue, answer: RootPermissionAnswer,
) -> tuple[RootPermissionWrapperResume, ...]:
    """Carry one-shot references for Core's replay batch, not a second state map."""
    state = loop_session.get_state(INTERRUPTION_KEY) if loop_session is not None else None
    if state is None:
        return ()
    wrappers, keys = [], []
    for call, key in _permission_entries(state):
        card = queue.get(key)
        if (
            card is None or card.state not in {"pending", "resuming"}
            or key.root_session_id != answer.card.key.root_session_id
        ):
            raise RootPermissionQueueError("permission_continuation_card_mismatch")
        keys.append(key)
        if call.id != key.tool_call_id:
            wrappers.append(RootPermissionWrapperResume(call, card))
    if answer.card.key not in keys or len(set(keys)) != len(keys):
        raise RootPermissionQueueError("permission_continuation_answer_mismatch")
    return tuple(wrappers)


async def discard_permission_continuation(
    instance: Any,
    target_sid: str,
    loop_session_id: str | None,
    frozen_keys: tuple[ToolInvocationKeyV1, ...],
) -> bool:
    """Discard one exact Core permission continuation and balance its context."""
    loop_session = getattr(instance, "_loop_session", None)
    if (
        loop_session is None
        or loop_session_id != target_sid
        or not frozen_keys
    ):
        return False

    try:
        state = loop_session.get_state(INTERRUPTION_KEY)
        interrupted_tools = getattr(state, "interrupted_tools", None)
        if not isinstance(interrupted_tools, Mapping) or not interrupted_tools:
            return False

        frozen = {key.invocation_id: key for key in frozen_keys}
        if len(frozen) != len(frozen_keys):
            return False
        pending: dict[str, ToolInvocationKeyV1] = {}
        pending_tool_ids: list[str] = []
        for tool_call, key in _permission_entries(state):
            tool_call_id = tool_call.id
            if (
                key.invocation_id in pending
                or key.root_session_id != target_sid
            ):
                return False
            pending[key.invocation_id] = key
            pending_tool_ids.append(tool_call_id)
        if pending != frozen:
            return False
        # Client tool calls of the same pause are superseded with the cards.
        client_tool_ids = [call.tool_call_id for call in _paused_client_tool_calls(state)]
        pending_tool_ids.extend(client_tool_ids)

        ai_message = getattr(state, "ai_message", None)
        state_calls = list(getattr(ai_message, "tool_calls", None) or [])
        state_signature = [
            (
                str(getattr(call, "id", "") or ""),
                str(getattr(call, "name", "") or ""),
            )
            for call in state_calls
        ]
        all_tool_ids = {tool_call_id for tool_call_id, _name in state_signature}
        if (
            not state_signature
            or len(all_tool_ids) != len(state_signature)
            or not set(pending_tool_ids).issubset(all_tool_ids)
        ):
            return False

        react_agent = getattr(instance, "react_agent", None)
        context_engine = getattr(react_agent, "context_engine", None)
        context = (
            context_engine.get_context(session_id=target_sid)
            if context_engine is not None
            else None
        )
        messages = list(context.get_messages() or []) if context is not None else []
        assistant_index = next(
            (
                index
                for index in range(len(messages) - 1, -1, -1)
                if getattr(messages[index], "tool_calls", None)
            ),
            -1,
        )
        if assistant_index < 0:
            return False
        context_signature: list[tuple[str, str]] = []
        for call in list(
            getattr(messages[assistant_index], "tool_calls", None) or []
        ):
            context_signature.append(
                (
                    str(getattr(call, "id", "") or ""),
                    str(getattr(call, "name", "") or ""),
                )
            )
        if context_signature != state_signature:
            return False
        pending_id_set = set(pending_tool_ids)
        completed_ids = all_tool_ids - pending_id_set
        tail_ids: list[str] = []
        for message in messages[assistant_index + 1:]:
            tool_call_id = str(getattr(message, "tool_call_id", "") or "")
            if (
                getattr(message, "role", None) != "tool"
                or tool_call_id not in all_tool_ids
                or tool_call_id in pending_id_set
            ):
                return False
            tail_ids.append(tool_call_id)
        if len(tail_ids) != len(completed_ids) or set(tail_ids) != completed_ids:
            return False

        added = 0
        try:
            names = dict(state_signature)
            for tool_call_id in pending_tool_ids:
                await context.add_messages(
                    _superseded_message(tool_call_id, names[tool_call_id], client_tool_ids)
                )
                added += 1
            loop_session.update_state({INTERRUPTION_KEY: None})
            await context_engine.save_contexts(loop_session)
        except Exception:
            loop_session.update_state({INTERRUPTION_KEY: state})
            if added:
                context.pop_messages(added, with_history=True)
            raise
    except Exception:
        logger.debug(
            "[PermissionContinuation] exact permission continuation discard failed "
            "session=%s",
            target_sid,
            exc_info=True,
        )
        return False

    logger.info(
        "[PermissionContinuation] discarded exact permission continuation "
        "session=%s cards=%d",
        target_sid,
        len(frozen_keys),
    )
    return True


_UNPROVEN = object()


class _PendingInterruptSplit(NamedTuple):
    loop_session: Any
    state: Any
    context: Any
    context_engine: Any
    active_ids: list[str]
    stale_ids: list[str]


def _split_pending_interrupt(instance: Any, target_sid: str, loop_session_id: str | None):
    """Split Core's pending interrupt into live and already-answered tool calls.

    Returns ``None`` when *target_sid* has no pending interrupt, ``_UNPROVEN``
    when the interrupt cannot be matched to the context, otherwise
    ``(loop_session, state, context, context_engine, active_ids, stale_ids)``.
    A tool call is stale when its result is already in the context tail.
    """
    loop_session = getattr(instance, "_loop_session", None)
    if loop_session is None or loop_session_id != target_sid:
        return None
    state = loop_session.get_state(INTERRUPTION_KEY)
    interrupted_tools = getattr(state, "interrupted_tools", None)
    if not isinstance(interrupted_tools, Mapping) or not interrupted_tools:
        return None

    pending_tool_ids: list[str] = []
    for outer_id, entry in interrupted_tools.items():
        call = getattr(entry, "tool_call", None)
        tool_call_id = str(getattr(call, "id", "") or "") if call is not None else ""
        if not tool_call_id or tool_call_id != str(outer_id or ""):
            continue
        pending_tool_ids.append(tool_call_id)

    ai_message = getattr(state, "ai_message", None)
    state_calls = list(getattr(ai_message, "tool_calls", None) or [])
    state_signature = [
        (
            str(getattr(call, "id", "") or ""),
            str(getattr(call, "name", "") or ""),
        )
        for call in state_calls
    ]
    all_tool_ids = {tool_call_id for tool_call_id, _name in state_signature}
    if (
        not state_signature
        or len(all_tool_ids) != len(state_signature)
        or not set(pending_tool_ids).issubset(all_tool_ids)
    ):
        return _UNPROVEN

    react_agent = getattr(instance, "react_agent", None)
    context_engine = getattr(react_agent, "context_engine", None)
    context = (
        context_engine.get_context(session_id=target_sid)
        if context_engine is not None
        else None
    )
    messages = list(context.get_messages() or []) if context is not None else []
    assistant_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if getattr(messages[index], "tool_calls", None)
        ),
        -1,
    )
    if assistant_index < 0:
        return _UNPROVEN
    context_signature: list[tuple[str, str]] = []
    for call in list(
        getattr(messages[assistant_index], "tool_calls", None) or []
    ):
        context_signature.append(
            (
                str(getattr(call, "id", "") or ""),
                str(getattr(call, "name", "") or ""),
            )
        )
    if context_signature != state_signature:
        return _UNPROVEN
    pending_id_set = set(pending_tool_ids)
    tail_id_set: set[str] = set()
    for message in messages[assistant_index + 1:]:
        if getattr(message, "role", None) != "tool":
            continue
        tool_call_id = str(getattr(message, "tool_call_id", "") or "")
        if tool_call_id in all_tool_ids:
            tail_id_set.add(tool_call_id)
    active_ids = [tid for tid in pending_tool_ids if tid not in tail_id_set]
    stale_ids = [tid for tid in pending_tool_ids if tid in tail_id_set]
    return _PendingInterruptSplit(loop_session, state, context, context_engine, active_ids, stale_ids)


def has_live_pending_interrupt(instance: Any, target_sid: str, loop_session_id: str | None) -> bool:
    """Return whether *target_sid* holds an interrupt a user still has to answer.

    Read-only. An interrupt that does not match the context counts as not
    live: no answer can resume it, and holding a task behind it never ends.
    A probe failure counts as live.
    """
    try:
        split = _split_pending_interrupt(instance, target_sid, loop_session_id)
    except Exception:
        logger.debug("[PermissionContinuation] pending interrupt probe failed session=%s",
                     target_sid, exc_info=True)
        return True
    if split is None:
        return False
    return split is not _UNPROVEN and bool(split.active_ids)


async def discard_pending_permission_interrupt_for_target(
    instance: Any,
    target_sid: str,
    loop_session_id: str | None,
    *,
    cancel_reason: str = "host_fresh_user_input_drops_pending_permission",
    only_stale: bool = False,
    drop_unproven: bool = False,
) -> bool:
    """Clear any pending Core permission interrupt for *target_sid*.

    With ``only_stale`` it clears nothing while a tool call still waits for an
    answer, and clears only leftover state whose tool results all exist.
    With ``drop_unproven`` it also clears state that does not match the
    context; no context messages are added for it.

    This is the manual-mode / Smart-queue-empty counterpart of
    ``discard_permission_continuation``. It does not require frozen_keys:
    whatever pending entries are in ``INTERRUPTION_KEY.interrupted_tools``
    are cleared, with cancellation ToolMessages appended to balance context.

    Mirrors the validation pattern of ``discard_permission_continuation``:
    cross-checks ``ai_message.tool_calls`` against the last assistant message
    in the context engine and verifies completed tool results before clearing.

    Returns True if state was cleared, False if nothing pending, mismatch,
    or session does not match.
    """
    try:
        split = _split_pending_interrupt(instance, target_sid, loop_session_id)
        if split is _UNPROVEN and drop_unproven:
            loop_session = getattr(instance, "_loop_session", None)
            if loop_session is None:
                return False
            loop_session.update_state({INTERRUPTION_KEY: None})
            context_engine = getattr(getattr(instance, "react_agent", None), "context_engine", None)
            if context_engine is not None:
                await context_engine.save_contexts(loop_session)
            logger.warning(
                "[PermissionContinuation] dropped unmatched pending interrupt session=%s reason=%s",
                target_sid, cancel_reason,
            )
            return True
        if split is None or split is _UNPROVEN:
            return False
        loop_session, state, context, context_engine, active_ids, stale_ids = split
        if only_stale and active_ids:
            return False

        added = 0
        try:
            names = {call.id: call.name for call in state.ai_message.tool_calls}
            client_tool_ids = {call.tool_call_id for call in _paused_client_tool_calls(state)}
            for tool_call_id in active_ids:
                await context.add_messages(
                    _superseded_message(tool_call_id, names[tool_call_id], client_tool_ids)
                )
                added += 1
            loop_session.update_state({INTERRUPTION_KEY: None})
            await context_engine.save_contexts(loop_session)
        except Exception:
            loop_session.update_state({INTERRUPTION_KEY: state})
            if added:
                context.pop_messages(added, with_history=True)
            raise
    except Exception:
        logger.debug(
            "[PermissionContinuation] pending permission interrupt discard failed "
            "session=%s reason=%s",
            target_sid,
            cancel_reason,
            exc_info=True,
        )
        return False

    logger.info(
        "[PermissionContinuation] discarded pending permission interrupt "
        "session=%s active=%d stale=%d reason=%s",
        target_sid,
        len(active_ids),
        len(stale_ids),
        cancel_reason,
    )
    return True


def unanswered_permission_batch(instance: Any, target_sid: str, loop_session_id: str | None) -> Any | None:
    """Return the canonical, wholly unanswered permission batch, if present."""
    session = getattr(instance, "_loop_session", None)
    if session is None or loop_session_id != target_sid:
        return None
    state = session.get_state(INTERRUPTION_KEY)
    interrupted = getattr(state, "interrupted_tools", None)
    message = getattr(state, "ai_message", None)
    calls = list(getattr(message, "tool_calls", None) or [])
    if not isinstance(interrupted, Mapping) or not interrupted or len(interrupted) != len(calls):
        return None
    requests = [request for entry in interrupted.values()
                for request in (getattr(entry, "interrupt_requests", None) or {}).values()]
    smart = any(isinstance(getattr(request, "metadata", None), Mapping)
                and "tool_invocation_key" in request.metadata for request in requests)
    if not smart:
        owner = (f"{JiuwenSwarmPermissionInterruptRail.__module__}."
                 f"{JiuwenSwarmPermissionInterruptRail.__qualname__}")
        if not requests or any(
            getattr(request, "interrupt_kind", None) != InterruptKind.PERMISSION
            or getattr(request, "interrupt_owner", None) != owner for request in requests
        ):
            return None
        if any(getattr(entry, "is_sub_agent", False) or getattr(entry, "carried_answers", None)
               for entry in interrupted.values()):
            return None
        # Default approvals have no Smart key. Only Core's exact, unanswered
        # root calls may be cancelled; never infer identity from display text.
        for outer_id, entry in interrupted.items():
            call = getattr(entry, "tool_call", None)
            pending = getattr(entry, "interrupt_requests", None)
            if (not isinstance(pending, Mapping) or len(pending) != 1
                    or getattr(call, "id", None) != outer_id):
                raise RootPermissionQueueError("permission_continuation_state_invalid")
            inner_id = next(iter(pending))
            if not isinstance(inner_id, str) or not inner_id:
                raise RootPermissionQueueError("permission_continuation_identity_mismatch")
            if inner_id != outer_id and call.name != "tool_call":
                raise RootPermissionQueueError("permission_continuation_identity_mismatch")
    expected = {getattr(call, "id", None): call for call in calls}
    if None in expected or "" in expected:
        raise RootPermissionQueueError("permission_continuation_identity_mismatch")
    if len(expected) != len(calls) or set(expected) != set(interrupted):
        raise RootPermissionQueueError("permission_continuation_identity_mismatch")
    entries = (_permission_entries(state) if smart else
               ((entry.tool_call, None) for entry in interrupted.values()))
    for call, key in entries:
        original = expected[call.id]
        if key is not None and (key.root_session_id != target_sid or key.execution_session_id != target_sid):
            raise RootPermissionQueueError("permission_continuation_identity_mismatch")
        # The stream rail strips display-only metadata before nested dispatch.
        _, arguments = extract_call_goal(call.arguments)
        _, original_arguments = extract_call_goal(original.arguments)
        if call.name != original.name or arguments != original_arguments:
            raise RootPermissionQueueError("permission_continuation_identity_mismatch")
    return message


async def discard_restored_permission_batch(
    instance: Any, target_sid: str, loop_session_id: str | None,
) -> bool:
    """Balance a cold, unanswered batch before the Host admits a fresh user turn.

    The caller owns the session lock and has excluded live work. History omits
    calls paused before their tool event; only Core's exact batch can fill that
    gap. This never restores an approval or guesses a completed tool result.
    """
    message = unanswered_permission_batch(instance, target_sid, loop_session_id)
    if message is None:
        raise RootPermissionQueueError("permission_continuation_state_missing")
    session = instance.loop_session
    engine = instance.react_agent.context_engine
    context = engine.get_context(session_id=target_sid)
    if context is None:
        raise RootPermissionQueueError("permission_continuation_context_missing")
    state = session.get_state(INTERRUPTION_KEY)
    old_context = copy.deepcopy(context.save_state())
    old_session_context = copy.deepcopy(session.get_state("context"))
    ids = {call.id for call in message.tool_calls}
    messages = list(context.get_messages() or [])
    if any(getattr(item, "tool_call_id", None) in ids for item in messages):
        raise RootPermissionQueueError("permission_continuation_context_mismatch")
    existing = []
    for item in messages:
        calls = getattr(item, "tool_calls", None) or []
        if any(getattr(call, "id", None) in ids for call in calls):
            existing.append(item)
    if existing and (len(existing) != 1 or existing[0].tool_calls != message.tool_calls):
        raise RootPermissionQueueError("permission_continuation_context_mismatch")
    try:
        if not existing:
            await context.add_messages(copy.deepcopy(message))
        if not await discard_pending_permission_interrupt_for_target(instance, target_sid, loop_session_id):
            raise RootPermissionQueueError("permission_continuation_discard_failed")
    except BaseException:
        context.load_state({context.context_id(): old_context})
        # Session updates merge dictionaries and treat nested None as deletion.
        session.update_state({"context": None})
        session.update_state({INTERRUPTION_KEY: state, "context": old_session_context})
        raise
    return True


def validate_manual_resume(loop_session: Any, query: InteractiveInput) -> None:
    """Retain the SDK's manual batch/subset contract during Smart activation."""
    state = loop_session.get_state(INTERRUPTION_KEY)
    interrupted = getattr(state, "interrupted_tools", None)
    if not isinstance(interrupted, Mapping) or not (query.user_inputs or query.interrupt_answers):
        raise RootPermissionQueueError("interaction_resume_state_missing")
    for entry in interrupted.values():
        if not isinstance(getattr(entry, "interrupt_requests", None), Mapping):
            raise RootPermissionQueueError("interaction_resume_state_invalid")
    # Manual SDK batches may answer a nonempty subset, by tool call id or by
    # interrupt id. Do not impose Smart's single-card or structured-ask contract.
    if match_answers(query, pending_interrupts(state)).unmatched:
        raise RootPermissionQueueError("interaction_resume_identity_mismatch")


def _paused_client_tool_calls(state: Any) -> list[PendingInterrupt]:
    """The client tool calls a Session's interrupt state is paused on."""
    return [interrupt for interrupt in pending_interrupts(state) if interrupt.kind == InterruptKind.CLIENT_TOOL]


def validate_client_tool_answers(loop_session: Any, query: Any) -> None:
    """Refuse a client tool resume unless it answers a paused client tool call, by interrupt id.

    A result addressed to a call paused on an approval or a question (by
    interrupt id or by tool call id) would be read as that answer, an approval
    the user never gave, so it is refused. A result for a call no longer
    paused answers nothing and is ignored; a resume of such results only is
    refused, so the client drops them.
    """
    state = loop_session.get_state(INTERRUPTION_KEY) if loop_session is not None else None
    pending = pending_interrupts(state)
    clients = {interrupt.interrupt_id for interrupt in pending if interrupt.kind == InterruptKind.CLIENT_TOOL}
    others = {interrupt.interrupt_id for interrupt in pending} - clients
    answered = _interrupt_ids_answered(query)
    if answered is None or not clients & answered or others & answered:
        raise ClientToolAnswerMismatch("no paused client tool call waits on these results")


def _interrupt_ids_answered(query: Any) -> set[str] | None:
    """The interrupt ids a resume answers, or None unless it answers by interrupt id alone."""
    if not isinstance(query, InteractiveInput) or query.raw_inputs is not None or query.user_inputs:
        return None
    return set(query.interrupt_answers)


def prepare_nonpermission_resume(
    loop_session: Any,
    inputs: Mapping[str, Any],
    query: InteractiveInput,
    *,
    root_session_id: str,
) -> dict[str, Any]:
    if loop_session is None or getattr(query, "raw_inputs", None) is not None:
        raise RootPermissionQueueError("interaction_resume_state_missing")
    state = loop_session.get_state(INTERRUPTION_KEY)
    interrupted = getattr(state, "interrupted_tools", None)
    if not isinstance(interrupted, Mapping):
        raise RootPermissionQueueError("interaction_resume_state_missing")
    expected: list[tuple[str, str, dict[str, Any], str]] = []
    for entry in interrupted.values():
        requests = getattr(entry, "interrupt_requests", None)
        if not isinstance(requests, Mapping):
            raise RootPermissionQueueError("interaction_resume_state_invalid")
        for inner_id, interrupt_request in requests.items():
            if _is_client_tool_request(interrupt_request):
                continue  # answered by its client; the call stays paused
            metadata = getattr(interrupt_request, "metadata", None)
            if not isinstance(metadata, Mapping) or "tool_invocation_key" in metadata:
                raise RootPermissionQueueError(
                    "interaction_resume_permission_state_mismatch"
                )
            tool_call = getattr(entry, "tool_call", None)
            expected.append(
                (
                    str(inner_id or "").strip(),
                    str(getattr(tool_call, "name", "") or "").strip(),
                    dict(metadata),
                    interrupt_request.interrupt_id,
                )
            )
    matched = match_answers(query, pending_interrupts(state))
    if len(expected) != 1 or list(matched.answers) != [expected[0][3]] or matched.unmatched:
        raise RootPermissionQueueError("interaction_resume_identity_mismatch")
    tool_call_id, tool_name, metadata, interrupt_id = expected[0]
    marker = RootNonPermissionResume(
        root_session_id=root_session_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
    )
    prepared_ask = None
    if tool_name == ASK_USER_TOOL_NAME:
        continuation = ask_user_continuation(
            metadata,
            expected_tool_call_id=tool_call_id,
        )
        if continuation is None:
            raise RootPermissionQueueError("interaction_resume_ask_state_invalid")
        prepared_ask = prepare_ask_user_resume(
            continuation=continuation,
            answer=matched.answers[interrupt_id],
        )
        if prepared_ask is None:
            raise RootPermissionQueueError("interaction_resume_ask_answer_invalid")
    prepared = put_root_nonpermission_resume_in_inputs(inputs, marker)
    return put_ask_user_resume_in_inputs(prepared, prepared_ask)
