# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host-side preparation of the context used by one :class:`UserTurn`."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from openjiuwen.core.events import Event, SessionContext

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    is_interrupt_resume_payload,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_context import (
    HOST_USER_ORIGIN_EXTERNAL,
)
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions_v2.event_types.prompt_context import (
    PROMPT_CONTEXT_EVENT,
    PromptContextInput,
    PromptContextResult,
    freeze_prompt_context_input,
    valid_prompt_context_output,
)
from jiuwenswarm.server.runtime.a2ui.integration import build_user_prompt_if_a2ui_event
from jiuwenswarm.server.runtime.a2ui.protocol import is_a2ui_client_event
from jiuwenswarm.server.runtime.agent_adapter.user_turn import UserTurn

if TYPE_CHECKING:
    from jiuwenswarm.common.schema.agent import AgentRequest

logger = logging.getLogger(__name__)


async def prepare_prompt_context(
    turn: UserTurn,
    request: AgentRequest,
    *,
    cwd: str | None,
) -> UserTurn:
    """Run ``prompt.context`` once and bind its result to ``turn``.

    The event is only for eligible external user-message payloads. A missing or broken
    extension assembly keeps the original synchronous rendering path; a failed
    event after a bus was found keeps a detached copy of the original blocks.
    """
    if not is_prompt_context_applicable(turn, request):
        return turn

    original_blocks = turn.build_prompt_contexts()
    event_input = freeze_prompt_context_input(PromptContextInput(blocks=list(original_blocks)))
    bus = _get_extension_bus()
    if bus is None:
        return turn

    session = SessionContext(
        session_id=request.session_id or "",
        cwd=cwd,
        metadata=_session_metadata(turn, request, event_input),
    )
    try:
        result = await bus.trigger(Event(PROMPT_CONTEXT_EVENT, event_input), session)
    except Exception as exc:  # noqa: BLE001 - prompt context is fail-open
        _log_dispatch_failure(request, event_input, exc)
        return turn.with_prompt_contexts(original_blocks, None)

    if not valid_prompt_context_output(result):
        _log_invalid_output(request, event_input, result)
        return turn.with_prompt_contexts(original_blocks, None)

    return turn.with_prompt_contexts(result.blocks, result.instructionFiles)


def _get_extension_bus():
    """Return the process-owned bus without making extension assembly mandatory."""
    try:
        from jiuwenswarm.extensions_v2.assembly import get_extension_bus
        return get_extension_bus()
    except Exception:  # noqa: BLE001 - a disabled assembly is a valid state
        logger.debug("[ExtensionsV2] prompt.context assembly unavailable", exc_info=True)
        return None


def is_prompt_context_applicable(turn: UserTurn, request: AgentRequest) -> bool:
    """Central gate for every request that may dispatch ``prompt.context``."""
    if not isinstance(request.session_id, str) or not request.session_id.strip():
        return False
    if request.req_method is not ReqMethod.CHAT_SEND:
        return False
    if turn.origin_kind != HOST_USER_ORIGIN_EXTERNAL:
        return False

    params = request.params if isinstance(request.params, dict) else {}
    mode = str(params.get("mode") or "").strip().lower()
    if mode.startswith("auto_harness"):
        return False
    if params.get("is_supplement") is True:
        return False
    if is_interrupt_resume_payload(params):
        return False
    metadata = request.metadata if isinstance(request.metadata, dict) else {}
    if metadata.get("skip_a2ui") is True or params.get("skip_a2ui") is True:
        return False

    if not isinstance(turn.text, (str, dict)):
        return False
    if not isinstance(turn.text, dict):
        return True
    if is_a2ui_client_event(turn.text):
        return False
    return (
        build_user_prompt_if_a2ui_event(
            turn.text,
            channel=turn.channel,
            language=turn.language,
        )
        is None
    )


def _session_metadata(
    turn: UserTurn,
    request: AgentRequest,
    event_input: PromptContextInput,
) -> dict[str, object]:
    """Build non-content facts for the trusted event session context."""
    params = request.params if isinstance(request.params, dict) else {}
    return {
        "request_id": request.request_id,
        "channel_id": request.channel_id,
        "prompt_channel": turn.prompt_channel(),
        "mode": str(params.get("mode") or ""),
        "context_names": [block.name for block in event_input.blocks],
        "context_count": len(event_input.blocks),
        "instruction_file_count": len(event_input.instructionFiles or []),
        "is_stream": request.is_stream,
        "origin_kind": turn.origin_kind,
    }


def _log_dispatch_failure(
    request: AgentRequest,
    event_input: PromptContextInput,
    error: Exception,
) -> None:
    logger.warning(
        "[ExtensionsV2] prompt.context failed open: event_type=%s request_id=%s "
        "context_names=%s instruction_file_count=%s error=%s",
        PROMPT_CONTEXT_EVENT,
        request.request_id,
        [block.name for block in event_input.blocks],
        len(event_input.instructionFiles or []),
        type(error).__name__,
    )


def _log_invalid_output(
    request: AgentRequest,
    event_input: PromptContextInput,
    result: object,
) -> None:
    logger.warning(
        "[ExtensionsV2] prompt.context returned invalid output: event_type=%s "
        "request_id=%s context_names=%s instruction_file_count=%s result_type=%s",
        PROMPT_CONTEXT_EVENT,
        request.request_id,
        [block.name for block in event_input.blocks],
        len(event_input.instructionFiles or []),
        type(result).__name__,
    )
