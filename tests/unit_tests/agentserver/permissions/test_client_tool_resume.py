# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A client tool resume answers paused client tool calls, and only those, by interrupt id.

In every permission mode a result sent for a call paused on an approval or a
question is refused, so a client cannot answer a prompt for the user. A result
for a call no longer paused answers nothing: the others of the resume still
go through, and a resume of such results only is refused so the client drops it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.interrupt import ClientToolRequest, InterruptRequest
from openjiuwen.core.single_agent.interrupt.state import ToolInterruptEntry, ToolInterruptionState

from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue import RootPermissionQueueError
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
    ROOT_NON_PERMISSION_RESUME_DTO_KEY,
    RootNonPermissionResume,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.client_tools import ClientToolAnswerMismatch
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.agent_adapter.permission_continuation import (
    prepare_nonpermission_resume,
    validate_client_tool_answers,
)

_ROOT = "root-session"


def _paused(*requests: tuple[str, str, InterruptRequest]):
    state = ToolInterruptionState(ai_message=AssistantMessage(content=""), iteration=0, interrupted_tools={
        call_id: ToolInterruptEntry(tool_call=ToolCall(id=call_id, type="function", name=name, arguments="{}"),
                                    interrupt_requests={call_id: request})
        for call_id, name, request in requests
    })
    return SimpleNamespace(get_state=lambda _key: state)


def _client(call_id: str):
    return call_id, "read_page", ClientToolRequest(interrupt_id=f"intr-{call_id}", tool_name="read_page")


def _question(call_id: str):
    return call_id, "exit_plan_mode", InterruptRequest(interrupt_id=f"intr-{call_id}", metadata={})


def _by_interrupt(*call_ids: str) -> InteractiveInput:
    query = InteractiveInput()
    for call_id in call_ids:
        query.answer(f"intr-{call_id}", {"content": call_id})
    return query


def _by_call(*call_ids: str) -> InteractiveInput:
    query = InteractiveInput()
    for call_id in call_ids:
        query.update(call_id, {"content": call_id})
    return query


@pytest.mark.parametrize("query", [
    _by_interrupt("read-1", "read-2"), _by_interrupt("read-2"), _by_interrupt("read-2", "read-9"),
])
def test_one_resume_answers_any_of_the_paused_client_tool_calls(query) -> None:
    validate_client_tool_answers(_paused(_client("read-1"), _client("read-2"), _question("ask-1")), query)


@pytest.mark.parametrize("query", [
    _by_interrupt("ask-1"), _by_call("ask-1"), _by_interrupt("read-1", "ask-1"), _by_interrupt("read-9"),
    _by_call("read-1"), InteractiveInput(), "a plain message",
])
def test_a_resume_answering_no_paused_client_tool_call_or_a_question_is_refused(query) -> None:
    with pytest.raises(ClientToolAnswerMismatch):
        validate_client_tool_answers(_paused(_client("read-1"), _question("ask-1")), query)


def test_the_users_answer_to_a_question_next_to_client_tool_calls_is_admitted() -> None:
    session = _paused(_client("read-1"), _question("ask-1"))

    prepared = prepare_nonpermission_resume(session, {}, _by_interrupt("ask-1"), root_session_id=_ROOT)

    marker = prepared["run"]["context"]["extra"][ROOT_NON_PERMISSION_RESUME_DTO_KEY]
    assert marker == RootNonPermissionResume(_ROOT, "ask-1", "exit_plan_mode")
    with pytest.raises(RootPermissionQueueError, match="interaction_resume_identity_mismatch"):
        prepare_nonpermission_resume(session, {}, _by_interrupt("read-1"), root_session_id=_ROOT)


@pytest.mark.asyncio
async def test_the_adapter_refuses_a_client_resume_before_any_permission_mode_reads_it() -> None:
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = SimpleNamespace(loop_session=_paused(("bash-1", "bash", InterruptRequest())))
    request = AgentRequest(request_id="r", channel_id="web", session_id=_ROOT, req_method=ReqMethod.CHAT_SEND,
                           params={"source": "client_tool_interrupt", "request_id": "bash-1"})

    with pytest.raises(ClientToolAnswerMismatch):
        await adapter._prepare_root_input_dispatch(request, {"query": _by_call("bash-1")})
