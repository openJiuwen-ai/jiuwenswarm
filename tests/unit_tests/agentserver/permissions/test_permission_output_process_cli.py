# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""CLI completion and cancellation through the real typed Runtime/Host/Core path."""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agent_gateway.extensions.registry import ExtensionRegistry
from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY

from jiuwenswarm.agents.harness import agent_observability
from jiuwenswarm.agents.harness.common.recommendation import prompt_suggestion
from jiuwenswarm.channels.process_cli import machine
from jiuwenswarm.channels.process_cli.client import InProcessRuntimeClient
from jiuwenswarm.channels.process_cli.duplex_control import DuplexController
from jiuwenswarm.channels.process_cli.machine_io import OneShotWriter
from jiuwenswarm.channels.process_cli.protocol import OneShotRunInput, WorkspaceSpec
from jiuwenswarm.channels.process_cli.session_guard import SessionLease
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.server.runtime.agent_adapter.output_handoff import OutputHandoff
from tests.unit_tests.agentserver.permissions.test_permission_answer_cutover import answer_host, cold
from tests.unit_tests.process_cli.test_duplex_control import ObservedOutput, QueueReader
from tests.unit_tests.runtime.test_runtime_service import FakeAgentManager, FakePlanController


class _CliRun:
    def __init__(self, host, runtime, client):
        self.host, self.runtime, self.client = host, runtime, client
        self.reader, self.output = QueueReader(), ObservedOutput()
        self.writer = OneShotWriter(self.output, request_id="external-cli-run")
        self.control = DuplexController(self.reader, self.writer)
        self.gates = []
        self.cards = []
        self.answers = []
        self.parents = []
        self.waits = asyncio.Queue()
        self.cancels = []
        self.snapshot = None
        self.interrupted_at_cleanup = None
        self.task = None

    def start(self, *, timeout=10):
        self.task = asyncio.create_task(machine.run_machine(
            OneShotRunInput(input="Run the approval probe", mode="agent.work.normal",
                            timeout_seconds=timeout,
                            workspace=WorkspaceSpec(project_dir=str(self.host.root))),
            self.writer, client_factory=lambda: self.client, control=self.control,
        ))

    async def notice(self):
        async with asyncio.timeout(10):
            while True:
                if self.task.done():
                    raise AssertionError(f"CLI ended before its card: {self.task.result()}")
                record_task = asyncio.create_task(self.output.events.get())
                try:
                    done, _ = await asyncio.wait((record_task, self.task),
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if record_task in done:
                        record = record_task.result()
                        if record.get("event_type") == "interaction.requested":
                            return record
                finally:
                    record_task.cancel()
                    await asyncio.gather(record_task, return_exceptions=True)

    def answer(self, notice, action="allow_once"):
        interaction = notice["payload"]["interaction"]
        question = interaction["questions"][0]
        value = {"selected_options": [action]}
        if question.get("card_id"):
            value["card_id"] = question["card_id"]
        self.reader.send({
            "schema_version": "0.1", "type": "answer",
            "request_id": notice["request_id"], "session_id": notice["session_id"],
            "interaction_id": notice["payload"]["interaction_id"], "answers": [value],
        })

    async def waiting(self, *, dispatches):
        async with asyncio.timeout(10):
            waiter = asyncio.create_task(self.waits.get())
            try:
                done, _ = await asyncio.wait((waiter, self.task), return_when=asyncio.FIRST_COMPLETED)
                assert waiter in done, f"CLI ended before handoff: {self.task.result()}"
                assert waiter.result().owner is self.host.adapter._instance
            finally:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
        assert len(self.host.dispatches) == dispatches
        assert not self.task.done()
        assert not self.host.executions or dispatches > 1
        assert not self.host.adapter._interaction_output_handoff.drained.is_set()

    async def finish(self):
        async with asyncio.timeout(10):
            result = await self.task
        # Publication belongs to the entrypoint after run_machine cleanup.
        assert self.runtime._closed and self.reader.closed
        assert not self.writer.final_written
        self.writer.write_result(result)
        assert len([row for row in self.output.records if row["type"] == "result"]) == 1
        assert {row["request_id"] for row in self.output.records} == {"external-cli-run"}
        assert not self.host.adapter._permission_dispatch.lock.locked()
        assert not self.control._streams and self.control._input_task.done()
        # The actual OS session lock must have been released by machine.cleanup.
        lease = SessionLease(self.host.adapter._parent_session_id)
        lease.acquire()
        lease.release()
        return result

    async def close(self):
        for gate in self.gates:
            gate.set()
        if self.task is not None:
            if not self.task.done():
                self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if not self.runtime._closed:
            await self.client.close()


@pytest.fixture
async def cli_host(answer_host, monkeypatch):
    h = answer_host
    runs = []
    registry = ExtensionRegistry(Runner.callback_framework, {}, logging.getLogger(__name__))
    monkeypatch.setattr(ExtensionRegistry, "_instance", registry)

    async def make(*, mode="auto", two_cards=False):
        h.raw["permissions"]["mode"] = mode
        monkeypatch.setattr(agent_observability, "sync_agent_observability", lambda **_kwargs: None)
        monkeypatch.setattr(prompt_suggestion.forks, "request_started", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(prompt_suggestion.forks, "turn_ended", lambda *_args, **_kwargs: None)
        await h.adapter.create_instance({"channel_id": "process_cli", "project_dir": str(h.root)},
                                        mode="agent")
        sid = h.adapter._parent_session_id
        await h.adapter.start_interaction(sid)

        def proposal(number):
            return AssistantMessage(content="", tool_calls=[ToolCall(
                id=f"cli_call_{number}", type="function", name=h.tool.card.name,
                arguments=json.dumps({"value": f"ONCE-{number}"}),
            )])

        h.script.responses = [proposal(1)]
        if two_cards:
            h.script.responses.append(proposal(2))
        h.script.responses.append(AssistantMessage(content="CLI-COMPLETED"))
        original_send = h.adapter._instance.send_input

        async def send(request):
            h.dispatches.append(request)
            return await original_send(request)

        monkeypatch.setattr(h.adapter._instance, "send_input", send)
        monkeypatch.setattr(h.facade, "_ensure_adapter", lambda **_kwargs: h.adapter)
        h.facade._session_manager = SimpleNamespace(get_session_id=lambda value: value,
                                                   cancel_session_task=AsyncMock())
        monkeypatch.setattr(h.facade, "reconcile_session_mcp", AsyncMock())
        manager = FakeAgentManager()
        runtime = AgentRuntime(agent_manager=manager, initializer=AsyncMock(),
                               plan_controller=FakePlanController())
        client = InProcessRuntimeClient(runtime)
        run = _CliRun(h, runtime, client)
        runs.append(run)
        original_wait = OutputHandoff.wait

        async def wait_for_owner(handoff):
            run.waits.put_nowait(handoff)
            return await original_wait(handoff)

        monkeypatch.setattr(OutputHandoff, "wait", wait_for_owner)

        async def forward(stream):
            try:
                async for chunk in stream:
                    gate = None
                    if (chunk.payload or {}).get("event_type") == "chat.ask_user_question":
                        gate = asyncio.Event()
                        run.gates.append(gate)
                        run.cards.append(chunk.payload)
                    yield chunk
                    if gate is not None:
                        # Gate the actual old consumer after publishing its real card.
                        await gate.wait()
            finally:
                await stream.aclose()

        class HostEntry:
            async def process_message(self, request):
                return await h.facade.process_message(request)

            async def process_message_stream(self, request):
                run.parents.append(request)
                inputs, _, _ = h.facade.build_inputs(request)
                async for chunk in forward(h.adapter.process_message_stream_impl(request, inputs)):
                    yield chunk

            async def deliver_control_input(self, request):
                async for chunk in forward(h.facade.deliver_control_input(request)):
                    yield chunk

        manager.agent = HostEntry()
        monkeypatch.setattr(manager, "get_agent_nowait", lambda *_args, **_kwargs: manager.agent)

        async def create_session(channel_id="", session_id=None):
            assert channel_id == "process_cli" and session_id is None
            manager.session_calls.append((channel_id, session_id))
            # Match the real manager's persisted SDK ownership before answering.
            from jiuwenswarm.server.runtime.session.session_metadata import init_session_metadata
            from jiuwenswarm.runtime.session_catalog import SessionGetInput

            init_session_metadata(session_id=sid, channel_id=channel_id,
                                  mode="agent.work.normal", work_mode="work",
                                  project_dir=str(h.root))
            assert runtime.get_session(SessionGetInput(channel_id=channel_id, session_id=sid)) is not None
            return sid

        async def cleanup_session(*, channel_id, session_id):
            assert channel_id == "process_cli" and session_id == sid
            run.snapshot = runtime._session_coordinator.snapshot_session(sid)
            run.interrupted_at_cleanup = h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY)
            manager.cleanup_session_calls.append((channel_id, session_id))
            await h.adapter.stop_interaction()
            return True

        monkeypatch.setattr(manager, "create_session", create_session)
        monkeypatch.setattr(manager, "cleanup_session_runtime", cleanup_session)
        original_answer = client.stream_interaction_answer

        def typed_answer(answer):
            run.answers.append(answer)
            return original_answer(answer)

        monkeypatch.setattr(client, "stream_interaction_answer", typed_answer)
        original_cancel = client.cancel

        async def cancel(request):
            root_task = run.control._streams.get("external-cli-run")
            run.cancels.append({
                "locked": h.adapter._permission_dispatch.lock.locked(),
                "answers_done": all(task.done() for operation_id, task in run.control._streams.items()
                                    if operation_id != "external-cli-run"),
                "root_cancelled": root_task.cancelled() if root_task is not None else False,
            })
            return await original_cancel(request)

        monkeypatch.setattr(client, "cancel", cancel)
        return run

    try:
        yield make
    finally:
        for run in runs:
            await run.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize("action", ["allow_once", "reject"])
async def test_cli_real_runtime_early_answer_keeps_output_and_finishes(cli_host, mode, action):
    run = await cli_host(mode=mode)
    h = run.host
    run.start()
    notice = await run.notice()
    run.answer(notice, action)
    await run.waiting(dispatches=1)
    assert not run.reader.eof_seen and not h.executions
    run.gates[0].set()
    result = await run.finish()
    assert result.status == "completed" and result.exit_code == 0
    assert result.output == "CLI-COMPLETED"
    tool_results = [row for row in run.output.records if row.get("event_type") == "chat.tool_result"]
    assert tool_results
    if action == "allow_once":
        assert "ONCE-1" in repr(tool_results)
    assert h.executions == ([{"value": "ONCE-1"}] if action == "allow_once" else [])
    assert len(h.dispatches) == 2 and len(run.answers) == 1
    assert len(h.script.calls) == 2 and not run.interrupted_at_cleanup
    assert run.answers[0].request_id != "external-cli-run"
    assert run.answers[0].interaction_id == run.cards[0]["request_id"]
    assert run.answers[0].source == "permission_interrupt"
    assert not h.adapter._root_permission_queue.has_live(root_session_id=h.adapter._parent_session_id)
    executions = run.snapshot.executions
    parent = next(row for row in executions if row.request_id == "external-cli-run")
    controls = [row for row in executions if row.work_kind.value == "control_input"]
    assert len(controls) == 1
    control = controls[0]
    assert control.parent_execution_id == parent.execution_id
    assert parent.state.value == control.state.value == "succeeded"
    assert not parent.waiting_control_ids and not control.waiting_control_ids


@pytest.mark.asyncio
async def test_cli_real_runtime_two_cards_do_not_finish_on_suspended_tail(cli_host):
    run = await cli_host(two_cards=True)
    run.start()
    first = await run.notice()
    run.answer(first)
    await run.waiting(dispatches=1)
    run.gates[0].set()
    second = await run.notice()
    assert not run.task.done() and not run.writer.final_written
    assert first["payload"]["interaction_id"] != second["payload"]["interaction_id"]
    assert run.host.executions == [{"value": "ONCE-1"}]
    run.answer(second, "reject")
    await run.waiting(dispatches=2)
    run.gates[1].set()
    result = await run.finish()
    assert result.exit_code == 0 and result.output == "CLI-COMPLETED"
    assert run.host.executions == [{"value": "ONCE-1"}]
    assert len(run.host.dispatches) == 3 and len(run.answers) == 2
    assert not run.reader.eof_seen
    assert not run.host.adapter._instance.loop_session.get_state(INTERRUPTION_KEY)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending,card", [("timeout", 1), ("cancel", 1), ("eof", 1),
                                         ("cancel", 2), ("timeout", 2)])
async def test_cli_real_runtime_wait_is_cancellable_without_approval(cli_host, ending, card):
    run = await cli_host(two_cards=card == 2)
    run.start(timeout=2 if ending == "timeout" else 10)
    notice = await run.notice()
    if card == 2:
        run.answer(notice)
        await run.waiting(dispatches=1)
        run.gates[0].set()
        notice = await run.notice()
    if ending != "eof":
        run.answer(notice)
        await run.waiting(dispatches=card)
    if ending == "cancel":
        run.reader.send({"schema_version": "0.1", "type": "cancel",
                         "request_id": "external-cli-run"})
    elif ending == "eof":
        run.reader.send(None)
    result = await run.finish()
    expected = {"timeout": ("TIMEOUT", 124), "cancel": ("CANCELLED", 130),
                "eof": ("INPUT_CLOSED", 130)}
    assert (result.error.code, result.exit_code) == expected[ending]
    assert not result.error.details.get("cleanup_errors"), result.error
    assert run.cancels == [{"locked": False, "answers_done": True, "root_cancelled": False}]
    assert run.host.executions == ([{"value": "ONCE-1"}] if card == 2 else [])
    assert len(run.host.dispatches) == card
    assert len(run.answers) == (0 if ending == "eof" else card)
    assert not run.control._pending
    assert all(row.state.terminal for row in run.snapshot.executions)


@pytest.mark.asyncio
async def test_cli_half_close_during_answer_cancels_the_next_unanswered_card(cli_host):
    run = await cli_host(two_cards=True)
    run.start()
    notice = await run.notice()
    run.answer(notice)
    await run.waiting(dispatches=1)
    # No card is pending in the CLI now; the next real Runtime card appears later.
    run.reader.send(None)
    async with asyncio.timeout(5):
        while not run.reader.eof_seen:
            await asyncio.sleep(0)
    assert not run.task.done()
    run.gates[0].set()
    result = await run.finish()
    assert result.error.code == "INPUT_CLOSED" and result.exit_code == 130
    assert not result.error.details.get("cleanup_errors"), result.error
    assert run.host.executions == [{"value": "ONCE-1"}]
    assert len(run.cards) == 2 and len(run.answers) == 1
    assert len(run.host.dispatches) == 2
    assert all(row.state.terminal for row in run.snapshot.executions)
