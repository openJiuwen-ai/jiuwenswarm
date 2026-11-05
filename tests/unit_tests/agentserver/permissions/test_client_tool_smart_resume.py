# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Smart Approval resumes client tool calls through the real dispatch and Core resume.

The task pane answers the client tool calls of a pause in one resume. Under
Smart Approval that resume goes through ``RootPermissionDispatch.prepare_resume``
and the root permission queue rail without touching a card: a client's result
is a call's output, never an approval. A pause that also holds an approval
card stays resumable in either order.
"""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall
from openjiuwen.core.foundation.tool import LocalFunction, ToolCard
from openjiuwen.core.runner.callback import AbortError
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.core.single_agent.agent_callback_manager import AgentCallbackManager
from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
from openjiuwen.core.single_agent.interrupt.handler import ResumeContext, ToolInterruptHandler
from openjiuwen.core.single_agent.interrupt.response import InterruptRequest
from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentCallbackEvent
from openjiuwen.harness.deep_agent import DeepAgent

from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue import (
    RootPermissionQueue,
    RootPermissionQueueError,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
    RootPermissionCompletionRail,
    RootPermissionQueueRail,
    bind_root_permission_request,
    mark_permission_interrupt_request,
    reset_root_permission_request,
    root_permission_resume_from_context,
)
from jiuwenswarm.runtime.client_tools import ClientToolAnswerMismatch
from jiuwenswarm.server.runtime.agent_adapter.permission_continuation import validate_client_tool_answers
from jiuwenswarm.server.runtime.agent_adapter.permission_dispatch import RootPermissionDispatch

_ROOT = "session-a"


class _Harness:
    """Core tool dispatch with the Smart queue rails, an approval step for ``bash`` and a client tool."""

    def __init__(self) -> None:
        self.data: dict = {}
        self.executed: list[str] = []
        # The callback framework logs and skips a failing rail; keep its failures to assert on.
        self.queue_errors: list[Exception] = []
        self.session = SimpleNamespace(
            get_session_id=lambda: _ROOT, get_state=self.data.get, update_state=self.data.update,
        )
        self.queue = RootPermissionQueue()
        self.dispatch = RootPermissionDispatch(self.queue)
        self.queue_rail = RootPermissionQueueRail(self.queue)
        self.completion = RootPermissionCompletionRail(self.queue)
        self.manager = AbilityManager(owner_id="client-smart")
        self.callbacks = AgentCallbackManager("client-smart")
        self.agent = SimpleNamespace(card=SimpleNamespace(id="client-smart"), ability_manager=self.manager,
                                     agent_callback_manager=self.callbacks)
        self.ctx = AgentCallbackContext(agent=self.agent, session=self.session, extra={})
        self.handler = ToolInterruptHandler(self.agent)
        self.handler.commit_interrupt = AsyncMock(side_effect=lambda state, *_: self._snapshot(state))

    async def start(self) -> None:
        for name, execution in (("read_page", "client"), ("bash", "server")):
            tool = LocalFunction(
                card=ToolCard(id=f"tool-{name}", name=name, description=name, execution=execution,
                              input_params={"type": "object", "properties": {}}),
                func=lambda _name=name, **kwargs: self.executed.append(_name) or "ran",
            )
            self.manager.add_ability(tool.card, tool)
        for event in (AgentCallbackEvent.BEFORE_TOOL_CALL, AgentCallbackEvent.AFTER_TOOL_CALL):
            await self.callbacks.register_callback(event, self._callback)

    async def stop(self) -> None:
        await self.callbacks.clear()

    async def _callback(self, ctx) -> None:
        if ctx.event == AgentCallbackEvent.AFTER_TOOL_CALL:
            await self.queue_rail.after_tool_call(ctx)
            return
        try:
            await self.queue_rail.before_tool_call(ctx)
        except RootPermissionQueueError as exc:
            self.queue_errors.append(exc)
            raise
        if ctx.inputs.tool_name == "bash" and root_permission_resume_from_context(ctx) is None:
            request = InterruptRequest(message="approve bash", metadata={})
            mark_permission_interrupt_request(ctx, request)
            raise AbortError(reason="approve bash",
                             cause=ToolInterruptException(request=request, tool_call=ctx.inputs.tool_call))
        await self.completion.before_tool_call(ctx)

    @staticmethod
    def _snapshot(state) -> dict:
        entries = [{"id": inner, "value": request} for entry in state.interrupted_tools.values()
                   for inner, request in entry.interrupt_requests.items()]
        return {"result_type": "interrupt", "interrupt_ids": [e["id"] for e in entries], "state": entries}

    async def _execute(self, context, tool_calls, session, _context):
        return await self.manager.execute(context, tool_calls, session, parallel_tool_calls=False)

    async def first_round(self, calls: list[ToolCall]) -> dict:
        results = await self._execute(self.ctx, calls, self.session, None)
        assert all(isinstance(result, ToolInterruptException) for result, _ in results)
        self.state, _ = self.handler.build_interrupt_state(results, calls, AssistantMessage(tool_calls=calls), 0)
        self.data[INTERRUPTION_KEY] = self.state
        return self._paused(self._snapshot(self.state))

    def _paused(self, snapshot: dict | None) -> dict:
        """Reconcile the pause as the stream rail does; ``{call id: card or None}``."""
        if snapshot is None:
            return {}
        reconciled = self.queue.reconcile(snapshot, root_session_id=_ROOT)
        cards = {card.key.tool_call_id: card for card in (reconciled.cards if reconciled else ())}
        return {call_id: cards.get(call_id) for call_id in snapshot["interrupt_ids"]}

    def interrupt_id(self, call_id: str) -> str:
        for entry in self.state.interrupted_tools.values():
            if call_id in entry.interrupt_requests:
                return entry.interrupt_requests[call_id].interrupt_id
        raise KeyError(call_id)

    def results(self, *call_ids: str) -> InteractiveInput:
        query = InteractiveInput()
        for call_id in call_ids:
            query.answer(self.interrupt_id(call_id), f"text {call_id}")
        return query

    async def resume(self, query: InteractiveInput, *, client: bool) -> dict:
        if client:
            validate_client_tool_answers(self.session, query)
        prepared = self.dispatch.prepare_resume(
            {"query": query}, root_session_id=_ROOT, loop_session=self.session, client_tool_resume=client,
        )
        self.ctx.extra["run_context"] = DeepAgent._normalize_inputs(None, deepcopy(prepared)).run_context
        resumed = await self.handler.handle_resume(ResumeContext(
            state=self.state, user_input=prepared["query"], ctx=self.ctx, context=SimpleNamespace(),
            session=self.session, execute_tool_call=self._execute,
        ))
        if resumed is not None:
            self.state = self.data[INTERRUPTION_KEY]
        return self._paused(resumed)


def _page(call_id: str) -> ToolCall:
    return ToolCall(id=call_id, type="function", name="read_page", arguments=json.dumps({"page": call_id}))


def _bash() -> ToolCall:
    return ToolCall(id="bash-1", type="function", name="bash", arguments="{}")


def _approval(card) -> InteractiveInput:
    query = InteractiveInput()
    query.update(card.key.tool_call_id, {"approved": True, "auto_confirm": False, "feedback": ""})
    return query


@pytest.fixture
async def harness():
    harness = _Harness()
    await harness.start()
    token = bind_root_permission_request(root_session_id=_ROOT, request_id="turn", enabled=True,
                                         queue=harness.queue)
    try:
        yield harness
    finally:
        reset_root_permission_request(token)
        await harness.stop()
    assert harness.queue_errors == []


@pytest.mark.asyncio
async def test_one_resume_answers_every_parallel_client_tool_call(harness) -> None:
    assert await harness.first_round([_page("read-1"), _page("read-2"), _page("read-3")]) == {
        "read-1": None, "read-2": None, "read-3": None}

    assert await harness.resume(harness.results("read-1", "read-2", "read-3"), client=True) == {}
    assert harness.executed == []  # client tools never run in the backend
    assert not harness.queue.has_live(root_session_id=_ROOT)


@pytest.mark.asyncio
async def test_a_partial_client_resume_keeps_the_rest_paused(harness) -> None:
    await harness.first_round([_page("read-1"), _page("read-2")])

    assert await harness.resume(harness.results("read-2"), client=True) == {"read-1": None}
    assert await harness.resume(harness.results("read-1"), client=True) == {}


@pytest.mark.asyncio
async def test_client_results_first_leave_the_approval_card_asking_again(harness) -> None:
    paused = await harness.first_round([_page("read-1"), _bash(), _page("read-2")])
    card = paused["bash-1"]
    assert card is not None and card.state == "pending"

    again = await harness.resume(harness.results("read-1", "read-2"), client=True)
    assert set(again) == {"bash-1"} and again["bash-1"].key == card.key
    assert harness.executed == []

    assert await harness.resume(_approval(card), client=False) == {}
    assert harness.executed == ["bash"]


@pytest.mark.asyncio
async def test_the_approval_first_leaves_the_client_tool_call_waiting(harness) -> None:
    paused = await harness.first_round([_page("read-1"), _bash()])

    assert await harness.resume(_approval(paused["bash-1"]), client=False) == {"read-1": None}
    assert harness.executed == ["bash"]

    assert await harness.resume(harness.results("read-1"), client=True) == {}
    assert not harness.queue.has_live(root_session_id=_ROOT)


@pytest.mark.asyncio
async def test_client_results_never_answer_the_approval_card(harness) -> None:
    paused = await harness.first_round([_page("read-1"), _bash()])
    by_call = InteractiveInput()
    by_call.update("bash-1", {"text": "approve"})
    client_by_call = InteractiveInput()
    client_by_call.update("read-1", "text read-1")
    stale = InteractiveInput()
    stale.answer("intr_not_pending", "text")

    by_card = InteractiveInput()
    by_card.answer(harness.interrupt_id("bash-1"), {"text": "approve"})
    by_card.answer(harness.interrupt_id("read-1"), "text read-1")

    for query in (by_call, client_by_call, stale, by_card, InteractiveInput()):
        with pytest.raises(ClientToolAnswerMismatch):
            validate_client_tool_answers(harness.session, query)
    assert harness.queue.get(paused["bash-1"].key).state == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("discard", ["continuation", "pending"])
async def test_new_user_input_supersedes_the_cards_and_client_tool_calls_of_a_pause(harness, discard) -> None:
    from jiuwenswarm.server.runtime.agent_adapter.permission_continuation import (
        discard_pending_permission_interrupt_for_target,
        discard_permission_continuation,
    )

    calls = [_page("read-1"), _bash()]
    paused = await harness.first_round(calls)
    added: list = []

    class _Context:
        def get_messages(self):
            return [AssistantMessage(tool_calls=calls), *added]

        async def add_messages(self, message):
            added.append(message)

    engine = SimpleNamespace(get_context=lambda session_id: _Context(), save_contexts=AsyncMock())
    instance = SimpleNamespace(_loop_session=harness.session, react_agent=SimpleNamespace(context_engine=engine))

    if discard == "continuation":
        assert await discard_permission_continuation(instance, _ROOT, _ROOT, (paused["bash-1"].key,))
    else:
        assert await discard_pending_permission_interrupt_for_target(instance, _ROOT, _ROOT)
    results = {message.tool_call_id: message.content for message in added}
    assert sorted(results) == ["bash-1", "read-1"]
    assert harness.data[INTERRUPTION_KEY] is None
    # The client may have run the call before the new input arrived; the model is told so.
    assert "Superseded by new user input" in results["bash-1"] and "client" not in results["bash-1"]
    assert "read_page" in results["read-1"] and "may or may not have taken effect" in results["read-1"]
