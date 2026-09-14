# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""OpenJiuwen permission-continuation operations, without a Host adapter owner."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY

from jiuwenswarm.agents.harness.common.rails.permissions.root_ask_user import (
    ASK_USER_TOOL_NAME, ask_user_continuation, prepare_ask_user_resume,
    put_ask_user_resume_in_inputs,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue import RootPermissionQueueError
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
    RootNonPermissionResume, put_root_nonpermission_resume_in_inputs,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_invocation_key import ToolInvocationKeyV1

logger = logging.getLogger(__name__)


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
        for entry in interrupted_tools.values():
            tool_call = getattr(entry, "tool_call", None)
            tool_call_id = str(getattr(tool_call, "id", "") or "").strip()
            requests = getattr(entry, "interrupt_requests", None)
            if (
                not tool_call_id
                or not isinstance(requests, Mapping)
                or len(requests) != 1
            ):
                return False
            interrupt_request = next(iter(requests.values()))
            metadata = getattr(interrupt_request, "metadata", None)
            wire_key = (
                metadata.get("tool_invocation_key")
                if isinstance(metadata, Mapping)
                else None
            )
            key = ToolInvocationKeyV1.from_wire(wire_key)
            if (
                key.invocation_id in pending
                or key.root_session_id != target_sid
                or key.tool_call_id != tool_call_id
            ):
                return False
            pending[key.invocation_id] = key
            pending_tool_ids.append(tool_call_id)
        if pending != frozen:
            return False

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
            for tool_call_id in pending_tool_ids:
                await context.add_messages(
                    ToolMessage(
                        tool_call_id=tool_call_id,
                        content="[INTERRUPTED - Superseded by new user input]",
                    )
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


def validate_manual_resume(loop_session: Any, query: InteractiveInput) -> None:
    """Retain the SDK's manual batch/subset contract during Smart activation."""
    state = loop_session.get_state(INTERRUPTION_KEY)
    interrupted = getattr(state, "interrupted_tools", None)
    if not isinstance(interrupted, Mapping) or not query.user_inputs:
        raise RootPermissionQueueError("interaction_resume_state_missing")
    pending_ids = set()
    for entry in interrupted.values():
        requests = getattr(entry, "interrupt_requests", None)
        if not isinstance(requests, Mapping):
            raise RootPermissionQueueError("interaction_resume_state_invalid")
        pending_ids.update(requests)
    # Manual SDK batches may answer a nonempty subset. Do not
    # impose Smart's single-card or structured-ask contract.
    if not set(query.user_inputs).issubset(pending_ids):
        raise RootPermissionQueueError("interaction_resume_identity_mismatch")


def prepare_nonpermission_resume(
    loop_session: Any,
    inputs: Mapping[str, Any],
    query: InteractiveInput,
    *,
    root_session_id: str,
) -> dict[str, Any]:
    if loop_session is None or getattr(query, "raw_inputs", None) is not None:
        raise RootPermissionQueueError("interaction_resume_state_missing")
    submitted = getattr(query, "user_inputs", None)
    state = loop_session.get_state(INTERRUPTION_KEY)
    interrupted = getattr(state, "interrupted_tools", None)
    if not isinstance(submitted, Mapping) or not isinstance(interrupted, Mapping):
        raise RootPermissionQueueError("interaction_resume_state_missing")
    expected: list[tuple[str, str, dict[str, Any]]] = []
    for entry in interrupted.values():
        requests = getattr(entry, "interrupt_requests", None)
        if not isinstance(requests, Mapping):
            raise RootPermissionQueueError("interaction_resume_state_invalid")
        for inner_id, interrupt_request in requests.items():
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
                )
            )
    if len(expected) != 1 or set(submitted) != {expected[0][0]}:
        raise RootPermissionQueueError("interaction_resume_identity_mismatch")
    tool_call_id, tool_name, metadata = expected[0]
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
            user_input=query,
        )
        if prepared_ask is None:
            raise RootPermissionQueueError("interaction_resume_ask_answer_invalid")
    prepared = put_root_nonpermission_resume_in_inputs(inputs, marker)
    return put_ask_user_resume_in_inputs(prepared, prepared_ask)
