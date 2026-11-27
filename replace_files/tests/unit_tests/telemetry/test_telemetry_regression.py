# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for telemetry instrumentation side effects.

These tests verify that enabling telemetry preserves core business behavior:
- request conversion still keeps original fields while adding trace metadata
- stream wrappers do not alter yielded chunks
- tool wrappers still emit the original frontend events
- session queue processing still works after cancellation
"""

# pylint: disable=protected-access

from __future__ import annotations

import asyncio
import logging
import sys
import time
import types
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

logger = logging.getLogger(__name__)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _collect(async_iterable):
    return [item async for item in async_iterable]


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise asyncio.TimeoutError("condition not met before timeout")


async def _shutdown_session_tasks(agent) -> None:
    for sid in list(agent._session_processors.keys()):
        task = agent._session_processors.get(sid)
        if task and not task.done():
            try:
                queue = agent._session_queues.get(sid)
                if queue is not None:
                    await queue.put((0, None))
                await asyncio.wait_for(task, timeout=1)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


@dataclass
class _FakeOutputSchema:
    type: str
    index: int
    payload: dict


def _build_fake_gateway_modules() -> dict[str, types.ModuleType]:
    gateway_pkg = types.ModuleType("jiuwenclaw.gateway")
    gateway_pkg.__path__ = []

    message_handler_module = types.ModuleType("jiuwenclaw.gateway.message_handler")

    class MessageHandler:
        def __init__(self, agent=None):
            self._agent = agent

        @staticmethod
        def message_to_e2a(msg):
            from jiuwenclaw.e2a.gateway_normalize import message_to_e2a_or_fallback

            return message_to_e2a_or_fallback(msg)

        async def process_stream(self, env, session_id, request_metadata=None):
            agent = getattr(self, "_agent", None)
            if agent is not None:
                await agent._ensure_session_processor(session_id)
            return None

    message_handler_module.MessageHandler = MessageHandler
    gateway_pkg.MessageHandler = MessageHandler
    gateway_pkg.message_handler = message_handler_module
    return {
        "jiuwenclaw.gateway": gateway_pkg,
        "jiuwenclaw.gateway.message_handler": message_handler_module,
    }


def _build_fake_agentserver_modules() -> dict[str, types.ModuleType]:
    agentserver_pkg = types.ModuleType("jiuwenclaw.agentserver")
    agentserver_pkg.__path__ = []

    interface_module = types.ModuleType("jiuwenclaw.agentserver.interface")
    react_agent_module = types.ModuleType("jiuwenclaw.agentserver.react_agent")
    session_manager_module = types.ModuleType("jiuwenclaw.agentserver.session_manager")

    class SessionManager:
        """Fake SessionManager for telemetry tests."""

        def __init__(self) -> None:
            self._session_tasks: dict = {}
            self._session_priorities: dict = {}
            self._session_queues: dict = {}
            self._session_processors: dict = {}

        async def cancel_session_task(self, session_id: str, log_msg_prefix: str = "") -> None:
            task = self._session_tasks.get(session_id)
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                self._session_tasks[session_id] = None

        async def ensure_session_processor(self, session_id: str) -> None:
            if session_id not in self._session_processors or self._session_processors[session_id].done():
                self._session_queues[session_id] = asyncio.PriorityQueue()
                self._session_priorities[session_id] = 0

                async def process_session_queue():
                    queue = self._session_queues[session_id]
                    while True:
                        try:
                            priority, task_func = await queue.get()
                            if task_func is None:
                                break

                            self._session_tasks[session_id] = asyncio.create_task(task_func())
                            try:
                                await self._session_tasks[session_id]
                            except asyncio.CancelledError:
                                # Inner task cancelled - continue processing queue
                                logger.debug("task cancelled: session_id=%s", session_id)
                            finally:
                                self._session_tasks[session_id] = None
                                queue.task_done()

                        except asyncio.CancelledError:
                            # Processor cancelled - break loop and cleanup
                            break

                    self._session_queues.pop(session_id, None)
                    self._session_priorities.pop(session_id, None)
                    self._session_tasks.pop(session_id, None)
                    self._session_processors.pop(session_id, None)

                self._session_processors[session_id] = asyncio.create_task(process_session_queue())

    session_manager_module.SessionManager = SessionManager

    class JiuWenClaw:
        """Fake JiuWenClaw using SessionManager for telemetry tests."""

        def __init__(self) -> None:
            self._agent_name = "main_agent"
            self._session_manager = SessionManager()

        async def process_message(self, request):
            return request

        async def process_message_stream(self, request):
            if False:
                yield request

        async def _cancel_session_task(self, session_id: str, log_msg_prefix: str = "") -> None:
            await self._session_manager.cancel_session_task(session_id, log_msg_prefix)

        async def _ensure_session_processor(self, session_id: str) -> None:
            await self._session_manager.ensure_session_processor(session_id)

        # Expose session manager attributes for test assertions
        @property
        def _session_tasks(self):
            return self._session_manager._session_tasks

        @property
        def _session_priorities(self):
            return self._session_manager._session_priorities

        @property
        def _session_queues(self):
            return self._session_manager._session_queues

        @property
        def _session_processors(self):
            return self._session_manager._session_processors

    class JiuClawReActAgent:
        async def _emit_tool_call(self, session, tool_call):
            await session.write_stream(
                _FakeOutputSchema(
                    type="tool_call",
                    index=0,
                    payload={
                        "tool_call": {
                            "name": getattr(tool_call, "name", ""),
                            "arguments": getattr(tool_call, "arguments", {}),
                            "tool_call_id": getattr(tool_call, "id", ""),
                        }
                    },
                )
            )

        async def _emit_tool_result(self, session, tool_call, result):
            await session.write_stream(
                _FakeOutputSchema(
                    type="tool_result",
                    index=0,
                    payload={
                        "tool_result": {
                            "tool_name": getattr(tool_call, "name", "") if tool_call else "",
                            "tool_call_id": getattr(tool_call, "id", "") if tool_call else "",
                            "result": str(result)[:1000] if result is not None else "",
                        }
                    },
                )
            )
            await session.write_stream(
                _FakeOutputSchema(
                    type="thinking",
                    index=0,
                    payload={},
                )
            )

    interface_module.JiuWenClaw = JiuWenClaw
    react_agent_module.JiuClawReActAgent = JiuClawReActAgent
    react_agent_module.OutputSchema = _FakeOutputSchema
    session_manager_module.SessionManager = SessionManager

    agentserver_pkg.JiuWenClaw = JiuWenClaw
    agentserver_pkg.JiuClawReActAgent = JiuClawReActAgent
    agentserver_pkg.SessionManager = SessionManager
    agentserver_pkg.interface = interface_module
    agentserver_pkg.react_agent = react_agent_module
    agentserver_pkg.session_manager = session_manager_module
    return {
        "jiuwenclaw.agentserver": agentserver_pkg,
        "jiuwenclaw.agentserver.interface": interface_module,
        "jiuwenclaw.agentserver.react_agent": react_agent_module,
        "jiuwenclaw.agentserver.session_manager": session_manager_module,
    }


@contextmanager
def _patched_regression_modules():
    core_module = types.ModuleType("openjiuwen.extensions.context_evolver.core")
    core_module.config = object()

    context_evolver_module = types.ModuleType("openjiuwen.extensions.context_evolver")
    context_evolver_module.core = core_module

    file_connector_pkg = types.ModuleType(
        "openjiuwen.extensions.context_evolver.core.file_connector"
    )

    json_connector_module = types.ModuleType(
        "openjiuwen.extensions.context_evolver.core.file_connector.json_file_connector"
    )
    json_connector_module.JSONFileConnector = object

    service_pkg = types.ModuleType("openjiuwen.extensions.context_evolver.service")
    task_memory_module = types.ModuleType(
        "openjiuwen.extensions.context_evolver.service.task_memory_service"
    )
    task_memory_module.AddMemoryRequest = object
    task_memory_module.TaskMemoryService = object

    modules = {
        "openjiuwen.extensions.context_evolver": context_evolver_module,
        "openjiuwen.extensions.context_evolver.core": core_module,
        "openjiuwen.extensions.context_evolver.core.file_connector": file_connector_pkg,
        "openjiuwen.extensions.context_evolver.core.file_connector.json_file_connector": json_connector_module,
        "openjiuwen.extensions.context_evolver.service": service_pkg,
        "openjiuwen.extensions.context_evolver.service.task_memory_service": task_memory_module,
        **_build_fake_gateway_modules(),
        **_build_fake_agentserver_modules(),
    }
    with patch.dict(sys.modules, modules):
        yield


async def _dispatch_stream_message(
    handler,
    request_id: str,
    session_id: str,
    channel_id: str = "web",
):
    from jiuwenclaw.schema.message import Message, ReqMethod

    msg = Message(
        id=request_id,
        type="req",
        channel_id=channel_id,
        session_id=session_id,
        params={"content": request_id, "mode": "agent"},
        timestamp=time.time(),
        ok=True,
        req_method=ReqMethod.CHAT_SEND,
        is_stream=True,
        metadata={"source": "test"},
    )
    env = handler.message_to_e2a(msg)
    await handler.process_stream(env, session_id)
    return env


class TestTelemetryRegression:
    @staticmethod
    def test_entry_message_to_e2a_preserves_fields_and_existing_channel_context():
        with _patched_regression_modules():
            from jiuwenclaw.gateway.message_handler import MessageHandler
            from jiuwenclaw.schema.message import Message, ReqMethod
            from jiuwenclaw.telemetry.instrumentors.entry import instrument_entry

            original_process_stream = MessageHandler.process_stream
            original_message_to_e2a = MessageHandler.message_to_e2a

            try:
                with patch(
                    "jiuwenclaw.telemetry.instrumentors.entry.inject_trace_context",
                    side_effect=lambda carrier: carrier.setdefault("traceparent", "test-trace"),
                ):
                    instrument_entry()

                    msg = Message(
                        id="req_001",
                        type="req",
                        channel_id="web",
                        session_id="sess_001",
                        params={"query": "hello"},
                        timestamp=time.time(),
                        ok=True,
                        req_method=ReqMethod.CHAT_SEND,
                        is_stream=True,
                        metadata={"source": "ui"},
                    )

                    env = MessageHandler.message_to_e2a(msg)

                    assert env.request_id == "req_001"
                    assert env.channel == "web"
                    assert env.session_id == "sess_001"
                    assert env.params == {"query": "hello"}
                    assert env.channel_context["source"] == "ui"
                    assert env.channel_context["traceparent"] == "test-trace"
            finally:
                MessageHandler.process_stream = original_process_stream
                MessageHandler.message_to_e2a = original_message_to_e2a

    @staticmethod
    def test_agent_stream_wrapper_preserves_chunk_sequence():
        with _patched_regression_modules():
            from jiuwenclaw.agentserver.interface import JiuWenClaw
            from jiuwenclaw.schema.agent import AgentRequest, AgentResponseChunk
            from jiuwenclaw.telemetry.instrumentors.agent import instrument_agent

            original_process_message = JiuWenClaw.process_message
            original_process_message_stream = JiuWenClaw.process_message_stream

            yielded_chunks = [
                AgentResponseChunk(
                    request_id="req_001",
                    channel_id="web",
                    payload={"event_type": "chat.delta", "content": "part-1"},
                ),
                AgentResponseChunk(
                    request_id="req_001",
                    channel_id="web",
                    payload={"event_type": "chat.final", "content": "part-2"},
                    is_complete=True,
                ),
            ]

            async def fake_stream(self, request):
                self._last_request = request
                for chunk in yielded_chunks:
                    yield chunk

            try:
                JiuWenClaw.process_message_stream = fake_stream
                instrument_agent()

                agent = JiuWenClaw()
                request = AgentRequest(
                    request_id="req_001",
                    channel_id="web",
                    session_id="sess_001",
                    params={"query": "hello"},
                    metadata={"source": "ui"},
                )

                chunks = _run(_collect(agent.process_message_stream(request)))

                assert chunks == yielded_chunks
                assert agent._last_request is request
                assert request.metadata == {"source": "ui"}
            finally:
                JiuWenClaw.process_message = original_process_message
                JiuWenClaw.process_message_stream = original_process_message_stream

    @staticmethod
    def test_tool_wrapper_still_emits_frontend_events():
        with _patched_regression_modules():
            from jiuwenclaw.agentserver.react_agent import JiuClawReActAgent
            import jiuwenclaw.telemetry.instrumentors.tool as tool_mod

            original_emit_tool_call = JiuClawReActAgent._emit_tool_call
            original_emit_tool_result = JiuClawReActAgent._emit_tool_result

            try:
                tool_mod._active_tool_spans.clear()
                tool_mod.instrument_tools()

                session = AsyncMock()
                fake_agent = MagicMock()
                tool_call = SimpleNamespace(
                    name="todo_list",
                    id="call_001",
                    arguments={"status": "pending"},
                )

                _run(JiuClawReActAgent._emit_tool_call(fake_agent, session, tool_call))
                _run(JiuClawReActAgent._emit_tool_result(fake_agent, session, tool_call, {"items": []}))

                assert session.write_stream.await_count == 3

                tool_call_event = session.write_stream.await_args_list[0].args[0]
                tool_result_event = session.write_stream.await_args_list[1].args[0]
                thinking_event = session.write_stream.await_args_list[2].args[0]

                assert tool_call_event.type == "tool_call"
                assert tool_call_event.payload["tool_call"]["name"] == "todo_list"
                assert tool_call_event.payload["tool_call"]["tool_call_id"] == "call_001"

                assert tool_result_event.type == "tool_result"
                assert tool_result_event.payload["tool_result"]["tool_name"] == "todo_list"
                assert tool_result_event.payload["tool_result"]["tool_call_id"] == "call_001"
                assert thinking_event.type == "thinking"

                assert tool_mod._active_tool_spans == {}
            finally:
                tool_mod._active_tool_spans.clear()
                JiuClawReActAgent._emit_tool_call = original_emit_tool_call
                JiuClawReActAgent._emit_tool_result = original_emit_tool_result

    @staticmethod
    def test_session_queue_still_runs_new_task_after_cancel():
        with _patched_regression_modules():
            from jiuwenclaw.agentserver.interface import JiuWenClaw
            from jiuwenclaw.telemetry.instrumentors.session import instrument_session

            original_init = JiuWenClaw.__init__
            original_ensure = JiuWenClaw._ensure_session_processor
            original_cancel = JiuWenClaw._cancel_session_task

            async def scenario():
                with patch(
                    "jiuwenclaw.telemetry.instrumentors.session._ensure_stuck_checker",
                    side_effect=lambda agent_server: None,
                ):
                    instrument_session(stuck_threshold_ms=1000, stuck_check_interval_s=60)

                    agent = JiuWenClaw()
                    session_id = "sess_cancel_then_continue"
                    slow_started = asyncio.Event()
                    quick_done = asyncio.Event()

                    async def slow_task():
                        slow_started.set()
                        await asyncio.sleep(100)

                    async def quick_task():
                        quick_done.set()
                        return "done"

                    try:
                        await agent._ensure_session_processor(session_id)

                        priority = agent._session_priorities[session_id]
                        agent._session_priorities[session_id] = priority - 1
                        await agent._session_queues[session_id].put((priority, slow_task))

                        await asyncio.wait_for(slow_started.wait(), timeout=1)
                        await agent._cancel_session_task(session_id, "test ")

                        priority = agent._session_priorities[session_id]
                        agent._session_priorities[session_id] = priority - 1
                        await agent._session_queues[session_id].put((priority, quick_task))

                        await asyncio.wait_for(quick_done.wait(), timeout=1)

                        await _wait_until(
                            lambda: agent._session_tasks.get(session_id) is None,
                            timeout=1,
                        )
                        assert session_id in agent._session_processors
                        assert not agent._session_processors[session_id].done()
                        assert getattr(agent, "_stuck_checker_task", None) is None
                    finally:
                        await _shutdown_session_tasks(agent)

            try:
                _run(scenario())
            finally:
                JiuWenClaw.__init__ = original_init
                JiuWenClaw._ensure_session_processor = original_ensure
                JiuWenClaw._cancel_session_task = original_cancel

    @staticmethod
    def test_session_metrics_track_created_and_active_sessions():
        with _patched_regression_modules():
            from jiuwenclaw.agentserver.interface import JiuWenClaw
            import jiuwenclaw.telemetry.instrumentors.session as session_mod
            import jiuwenclaw.telemetry.metrics as metrics_mod

            original_init = JiuWenClaw.__init__
            original_ensure = JiuWenClaw._ensure_session_processor
            original_cancel = JiuWenClaw._cancel_session_task

            async def scenario():
                session_mod._tracked_session_managers.clear()
                metrics_mod.set_session_active_observer(None)

                with patch(
                    "jiuwenclaw.telemetry.instrumentors.session._ensure_stuck_checker",
                    side_effect=lambda agent_server: None,
                ):
                    with patch.object(metrics_mod.session_created_count, "add") as mock_created:
                        session_mod.instrument_session(
                            stuck_threshold_ms=1000,
                            stuck_check_interval_s=60,
                        )

                        agent = JiuWenClaw()
                        assert session_mod._count_active_sessions() == 0
                        assert list(metrics_mod._observe_session_active(MagicMock()))[0].value == 0

                        try:
                            await agent._ensure_session_processor("sess_created_1")
                            assert mock_created.call_count == 1
                            assert session_mod._count_active_sessions() == 1
                            assert list(metrics_mod._observe_session_active(MagicMock()))[0].value == 1

                            # Reusing the same session must not increment the created counter again.
                            await agent._ensure_session_processor("sess_created_1")
                            assert mock_created.call_count == 1

                            await agent._ensure_session_processor("sess_created_2")
                            assert mock_created.call_count == 2
                            assert session_mod._count_active_sessions() == 2
                            assert list(metrics_mod._observe_session_active(MagicMock()))[0].value == 2
                        finally:
                            await _shutdown_session_tasks(agent)

                        assert session_mod._count_active_sessions() == 0
                        assert list(metrics_mod._observe_session_active(MagicMock()))[0].value == 0

            try:
                _run(scenario())
            finally:
                session_mod._tracked_session_managers.clear()
                metrics_mod.set_session_active_observer(None)
                JiuWenClaw.__init__ = original_init
                JiuWenClaw._ensure_session_processor = original_ensure
                JiuWenClaw._cancel_session_task = original_cancel

    @staticmethod
    def test_same_session_two_messages_record_one_created_and_two_requests():
        with _patched_regression_modules():
            from jiuwenclaw.agentserver.interface import JiuWenClaw
            from jiuwenclaw.gateway.message_handler import MessageHandler
            import jiuwenclaw.telemetry.instrumentors.entry as entry_mod
            import jiuwenclaw.telemetry.instrumentors.session as session_mod
            import jiuwenclaw.telemetry.metrics as metrics_mod

            original_init = JiuWenClaw.__init__
            original_ensure = JiuWenClaw._ensure_session_processor
            original_cancel = JiuWenClaw._cancel_session_task
            original_process_stream = MessageHandler.process_stream
            original_message_to_e2a = MessageHandler.message_to_e2a

            async def scenario():
                session_mod._tracked_session_managers.clear()
                metrics_mod.set_session_active_observer(None)

                with patch(
                    "jiuwenclaw.telemetry.instrumentors.session._ensure_stuck_checker",
                    side_effect=lambda agent_server: None,
                ):
                    with patch(
                        "jiuwenclaw.telemetry.instrumentors.entry.inject_trace_context",
                        side_effect=lambda carrier: carrier.setdefault("traceparent", "test-traceparent"),
                    ):
                        session_mod.instrument_session(
                            stuck_threshold_ms=1000,
                            stuck_check_interval_s=60,
                        )
                        entry_mod.instrument_entry()

                        agent = JiuWenClaw()
                        handler = MessageHandler(agent)

                        try:
                            with patch.object(metrics_mod.session_created_count, "add") as mock_created:
                                with patch.object(metrics_mod.request_count, "add") as mock_request:
                                    req_1 = await _dispatch_stream_message(
                                        handler,
                                        "req_same_1",
                                        "sess_same_twice",
                                    )
                                    req_2 = await _dispatch_stream_message(
                                        handler,
                                        "req_same_2",
                                        "sess_same_twice",
                                    )

                                    assert req_1.channel_context["source"] == "test"
                                    assert req_1.channel_context["traceparent"] == "test-traceparent"
                                    assert req_2.channel_context["source"] == "test"
                                    assert req_2.channel_context["traceparent"] == "test-traceparent"

                                    assert mock_created.call_count == 1
                                    assert mock_request.call_count == 2
                                    assert all(
                                        call.args[0] == 1
                                        and call.args[1].get("jiuwenclaw.channel.id") == "web"
                                        for call in mock_request.call_args_list
                                    )
                                    assert len(agent._session_processors) == 1
                                    assert session_mod._count_active_sessions() == 1
                                    assert list(metrics_mod._observe_session_active(MagicMock()))[0].value == 1
                        finally:
                            await _shutdown_session_tasks(agent)

                        assert session_mod._count_active_sessions() == 0
                        assert list(metrics_mod._observe_session_active(MagicMock()))[0].value == 0

            try:
                _run(scenario())
            finally:
                session_mod._tracked_session_managers.clear()
                metrics_mod.set_session_active_observer(None)
                JiuWenClaw.__init__ = original_init
                JiuWenClaw._ensure_session_processor = original_ensure
                JiuWenClaw._cancel_session_task = original_cancel
                MessageHandler.process_stream = original_process_stream
                MessageHandler.message_to_e2a = original_message_to_e2a

    @staticmethod
    def test_three_sessions_two_messages_each_record_three_created_and_six_requests():
        with _patched_regression_modules():
            from jiuwenclaw.agentserver.interface import JiuWenClaw
            from jiuwenclaw.gateway.message_handler import MessageHandler
            import jiuwenclaw.telemetry.instrumentors.entry as entry_mod
            import jiuwenclaw.telemetry.instrumentors.session as session_mod
            import jiuwenclaw.telemetry.metrics as metrics_mod

            original_init = JiuWenClaw.__init__
            original_ensure = JiuWenClaw._ensure_session_processor
            original_cancel = JiuWenClaw._cancel_session_task
            original_process_stream = MessageHandler.process_stream
            original_message_to_e2a = MessageHandler.message_to_e2a

            async def scenario():
                session_mod._tracked_session_managers.clear()
                metrics_mod.set_session_active_observer(None)

                with patch(
                    "jiuwenclaw.telemetry.instrumentors.session._ensure_stuck_checker",
                    side_effect=lambda agent_server: None,
                ):
                    with patch(
                        "jiuwenclaw.telemetry.instrumentors.entry.inject_trace_context",
                        side_effect=lambda carrier: carrier.setdefault("traceparent", "test-traceparent"),
                    ):
                        session_mod.instrument_session(
                            stuck_threshold_ms=1000,
                            stuck_check_interval_s=60,
                        )
                        entry_mod.instrument_entry()

                        agent = JiuWenClaw()
                        handler = MessageHandler(agent)
                        session_ids = ("sess_multi_1", "sess_multi_2", "sess_multi_3")

                        try:
                            with patch.object(metrics_mod.session_created_count, "add") as mock_created:
                                with patch.object(metrics_mod.request_count, "add") as mock_request:
                                    for session_id in session_ids:
                                        for index in range(1, 3):
                                            req = await _dispatch_stream_message(
                                                handler,
                                                f"{session_id}_req_{index}",
                                                session_id,
                                            )
                                            assert req.channel_context["source"] == "test"
                                            assert req.channel_context["traceparent"] == "test-traceparent"

                                    assert mock_created.call_count == 3
                                    assert mock_request.call_count == 6
                                    assert all(
                                        call.args[0] == 1
                                        and call.args[1].get("jiuwenclaw.channel.id") == "web"
                                        for call in mock_request.call_args_list
                                    )
                                    assert set(agent._session_processors.keys()) == set(session_ids)
                                    assert session_mod._count_active_sessions() == 3
                                    assert list(metrics_mod._observe_session_active(MagicMock()))[0].value == 3
                        finally:
                            await _shutdown_session_tasks(agent)

                        assert session_mod._count_active_sessions() == 0
                        assert list(metrics_mod._observe_session_active(MagicMock()))[0].value == 0

            try:
                _run(scenario())
            finally:
                session_mod._tracked_session_managers.clear()
                metrics_mod.set_session_active_observer(None)
                JiuWenClaw.__init__ = original_init
                JiuWenClaw._ensure_session_processor = original_ensure
                JiuWenClaw._cancel_session_task = original_cancel
                MessageHandler.process_stream = original_process_stream
                MessageHandler.message_to_e2a = original_message_to_e2a

    @staticmethod
    def test_llm_error_uses_existing_status_dimension_counter():
        with _patched_regression_modules():
            from jiuwenclaw.agentserver.react_agent import JiuClawReActAgent
            import jiuwenclaw.telemetry.instrumentors.llm as llm_mod
            import jiuwenclaw.telemetry.metrics as metrics_mod

            original_call_llm = getattr(JiuClawReActAgent, "_call_llm", None)

            async def failing_call_llm(self, messages, tools=None, session=None, chunk_threshold=10):
                raise RuntimeError("LLM timeout")

            try:
                JiuClawReActAgent._call_llm = failing_call_llm
                llm_mod.instrument_llm()

                agent = JiuClawReActAgent()
                agent._config = SimpleNamespace(
                    model_name="deepseek-chat",
                    model_client_config={"client_provider": "openai"},
                    model_config_obj=SimpleNamespace(temperature=None, top_p=None),
                )

                with patch.object(metrics_mod.llm_call_count, "add") as mock_metric_add:
                    with pytest.raises(RuntimeError, match="LLM timeout"):
                        _run(agent._call_llm([], None, None, 10))

                mock_metric_add.assert_called_once()
                call_args = mock_metric_add.call_args
                assert call_args[0][0] == 1  # value
                assert call_args[0][1]["gen_ai.request.model"] == "deepseek-chat"
                assert call_args[0][1]["status"] == "error"
            finally:
                if original_call_llm is None:
                    delattr(JiuClawReActAgent, "_call_llm")
                else:
                    JiuClawReActAgent._call_llm = original_call_llm
