"""Real SQLite -> SDK mailbox -> TinyAgent HTTP -> Native/ReAct/tools -> ACK.

Only model responses are scripted. No dispatcher, harness, context, tool
executor, fast-model adapter, or message-manager method is mocked.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from aiohttp import web

from openjiuwen.agent_teams.context import set_session_id, reset_session_id
from openjiuwen.agent_teams.messager import InProcessMessager
from openjiuwen.agent_teams.harness.team_harness import TeamHarness
from openjiuwen.agent_teams.harness.state import HarnessState
from openjiuwen.agent_teams.schema.team import TeamRole
from openjiuwen.agent_teams.agent.team_agent import TeamAgent
from openjiuwen.agent_teams.agent.coordination.handlers.message import MessageHandler
from openjiuwen.agent_teams.tools.database import DatabaseConfig, DatabaseType, TeamDatabase
from openjiuwen.agent_teams.tools.message_manager import TeamMessageManager
from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig
from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.factory import DeepAgentParts
from openjiuwen.harness.schema.config import DeepAgentConfig
from openjiuwen.harness.schema.deep_agent_spec import TeamModelConfig

from jiuwenswarm.agents.harness.team.duplex_shadow import install_shadow_observer, snapshot_from_native
from jiuwenswarm.agents.harness.team.duplex_native import DuplexNativeHarness, DeliverySuperseded


class Endpoint:
    def __init__(self):
        self.calls = []
        self.fast_action = "INTERRUPT"
        self.fast_error = False
        self.fast_gate = asyncio.Event()
        self.fast_gate.set()
        self.model_gate = asyncio.Event()
        self.model_entered = asyncio.Event()
        self.block_stream_call = None
        self.fast_entered = asyncio.Event()
        self.block_model = True
        self.block_prompt = None
        self.tool_first = True
        self.repeat_tool_without_result = False
        self.final_content = "Finished using PostgreSQL."
        self.on_slow_request = None
        self.slow_responder = None

    async def handle(self, request):
        body = await request.json()
        if any(isinstance(m.get("content"), list) and any(
                p.get("type") == "image_url" for p in m["content"] if isinstance(p, dict))
               for m in body["messages"]):
            return web.json_response({"id": "probe", "object": "chat.completion", "model": body["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "red"},
                             "finish_reason": "stop"}], "usage": {"total_tokens": 1}})
        self.calls.append(body)
        model = body["model"]
        if model == "slow" and self.on_slow_request is not None:
            self.on_slow_request()
        call = sum(c["model"] == model for c in self.calls)
        slow_call = call
        message = {"role": "assistant", "content": self.final_content}
        if model == "fast":
            self.fast_entered.set()
            await self.fast_gate.wait()
            if self.fast_error:
                return web.json_response({"error": {"message": "scripted failure"}}, status=500)
            function = next(t["function"] for t in body["tools"]
                            if t["function"]["name"] == "structured_output")
            assert set(function["parameters"]["properties"]) == {"action"}
            result = {"action": self.fast_action}
            message = self.tool_message("structured_output", result)
        elif self.slow_responder is not None:
            message = self.slow_responder(body)
        elif self.tool_first and (slow_call == 1 or (self.repeat_tool_without_result and not any(
                m.get("role") == "tool" and "committed exactly once" in str(m.get("content"))
                for m in body["messages"]))):
            message = self.tool_message("write_once", {})
        elif self.block_model and (
                self.block_prompt in json.dumps(body["messages"]) if self.block_prompt is not None
                else slow_call == (2 if self.tool_first else 1)):
            self.model_entered.set()
            await self.model_gate.wait()
            message["content"] = "Obsolete Kafka answer."
        if not body.get("stream"):
            return web.json_response({"id": "test", "object": "chat.completion", "model": model,
                "choices": [{"index": 0, "message": message, "finish_reason":
                             "tool_calls" if "tool_calls" in message else "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        delta = dict(message)
        if "tool_calls" in delta:
            delta["tool_calls"][0]["index"] = 0
        chunk = {"id": "test", "object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
        await response.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        if model == "slow" and call == self.block_stream_call:
            await self.model_gate.wait()
        chunk["choices"] = [{"index": 0, "delta": {}, "finish_reason":
                             "tool_calls" if "tool_calls" in message else "stop"}]
        await response.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())
        await response.write_eof()
        return response

    @staticmethod
    def tool_message(name, arguments):
        return {"role": "assistant", "content": None, "tool_calls": [{
            "id": uuid.uuid4().hex, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}]}


class WriteOnce(Tool):
    def __init__(self, path):
        super().__init__(ToolCard(name="write_once", description="Record an irreversible operation",
                                 input_params={"type": "object", "properties": {}}))
        self.path = path
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()
        self.gate.set()
        self.cancelled = 0
        self.on_commit = None

    async def invoke(self, inputs, **kwargs):
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write("committed\n")
        if self.on_commit is not None:
            self.on_commit()
        self.entered.set()
        try:
            await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return "Irreversible operation committed exactly once."

    async def stream(self, inputs, **kwargs):
        raise NotImplementedError


class Host:
    # Use the production SDK delivery entry, including the installed adapter.
    def __init__(self, harness, fast_config):
        self.harness = harness
        self.blueprint = NS(member_name="A2")
        self.tiny_agent_model_resolver = lambda name: fast_config if name == "fast" else None

    def has_pending_interrupt(self):
        return self.harness.has_pending_interrupt()

    async def deliver_input(self, content, *, use_steer=True):
        return await TeamAgent.deliver_input(self, content, use_steer=use_steer)


async def wait_until(predicate, timeout=6):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest_asyncio.fixture
async def world(tmp_path, monkeypatch, request):
    from jiuwenswarm.common import config

    settings = {"duplex_router": {"mode": "active", "model_name": "fast", "timeout_seconds": 2}}
    settings["duplex_router"].update(getattr(request, "param", {}))
    monkeypatch.setattr(config, "get_config", lambda: settings)
    install_shadow_observer()
    endpoint = Endpoint()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", endpoint.handle)
    server = web.AppRunner(app, shutdown_timeout=0.1)
    await server.setup()
    site = web.TCPSite(server, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    client = ModelClientConfig(client_provider="OpenAI", api_base=f"http://127.0.0.1:{port}/v1",
                              api_key="local-test", max_retries=0)
    fast = TeamModelConfig(model_client_config=client,
                          model_request_config=ModelRequestConfig(model_name="fast"))
    slow = Model(model_client_config=client, model_config=ModelRequestConfig(model_name="slow"))
    tool = WriteOnce(tmp_path / "effects.txt")
    card = AgentCard(id=uuid.uuid4().hex, name="duplex-e2e")

    class Spec:
        def resolve_parts(self, context=None):
            return DeepAgentParts(config=DeepAgentConfig(card=card, model=slow,
                system_prompt="Complete the task using tools. Respect new constraints.",
                enable_task_loop=True, max_iterations=8),
                rails=[], tool_cards=[tool.card], tool_instances=[tool])

    await Runner.start()
    harness = TeamHarness.build(agent_spec=Spec(), role=TeamRole.LEADER, member_name="A2")
    token = set_session_id("duplex_" + uuid.uuid4().hex)
    db = TeamDatabase(DatabaseConfig(db_type=DatabaseType.SQLITE,
                                     connection_string=str(tmp_path / "messages.db")))
    await db.initialize()
    await db.team.create_team(team_name="duplex", display_name="duplex", leader_member_name="A2")
    for member in ("A1", "A2"):
        await db.member.create_member(member_name=member, team_name="duplex", display_name=member,
                                      agent_card=card.model_dump_json(), status="busy")
    # No subscriber by default: those cases exercise DB polling after event loss.
    messager = InProcessMessager()
    manager = TeamMessageManager(team_name="duplex", db=db, messager=messager, member_name="A1")
    host = Host(harness, fast)
    blueprint = NS(role=TeamRole.LEADER, member_name="A2", language="en", team_spec=None)
    handler = MessageHandler(host, blueprint, NS(message_manager=manager, team_backend=None), NS())
    chunks = []
    await harness.start()

    async def collect():
        async for chunk in harness.outputs():
            chunks.append(chunk)

    collector = asyncio.create_task(collect())
    result = NS(endpoint=endpoint, tool=tool, harness=harness, native=harness.inner_agent,
                manager=manager, handler=handler, settings=settings, chunks=chunks, host=host,
                messager=messager)
    try:
        if settings["duplex_router"]["mode"] == "active":
            assert isinstance(result.native, DuplexNativeHarness), "real TeamHarness must construct duplex native"
        yield result
    finally:
        endpoint.model_gate.set()
        endpoint.fast_gate.set()
        tool.gate.set()
        await harness.stop()
        await asyncio.wait_for(collector, 3)
        await db.close()
        reset_session_id(token)
        await Runner.stop()
        await server.cleanup()


async def send_message(world, text="Customer forbids Kafka. Use PostgreSQL."):
    return await world.manager.send_message(content=text, to_member_name="A2")




async def poll_and_apply(world):
    await world.handler.on_poll_mailbox(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["model", "tool"])
async def test_monitor_correction_invalidates_kafka_plan_before_redis_replan(world, phase):
    """Real pause/continue: a later lifecycle rollback must not revive a wrong plan."""
    from openjiuwen.harness.schema.task import TaskPlan, TodoItem

    w = world
    original = "Implement the order queue using Redis; retain idempotency."
    correction = "Monitor: the user asked for Redis. Your Kafka plan is wrong."
    resumed = []

    def install_wrong_plan():
        state = w.native.load_state(w.native._session)
        state.task_plan = TaskPlan(goal="Build the queue with Kafka", tasks=[
            TodoItem(id="kafka", content="Install Kafka and write a Kafka producer")])
        w.native.save_state(w.native._session, state)

    def inspect_request():
        active = w.native.active_round
        if active.round_id > 1:
            resumed.append((w.native.load_state(w.native._session).task_plan,
                            active.original_query,
                            active.pre_round_snapshot.deep_agent_state["task_plan"]))

    w.tool.on_commit = install_wrong_plan
    w.endpoint.on_slow_request = inspect_request
    if phase == "tool":
        w.endpoint.block_model = False
        w.tool.gate.clear()
    await w.harness.send(original)
    await asyncio.wait_for((w.tool.entered if phase == "tool" else w.endpoint.model_entered).wait(), 6)
    mid = await send_message(w, correction)
    drain = asyncio.create_task(poll_and_apply(w))
    if phase == "tool":
        await wait_until(lambda: w.harness.state is HarnessState.PAUSING)
        w.tool.gate.set()
    await asyncio.wait_for(drain, 6)
    await wait_until(lambda: w.harness.state is HarnessState.IDLE)
    assert resumed
    assert all(plan is None and snapshot_plan is None for plan, _, snapshot_plan in resumed)
    assert all("[Execution plan invalidated]" in query and query != original
               for _, query, _ in resumed)
    recovered = json.dumps([c for c in w.endpoint.calls if c["model"] == "slow"][-1]["messages"])
    assert original in recovered and correction in recovered
    assert "do not resume them" in recovered
    assert "Irreversible operation committed exactly once" in recovered
    assert recovered.count(mid) == 1
    assert w.tool.path.read_text() == "committed\n"
    assert w.tool.cancelled == 0
    assert not await w.manager.get_messages(to_member_name="A2", unread_only=True)


@pytest.mark.asyncio
async def test_model_interrupt_keeps_original_task_tool_result_and_db_ack(world):
    w = world
    await w.harness.send("Implement the order event system with Kafka.")
    await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
    before = w.native.active_round.round_id
    mid = await send_message(w)
    await asyncio.wait_for(poll_and_apply(w), 6)
    await wait_until(lambda: w.harness.state is HarnessState.IDLE)
    assert w.native._st.round_id_counter > before
    assert not await w.manager.get_messages(to_member_name="A2", unread_only=True)
    assert w.tool.path.read_text() == "committed\n"
    assert w.tool.cancelled == 0
    slow = [c for c in w.endpoint.calls if c["model"] == "slow"]
    recovered = json.dumps(slow[-1]["messages"])
    assert "Implement the order event system" in recovered
    assert "Irreversible operation committed exactly once" in recovered
    assert mid in recovered and "Customer forbids Kafka" in recovered
    assert "Obsolete Kafka answer" not in recovered
    assert "Request cancelled by user" not in recovered
    assert any(getattr(c, "type", None) == "round_aborted" for c in w.chunks)
    assert any(c["model"] == "fast" for c in w.endpoint.calls)


@pytest.mark.asyncio
async def test_interrupt_first_model_call_preserves_original_query(world):
    w = world
    w.endpoint.tool_first = False
    await w.harness.send("Plan order events with Kafka; include migration steps.")
    await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
    mid = await send_message(w)
    await asyncio.wait_for(poll_and_apply(w), 6)
    await wait_until(lambda: w.harness.state is HarnessState.IDLE)
    slow = [c for c in w.endpoint.calls if c["model"] == "slow"]
    assert len(slow) == 2
    recovered = json.dumps(slow[-1]["messages"])
    assert "include migration steps" in recovered
    assert mid in recovered and "Customer forbids Kafka" in recovered
    assert "Obsolete Kafka answer" not in recovered
    assert "Request cancelled by user" not in recovered
    assert not w.tool.path.exists()
    assert not await w.manager.get_messages(to_member_name="A2", unread_only=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_tool_call", [False, True])
async def test_interrupt_discards_partial_stream_without_executing_its_tool(world, with_tool_call):
    w = world
    original = "Plan order events with Kafka; include migration steps."
    draft = (w.endpoint.tool_message("write_once", {}) if with_tool_call
             else {"role": "assistant"})
    draft["content"] = "Obsolete Kafka draft."
    replies = iter([draft, {"role": "assistant", "content": "Finished using PostgreSQL."}])
    w.endpoint.slow_responder = lambda body: next(replies)
    w.endpoint.block_stream_call = 1
    await w.harness.send(original)
    await wait_until(lambda: any(
        getattr(c, "type", None) == "llm_output"
        and "Obsolete Kafka draft" in str(c.payload) for c in w.chunks))
    assert not w.tool.path.exists()
    mid = await send_message(w)
    await asyncio.wait_for(poll_and_apply(w), 6)
    await wait_until(lambda: w.harness.state is HarnessState.IDLE)
    slow = [c for c in w.endpoint.calls if c["model"] == "slow"]
    assert len(slow) == 2
    recovered = json.dumps(slow[-1]["messages"])
    assert recovered.count(original) == 1 and recovered.count(mid) == 1
    assert "Obsolete Kafka draft" not in recovered
    assert "Request cancelled by user" not in recovered
    assert all(m["role"] != "tool" and not m.get("tool_calls") for m in slow[-1]["messages"])
    assert not w.tool.path.exists()
    assert any(getattr(c, "type", None) == "round_aborted" for c in w.chunks)
    assert not await w.manager.get_messages(to_member_name="A2", unread_only=True)


@pytest.mark.asyncio
async def test_interrupt_during_tool_waits_for_commit_and_does_not_repeat(world):
    w = world
    w.tool.gate.clear()
    w.endpoint.block_model = False
    await w.harness.send("Implement order events.")
    await asyncio.wait_for(w.tool.entered.wait(), 6)
    before = w.native.active_round.round_id
    mid = await send_message(w)
    drain = asyncio.create_task(poll_and_apply(w))
    await wait_until(lambda: w.harness.state is HarnessState.PAUSING)
    assert not drain.done()
    assert await w.manager.get_messages(to_member_name="A2", unread_only=True)
    assert w.native.active_round.round_id == before
    w.tool.gate.set()
    await asyncio.wait_for(drain, 6)
    await wait_until(lambda: w.harness.state is HarnessState.IDLE)
    assert w.tool.path.read_text() == "committed\n"
    assert w.tool.cancelled == 0
    assert not await w.manager.get_messages(to_member_name="A2", unread_only=True)
    recovered = json.dumps([c for c in w.endpoint.calls if c["model"] == "slow"][-1])
    assert "Irreversible operation committed exactly once" in recovered
    assert mid in recovered


@pytest.mark.asyncio
async def test_append_during_tool_is_adopted_without_restart(world):
    w = world
    w.endpoint.fast_action = "APPEND"
    w.endpoint.block_model = False
    w.tool.gate.clear()
    await w.harness.send("Research the database options.")
    await asyncio.wait_for(w.tool.entered.wait(), 6)
    before = w.native.active_round.round_id
    mid = await send_message(w, "Also export Markdown.")
    await asyncio.wait_for(poll_and_apply(w), 6)
    assert w.native.active_round.round_id == before
    assert w.harness.state is HarnessState.RUNNING
    w.tool.gate.set()
    await wait_until(lambda: w.harness.state is HarnessState.IDLE)
    assert w.native._st.round_id_counter == before
    assert mid in json.dumps([c for c in w.endpoint.calls if c["model"] == "slow"][-1])


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_source", ["router", "sdk_model"])
async def test_fast_timeout_falls_back_and_db_message_is_not_lost(world, timeout_source):
    w = world
    if timeout_source == "router":
        w.settings["duplex_router"]["timeout_seconds"] = 0.05
    else:
        w.settings["duplex_router"].pop("timeout_seconds")
        w.host.tiny_agent_model_resolver("fast").model_client_config.timeout = 0.05
    w.endpoint.fast_gate.clear()
    w.endpoint.block_model = False
    w.tool.gate.clear()
    await w.harness.send("Research database options.")
    await asyncio.wait_for(w.tool.entered.wait(), 6)
    mid = await send_message(w, "Also export Markdown.")
    await asyncio.wait_for(poll_and_apply(w), 3)
    assert not await w.manager.get_messages(to_member_name="A2", unread_only=True)
    w.tool.gate.set()
    await wait_until(lambda: w.harness.state is HarnessState.IDLE)
    assert mid in json.dumps([c for c in w.endpoint.calls if c["model"] == "slow"][-1])


@pytest.mark.asyncio
async def test_append_survives_a_later_interrupt_before_it_is_consumed(world):
    w = world
    await w.harness.send("Implement the order system.")
    await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
    w.endpoint.fast_action = "APPEND"
    first = await send_message(w, "Also include latency figures.")
    await asyncio.wait_for(poll_and_apply(w), 6)
    w.endpoint.fast_action = "INTERRUPT"
    second = await send_message(w)
    await asyncio.wait_for(poll_and_apply(w), 6)
    await wait_until(lambda: w.harness.state is HarnessState.IDLE)
    recovered = json.dumps([c for c in w.endpoint.calls if c["model"] == "slow"][-1]["messages"])
    assert recovered.count(first) == 1
    assert recovered.count(second) == 1
    assert w.tool.path.read_text() == "committed\n"


@pytest.mark.asyncio
async def test_admitted_append_survives_repeated_interrupts(world):
    w = world
    original = "Implement the order system; retain idempotency."
    w.tool.gate.clear()
    w.endpoint.repeat_tool_without_result = True
    await w.harness.send(original)
    await asyncio.wait_for(w.tool.entered.wait(), 6)
    w.endpoint.fast_action = "APPEND"
    appended = await send_message(w, "Also include latency figures.")
    await asyncio.wait_for(poll_and_apply(w), 6)
    w.tool.gate.set()
    await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
    admitted = [c for c in w.endpoint.calls if c["model"] == "slow"][-1]["messages"]
    assert appended in json.dumps(admitted)

    w.endpoint.fast_action = "INTERRUPT"
    w.endpoint.block_prompt = "Customer forbids Kafka"
    w.endpoint.model_entered.clear()
    first = await send_message(w)
    await asyncio.wait_for(poll_and_apply(w), 6)
    await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
    w.endpoint.block_prompt = None
    second = await send_message(w, "Keep PostgreSQL, but change the delivery plan to an outbox.")
    await asyncio.wait_for(poll_and_apply(w), 6)
    await wait_until(lambda: w.harness.state is HarnessState.IDLE)
    recovered = json.dumps([c for c in w.endpoint.calls if c["model"] == "slow"][-1]["messages"])
    for content in (original, appended, first, second, "Irreversible operation committed exactly once"):
        assert recovered.count(content) == 1
    assert "Request cancelled by user" not in recovered
    assert w.native._st.round_id_counter == 3
    assert w.tool.path.read_text() == "committed\n"
    assert w.tool.cancelled == 0
    assert not await w.manager.get_messages(to_member_name="A2", unread_only=True)




@pytest.mark.asyncio
async def test_lifecycle_pause_supersedes_restart_and_leaves_message_unread(world):
    w = world
    w.tool.gate.clear()
    w.endpoint.block_model = False
    await w.harness.send("Implement the order system.")
    await asyncio.wait_for(w.tool.entered.wait(), 6)
    await send_message(w)
    drain = asyncio.create_task(poll_and_apply(w))
    await wait_until(lambda: w.harness.state is HarnessState.PAUSING)
    await w.harness.pause()
    with pytest.raises(DeliverySuperseded):
        await asyncio.wait_for(drain, 3)
    w.tool.gate.set()
    await wait_until(lambda: w.harness.state is HarnessState.PAUSED)
    assert w.native._st.round_id_counter == 1
    assert await w.manager.get_messages(to_member_name="A2", unread_only=True)
    assert w.tool.path.read_text() == "committed\n"
    assert w.tool.cancelled == 0


@pytest.mark.asyncio
async def test_manual_pause_after_internal_interrupt_keeps_admitted_correction(world):
    w = world
    original = "Implement the order system; retain idempotency."
    await w.harness.send(original)
    await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
    w.endpoint.block_prompt = "Customer forbids Kafka"
    w.endpoint.model_entered.clear()
    mid = await send_message(w)
    await asyncio.wait_for(poll_and_apply(w), 6)
    await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
    await w.harness.pause()
    assert w.harness.state is HarnessState.PAUSED
    w.endpoint.block_prompt = None
    await w.harness.resume()
    await wait_until(lambda: w.harness.state is HarnessState.IDLE)
    recovered = json.dumps([c for c in w.endpoint.calls if c["model"] == "slow"][-1]["messages"])
    assert recovered.count(original) == 1
    assert recovered.count(mid) == 1 and "Customer forbids Kafka" in recovered
    assert "Irreversible operation committed exactly once" in recovered
    assert w.tool.path.read_text() == "committed\n"
    assert w.tool.cancelled == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["abort", "stop"])
async def test_explicit_cancel_supersedes_pending_interrupt_without_restart(world, control):
    w = world
    w.tool.gate.clear()
    w.endpoint.block_model = False
    await w.harness.send("Implement the order system.")
    await asyncio.wait_for(w.tool.entered.wait(), 6)
    await send_message(w)
    drain = asyncio.create_task(poll_and_apply(w))
    await wait_until(lambda: w.harness.state is HarnessState.PAUSING)
    if control == "abort":
        await w.harness.abort(immediate=True)
        assert w.harness.state is HarnessState.IDLE
    else:
        await w.harness.stop()
        assert w.harness.state is HarnessState.TERMINATED
    with pytest.raises(DeliverySuperseded):
        await asyncio.wait_for(drain, 3)
    assert w.native._st.round_id_counter == 1
    assert w.native._duplex_pending is None
    assert await w.manager.get_messages(to_member_name="A2", unread_only=True)
    assert w.tool.cancelled == 1


@pytest.mark.asyncio
async def test_stale_supervisor_command_cannot_interrupt_newer_context(world):
    w = world
    await w.harness.send("Implement the order system.")
    await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
    old = snapshot_from_native(w.native)
    await w.harness.send("Also include migration steps.", immediate=True)
    result = await w.native.interrupt("old decision", version=old.context_version,
                                      message_id="old")
    assert result == "STALE"
    assert w.native.active_round.round_id == int(old.round_id)


@pytest.mark.asyncio
async def test_failed_db_ack_retry_does_not_reexecute_delivery(world, monkeypatch):
    w = world
    await w.harness.send("Implement the order system.")
    await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
    await send_message(w)
    ack = w.manager.mark_messages_read

    async def fail_ack(*args, **kwargs):
        raise OSError("injected database write failure")

    monkeypatch.setattr(w.manager, "mark_messages_read", fail_ack)
    with pytest.raises(OSError):
        await poll_and_apply(w)
    await wait_until(lambda: w.harness.state is HarnessState.IDLE)
    rounds = w.native._st.round_id_counter
    monkeypatch.setattr(w.manager, "mark_messages_read", ack)
    await poll_and_apply(w)
    assert w.native._st.round_id_counter == rounds
    assert w.tool.path.read_text() == "committed\n"
    assert not await w.manager.get_messages(to_member_name="A2", unread_only=True)


@pytest.mark.asyncio
async def test_user_input_uses_same_router(world):
    from openjiuwen.agent_teams.agent.coordination.handlers.agent_lifecycle import AgentLifecycleHandler
    from openjiuwen.agent_teams.agent.coordination.event_bus import InnerEventMessage, InnerEventType
    w = world
    await w.harness.send("Implement the order system.")
    await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
    handler = AgentLifecycleHandler(w.host, w.handler._blueprint, w.handler._infra, NS())
    await handler.on_user_input(InnerEventMessage(event_type=InnerEventType.USER_INPUT,
                              payload={"content": "Change goal: use PostgreSQL only.", "message_id": "u204"}))
    await wait_until(lambda: w.harness.state is HarnessState.IDLE)
    recovered = json.dumps([c for c in w.endpoint.calls if c["model"] == "slow"][-1]["messages"])
    assert "Change goal: use PostgreSQL only" in recovered
    assert w.native._st.round_id_counter == 2


@pytest.mark.asyncio
async def test_official_interruptbench_update_reaches_native_unchanged(world, tmp_path):
    """Official input through real runtime; not a WebArena task success test."""
    from jiuwenswarm.common.duplex_public_benchmark import load_official_interrupt
    from openjiuwen.agent_teams.agent.coordination.handlers.agent_lifecycle import AgentLifecycleHandler

    source = os.environ.get("JIUWEN_INTERRUPT_BENCH_ROOT")
    if not source:
        pytest.skip("set JIUWEN_INTERRUPT_BENCH_ROOT to the pinned official checkout")
    root = Path(source)
    trajectory = tmp_path / "baseline-shape-fixture.json"
    trajectory.write_text(json.dumps({"task_id": 0, "actions": [{"action_type": 0}] * 7}))
    case = load_official_interrupt(root, suite="1update", task_id="0",
        config_file=root / "Eval/config_files/wa/test_webarena_lite_transformed_1update/0.json",
        spec_file=root / "Eval/interrupt_config/process/interrupt_spec_1update_opus_02.json",
        trajectory_file=trajectory)
    w = world
    await w.harness.send(case.initial_intent)
    await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
    lifecycle = AgentLifecycleHandler(w.host, w.handler._blueprint, w.handler._infra, NS())
    with pytest.raises(ValueError, match="boundary"):
        await case.deliver(lifecycle, completed_actions=2, run_id="fixture")
    await case.deliver(lifecycle, completed_actions=1, run_id="fixture")
    await wait_until(lambda: w.harness.state is HarnessState.IDLE)
    recovered = json.dumps([c for c in w.endpoint.calls if c["model"] == "slow"][-1]["messages"])
    assert case.initial_intent in recovered and case.update in recovered
    assert "Impulse Duffle" not in recovered  # official evaluator answer stays private
    rounds = w.native._st.round_id_counter
    await case.deliver(lifecycle, completed_actions=1, run_id="fixture")
    assert w.native._st.round_id_counter == rounds
    assert w.tool.path.read_text() == "committed\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("policy,writes", [("model", 1), ("abort_restart", 2)])
async def test_benchmark_native_peer_executes_recovery_and_naive_restart(world, tmp_path, policy, writes):
    from jiuwenswarm.benchmarks.duplex_runtime import Events, NativePeer
    w = world
    w.endpoint.repeat_tool_without_result = True
    fast = w.host.tiny_agent_model_resolver("fast")
    slow = fast.model_copy(update={"model_request_config": fast.model_request_config.model_copy(
        update={"model_name": "slow"})})
    tool = WriteOnce(tmp_path / "peer-effects.txt")
    events = Events(tmp_path / "metrics.jsonl")
    peer = NativePeer(name="real-peer", models={"slow": slow, "fast": fast}, policy=policy,
        system_prompt="Implement the task using tools.", tools=[tool], events=events)
    await peer.start()
    try:
        await peer.send("Implement order events using Kafka.")
        await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
        await peer.receive("Customer forbids Kafka. Use PostgreSQL.", message_id="m1")
        result = await peer.wait(timeout=6)
        assert "PostgreSQL" in result["output"]
        assert tool.path.read_text() == "committed\n" * writes
        assert any(e["event"] == "model_end" and e["status"] == "cancelled" for e in events.records)
        assert any(e["event"] == "message_accepted" for e in events.records)
    finally:
        await peer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["steer", "model"])
async def test_three_database_peers_keep_distinct_listeners_and_deliver_without_waiting(world, tmp_path, policy):
    from jiuwenswarm.benchmarks.duplex_runtime import Events
    from jiuwenswarm.benchmarks.duplex_database_peer import DatabasePeer

    w = world
    w.endpoint.tool_first = False
    w.endpoint.block_model = False
    fast = w.host.tiny_agent_model_resolver("fast")
    slow = fast.model_copy(update={"model_request_config": fast.model_request_config.model_copy(
        update={"model_name": "slow"})})
    events = Events(tmp_path / "team-events.jsonl")
    peers = [DatabasePeer(database=tmp_path / "team.sqlite3", team="three_" + policy,
        name=name, models={"slow": slow, "fast": fast}, policy=policy,
        system_prompt=f"You are {name}.", tools=[], events=events)
        for name in ("implementer", "diagnoser", "reviewer")]
    try:
        for peer in peers:
            await peer.start()
        for i, peer in enumerate(peers):
            # Returns after DB write + enqueue, without waiting for the
            # receiver's Native round or any fast-model decision.
            await asyncio.wait_for(peer.send_to(peers[(i + 1) % 3].name,
                f"Finding from {peer.name}: requirement is Redis."), 3)
        await asyncio.wait_for(asyncio.gather(*(p.wait(timeout=6) for p in peers)), 7)
        await wait_until(lambda: all(not p._unacknowledged for p in peers))
        arrivals = [e for e in events.records if e["event"] == "message_arrived"]
        acks = [e for e in events.records if e["event"] == "message_accepted"]
        assert len(arrivals) == len(acks) == 3
        assert {e["member"] for e in arrivals} == {p.name for p in peers}
        assert all("phase" in e for e in arrivals)
        assert len([e for e in events.records if e["event"] == "delivery_effective"]) == 3
        assert len({e["message_id"] for e in acks}) == 3
    finally:
        for peer in reversed(peers):
            if peer.db is not None:
                await peer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["steer", "model"])
async def test_workload_team_runs_real_native_tools_and_natural_db_messages(world, tmp_path, policy):
    """Scripted models test the runner only; this is not an official task score."""
    from jiuwenswarm.benchmarks.duplex_runtime import Events
    from jiuwenswarm.benchmarks.duplex_workload_team import WorkloadTeam

    w = world
    names = ["implementer", "diagnoser", "reviewer"]
    w.endpoint.fast_action = "APPEND"

    def respond(body):
        prompt = json.dumps(body["messages"])
        member = next(n for n in names if f"Your name: {n}." in prompt)
        sent = any(m.get("role") == "assistant" and any(t["function"]["name"] == "SendMessage"
            for t in m.get("tool_calls", [])) for m in body["messages"])
        if not sent:
            return Endpoint.tool_message("SendMessage", {"recipient": names[(names.index(member) + 1) % 3],
                "content": f"Evidence from {member}: Redis is required."})
        return {"role": "assistant", "content": f"{member} finished its check."}

    w.endpoint.slow_responder = respond
    fast = w.host.tiny_agent_model_resolver("fast")
    slow = fast.model_copy(update={"model_request_config": fast.model_request_config.model_copy(
        update={"model_name": "slow"})})
    events = Events(tmp_path / "workload-events.jsonl")
    team = WorkloadTeam(suite="development", task="Use Redis for the queue.", models={"slow": slow, "fast": fast},
        policy=policy, environment=NS(instructions=""), output=tmp_path, events=events,
        timeout=15, max_model_calls=10)
    result = await asyncio.wait_for(team.run(), 20)
    assert result["status"] == "completed", result
    assert len([e for e in events.records if e["event"] == "message_sent"]) == 3
    assert len([e for e in events.records if e["event"] == "message_accepted"]) == 3
    assert {e["member"] for e in events.records if e["event"] == "agent_final"} == set(names)






@pytest.mark.asyncio
async def test_browser_native_adapter_returns_original_prompt_action(world, tmp_path):
    from jiuwenswarm.benchmarks.duplex_runtime import Events
    from jiuwenswarm.benchmarks.interruptbench_runner import NativeBrowserBridge
    w = world
    w.endpoint.tool_first = False
    w.endpoint.block_model = False
    fast = w.host.tiny_agent_model_resolver("fast")
    slow = fast.model_copy(update={"model_request_config": fast.model_request_config.model_copy(
        update={"model_name": "slow"})})
    events = Events(tmp_path / "browser-metrics.jsonl")
    bridge = NativeBrowserBridge(models={"slow": slow, "fast": fast}, policy="model",
        events=events, official_records=[])
    prompt = [{"role": "system", "content": "Keep official action syntax."},
              {"role": "user", "content": "Original browser observation."}]
    after = [*prompt, {"role": "user", "content": "Official updated goal."}]
    current = {"after_prompt": after, "pending": {"before": "Original goal", "after": "Updated goal",
        "update": "Official updated goal.", "message_id": "u-1", "delivered": False}}
    try:
        answer = await bridge._predict(NS(model="slow", gen_config={}), prompt, current)
        assert "PostgreSQL" in answer
        requests = [r for r in w.endpoint.calls if r["model"] == "slow"]
        assert requests[-1]["messages"] == after
        assert not requests[-1].get("tools")
        original_peer = bridge._peer
        second_prompt = [*after, {"role": "user", "content": "Next browser observation."}]
        await bridge._predict(NS(model="slow", gen_config={}), second_prompt,
                              {"intent": "Updated goal", "pending": None})
        assert bridge._peer is original_peer
        assert bridge._peer.harness._st.round_id_counter >= 2
        assert [r for r in w.endpoint.calls if r["model"] == "slow"][-1]["messages"] == second_prompt
    finally:
        await bridge.close_native()



@pytest.mark.asyncio
async def test_browser_worker_process_uses_native_http_and_clean_stdio(world, tmp_path):
    from jiuwenswarm.benchmarks.interruptbench_runner import ProcessWorker
    w = world
    w.endpoint.tool_first = False
    w.endpoint.block_model = False
    fast = w.host.tiny_agent_model_resolver("fast")
    slow = fast.model_copy(update={"model_request_config": fast.model_request_config.model_copy(
        update={"model_name": "slow"})})
    models = tmp_path / "models.json"
    models.write_text(json.dumps({"slow": slow.model_dump(), "fast": fast.model_dump()}))
    worker = ProcessWorker(Path(sys.executable), models, "model", tmp_path / "process.jsonl")
    try:
        prompt = [{"role": "user", "content": "Use the official browser action format."}]
        answer = await asyncio.wait_for(asyncio.to_thread(worker.predict,
            NS(model="slow", gen_config={}), prompt, None), 45)
        assert "PostgreSQL" in answer
        assert [r for r in w.endpoint.calls if r["model"] == "slow"][-1]["messages"] == prompt
    finally:
        await asyncio.to_thread(worker.close)


@pytest.mark.asyncio
async def test_background_bash_completion_reaches_live_native_peer(world, tmp_path):
    from jiuwenswarm.benchmarks.agentradio_peer import BashTool
    from jiuwenswarm.benchmarks.duplex_runtime import Events, NativePeer
    import shutil
    shell = shutil.which("bash") if os.name != "nt" else "C:/Program Files/Git/bin/bash.exe"
    if not shell or not Path(shell).exists():
        pytest.skip("Bash is required for the official AgentRadio shell adapter")
    w = world
    fast = w.host.tiny_agent_model_resolver("fast")
    slow = fast.model_copy(update={"model_request_config": fast.model_request_config.model_copy(
        update={"model_name": "slow"})})
    events = Events(tmp_path / "background.jsonl")
    bash = BashTool(tmp_path, events, shell=shell)
    peer = NativePeer(name="background-peer", models={"slow": slow, "fast": fast}, policy="model",
        system_prompt="Implement the task.", tools=[WriteOnce(tmp_path / "bg-effects.txt"), bash], events=events)
    bash.peer = peer
    await peer.start()
    try:
        await peer.send("Implement order events using Kafka.")
        await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
        result = await bash.invoke({"command": "printf 'Customer forbids Kafka. Use PostgreSQL.'", "run_in_background": True})
        assert "started" in result
        await asyncio.wait_for(asyncio.gather(*bash.jobs.values()), 6)
        assert "PostgreSQL" in (await peer.wait(timeout=6))["output"]
        slow_calls = [r for r in w.endpoint.calls if r["model"] == "slow"]
        assert "Customer forbids Kafka" in json.dumps(slow_calls[-1]["messages"])
    finally:
        await bash.close()
        await peer.close()


@pytest.mark.asyncio
async def test_original_browser_prompt_and_action_parser_across_environments(world, tmp_path):
    """Real upstream PromptAgent -> separate Native process -> original parser."""
    python = os.environ.get("JIUWEN_INTERRUPT_PYTHON")
    root = os.environ.get("JIUWEN_INTERRUPT_BENCH_ROOT")
    if not python or not root:
        pytest.skip("separate official WebArena environment required")
    w = world
    w.endpoint.tool_first = False
    w.endpoint.block_model = False
    w.endpoint.final_content = "```stop [done]```"
    fast = w.host.tiny_agent_model_resolver("fast")
    slow = fast.model_copy(update={"model_request_config": fast.model_request_config.model_copy(
        update={"model_name": "slow"})})
    models = tmp_path / "models.json"
    models.write_text(json.dumps({"slow": slow.model_dump(), "fast": fast.model_dump()}))
    script = tmp_path / "official-agent.py"
    script.write_text('''import json,os,sys
from pathlib import Path
root,models,native,metrics=sys.argv[1:]
os.chdir(Path(root)/"Eval")
sys.path.insert(0,str(Path(root)/"Eval"))
import agent.agent as module
import run as upstream
from browser_env.utils import DetachedPage
from jiuwenswarm.benchmarks.duplex_metrics import Events
from jiuwenswarm.benchmarks.interruptbench_runner import NativeBrowserBridge
sys.argv=["run.py","--provider","openai","--model","slow","--mode","chat",
"--instruction_path","agent/prompts/jsons/p_cot_id_actree_2s.json",
"--action_set_tag","id_accessibility_tree","--observation_type","accessibility_tree",
"--max_obs_length","0"]
args=upstream.config()
bridge=NativeBrowserBridge(models=None,policy="model",events=Events(Path(metrics)),
official_records=[],native_python=Path(native),models_file=Path(models))
restore=bridge.install(module)
try:
    agent=module.construct_agent(args)
    trajectory=[{"observation":{"text":"A fixture page"},"info":{"page":DetachedPage("http://127.0.0.1:1","")}}]
    action=agent.next_action(trajectory,"Finish the fixture task.",meta_data={"action_history":["None"]})
    assert int(action["action_type"])==17,action
    assert action["answer"]=="done",action
    print("OFFICIAL_ACTION_OK")
finally:
    restore()
''', encoding="utf-8")
    env = {**os.environ, "DATASET": "webarena", "OPENAI_API_KEY": "local-test",
           "OPENAI_API_URL": "http://127.0.0.1:1"}
    env.update({name: "http://127.0.0.1:1" for name in
        ("REDDIT", "SHOPPING", "SHOPPING_ADMIN", "GITLAB", "WIKIPEDIA", "MAP", "HOMEPAGE")})
    process = await asyncio.create_subprocess_exec(python, str(script), root, str(models), sys.executable,
        str(tmp_path / "official.jsonl"), env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 90)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 0, output.decode(errors="replace")
    assert b"OFFICIAL_ACTION_OK" in output


@pytest.mark.asyncio
async def test_real_event_bus_wakes_receiver_and_broadcast_watermark_advances(world):
    from openjiuwen.agent_teams.agent.coordination.event_bus import EventBus
    from openjiuwen.agent_teams.context import get_session_id
    from openjiuwen.agent_teams.schema.events import TeamTopic
    w = world
    bus = EventBus(role=TeamRole.LEADER)
    w.handler._poll = bus
    topic = TeamTopic.MESSAGE.build(get_session_id(), "duplex")
    handled = asyncio.Event()

    async def wake(event):
        await w.handler.on_message_or_broadcast(event)
        handled.set()

    await bus.start(wake_callback=wake)
    await w.messager.subscribe(topic, bus.enqueue)
    try:
        await w.harness.send("Implement the order system.")
        await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
        await w.manager.broadcast_message(content="Customer forbids Kafka. Use PostgreSQL.")
        await asyncio.wait_for(handled.wait(), 6)
        await wait_until(lambda: w.harness.state is HarnessState.IDLE)
        assert not await w.manager.get_broadcast_messages(member_name="A2", unread_only=True)
        assert w.native._st.round_id_counter == 2
        assert w.tool.path.read_text() == "committed\n"
    finally:
        await w.messager.unsubscribe(topic)
        await bus.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("world,group,use_steer", [
    ({"mode": "off"}, "G0-serial", False),
    ({"mode": "off"}, "G1-current-steer", True),
    ({"policy": "always_interrupt"}, "G2-always-interrupt", True),
    ({"policy": "model"}, "G3-model-router", True),
], indirect=["world"])
async def test_four_strategies_at_same_tool_boundary(world, group, use_steer, record_property):
    w = world
    w.endpoint.block_model = False
    w.tool.gate.clear()
    await w.harness.send("Implement the order system.")
    await asyncio.wait_for(w.tool.entered.wait(), 6)
    mid = await send_message(w)
    drain = asyncio.create_task(w.handler._process_unread_messages("A2", use_steer=use_steer))
    if group.startswith(("G2", "G3")):
        await wait_until(lambda: w.harness.state is HarnessState.PAUSING)
    else:
        await asyncio.wait_for(drain, 3)
    w.tool.gate.set()
    await asyncio.wait_for(drain, 6)
    await wait_until(lambda: w.harness.state is HarnessState.IDLE)
    slow = [c for c in w.endpoint.calls if c["model"] == "slow"]
    assert mid in json.dumps(slow[-1]["messages"])
    assert w.tool.path.read_text() == "committed\n"
    assert not await w.manager.get_messages(to_member_name="A2", unread_only=True)
    record_property("strategy", group)
    record_property("slow_requests", len(slow))
    record_property("fast_requests", sum(c["model"] == "fast" for c in w.endpoint.calls))
    assert len(slow) == (3 if group == "G0-serial" else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("world", [{"mode": "shadow"}], indirect=True)
async def test_inactive_mode_uses_original_native_without_fast_call(world):
    w = world
    assert not isinstance(w.native, DuplexNativeHarness)
    await w.harness.send("Implement the order system.")
    await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
    await send_message(w)
    await poll_and_apply(w)
    assert not any(c["model"] == "fast" for c in w.endpoint.calls)
    assert w.harness.state is HarnessState.RUNNING
    assert w.native.active_round.round_id == 1
    assert not any(t["function"]["name"] == "update_working_intent"
                   for t in w.endpoint.calls[0]["tools"])


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["ordinary", "append", "timeout", "error", "missing_model", "serial"])
async def test_slow_requests_match_original_sdk_when_not_interrupting(world, tmp_path, scenario):
    """Compare actual HTTP messages/tools/round counts, not just final answers."""
    from jiuwenswarm.benchmarks.duplex_runtime import Events, NativePeer
    from jiuwenswarm.agents.harness.team.duplex_shadow import RoutedInput, deliver_routed
    from jiuwenswarm.common.duplex_router import InboundMessage

    w = world
    fast = w.host.tiny_agent_model_resolver("fast")
    slow = fast.model_copy(update={"model_request_config": fast.model_request_config.model_copy(
        update={"model_name": "slow"})})
    observed = []
    for policy in ("steer", "model"):
        w.endpoint.calls.clear()
        w.endpoint.block_model = False
        w.endpoint.fast_action = "APPEND"
        w.endpoint.fast_error = scenario == "error"
        w.endpoint.fast_gate.set()
        if scenario == "timeout":
            w.endpoint.fast_gate.clear()
        tool = WriteOnce(tmp_path / f"{policy}-effects.txt")
        if scenario != "ordinary":
            tool.gate.clear()
        peer = NativePeer(name="parity", models={"slow": slow, "fast": fast}, policy=policy,
            system_prompt="Implement the task.", tools=[tool],
            events=Events(tmp_path / f"{policy}.jsonl"))
        await peer.start()
        try:
            await peer.send("Implement order events.")
            if scenario != "ordinary":
                await asyncio.wait_for(tool.entered.wait(), 6)
                content = "Also include migration steps."
                if policy == "model":
                    await deliver_routed(peer, RoutedInput(content, InboundMessage("m1", "user", content)),
                        use_steer=True, original=peer._original,
                        settings={"mode": "active", "model_name": "" if scenario == "missing_model" else "fast",
                                  "policy": "serial" if scenario == "serial" else "model",
                                  "timeout_seconds": 0.05 if scenario == "timeout" else 2})
                else:
                    await peer.harness.send(content, immediate=scenario != "serial")
                tool.gate.set()
            await peer.wait(timeout=6)
            requests = [c for c in w.endpoint.calls if c["model"] == "slow"]
            # The scripted server generates random tool call IDs. Normalize
            # only those identities, preserving all prompts and tool arguments.
            identities = {}
            for request in requests:
                for message in request["messages"]:
                    for call in message.get("tool_calls", []):
                        identities.setdefault(call["id"], f"call-{len(identities)}")
            payload = json.dumps([{"messages": c["messages"], "tools": c.get("tools")} for c in requests],
                                 sort_keys=True)
            for key, value in identities.items():
                payload = payload.replace(key, value)
            observed.append((payload, peer.harness._st.round_id_counter))
            assert tool.path.read_text() == "committed\n"
            assert "update_working_intent" not in payload
        finally:
            tool.gate.set()
            await peer.close()
    assert observed[1] == observed[0]

@pytest.mark.asyncio
async def test_live_verifier_forces_one_structured_request(world):
    from jiuwenswarm.benchmarks.duplex_live_review import verify_finding

    fast = world.host.tiny_agent_model_resolver('fast')
    slow = fast.model_copy(update={'model_request_config': fast.model_request_config.model_copy(
        update={'model_name': 'slow'})})
    original_config = slow.model_request_config.model_dump()
    world.endpoint.calls.clear()

    def respond(body):
        assert body['tool_choice'] == {'type': 'function', 'function': {'name': 'structured_output'}}
        return Endpoint.tool_message('structured_output', {
            'supported': True, 'evidence': "queue = 'Kafka'",
            'correction': 'Use Redis.', 'reason': 'The user required Redis.'})

    world.endpoint.slow_responder = respond
    result = await verify_finding(slow, {'task': 'Use Redis.', 'command': "queue = 'Kafka'"},
        {'evidence': "The command uses `queue = 'Kafka'`, which violates the requirement.",
         'explanation': 'Wrong queue backend.', 'correction': 'Use Redis.'})
    assert result['status'] == 'problem'
    assert len([c for c in world.endpoint.calls if c['model'] == 'slow']) == 1
    assert slow.model_request_config.model_dump() == original_config


@pytest.mark.asyncio
@pytest.mark.parametrize('policy', ['steer', 'model'])
@pytest.mark.parametrize('repair', [False, True])
async def test_final_verification_ends_without_extra_model_call(world, tmp_path, monkeypatch, policy, repair):
    from jiuwenswarm.benchmarks.duplex_metrics import Events
    from jiuwenswarm.benchmarks.duplex_workload_team import WorkloadTeam
    from jiuwenswarm.benchmarks import duplex_live_review

    artifact = tmp_path / 'output.txt'

    class Environment:
        live_review = True
        owner = 'implementer'
        roles = {'implementer': 'Save and verify output.', 'reviewer': 'Monitor.'}
        instructions = ''

        async def execute(self, member, command, seconds):
            if command.startswith('save '):
                artifact.write_text(command[5:])
            if command == 'check' and artifact.read_text() != 'correct':
                return 'exit_code=1\nAssertionError: wrong output'
            return 'exit_code=0\nchecks passed'

        async def review_evidence(self, command, output):
            return {'revision': artifact.read_text(), 'command': command, 'tool_output': output}

        async def submission_state(self):
            return {'ready': artifact.exists(), 'reason': 'Missing output',
                    'revision': artifact.read_text() if artifact.exists() else ''}

    async def review(model, evidence, **kwargs):
        return {'status': 'ok', 'evidence': '', 'correction': ''}

    monkeypatch.setattr(duplex_live_review, 'model_review', review)
    requests = []

    def respond(body):
        requests.append(body)
        step = len(requests)
        assert 'Runtime budget:' not in json.dumps(body['messages'])
        if step == 1:
            return Endpoint.tool_message('Bash', {'command': 'save wrong' if repair else 'save correct'})
        if repair and step == 3:
            return Endpoint.tool_message('Bash', {'command': 'save correct'})
        assert step == 2 or (repair and step == 4), 'Unnecessary model call after final verification'
        return Endpoint.tool_message('VerifyAndFinish', {'command': 'check', 'summary': 'Saved and checked.'})

    world.endpoint.slow_responder = respond
    fast = world.host.tiny_agent_model_resolver('fast')
    slow = fast.model_copy(update={'model_request_config': fast.model_request_config.model_copy(
        update={'model_name': 'slow'})})
    events = Events(tmp_path / ('submit-' + policy + '.jsonl'))
    team = WorkloadTeam(suite='development', task='Save correct output.', models={'slow': slow, 'fast': fast},
                        policy=policy, environment=Environment(), output=tmp_path / policy,
                        events=events, timeout=12, max_model_calls=6)
    result = await team.run()
    assert result['status'] == 'completed', result
    assert len(requests) == (4 if repair else 2)
    assert sum(e['event'] == 'submission_accepted' for e in events.records) == 1
    assert sum(e['event'] == 'submission_rejected' for e in events.records) == int(repair)
    starts = [e for e in events.records if e['event'] == 'model_start']
    assert all(len(e['semantic_request_sha256']) == 64 for e in starts)
    assert all(e['first_chunk_seconds'] is not None and e['first_output_seconds'] is not None
               for e in events.records if e['event'] == 'model_end')


@pytest.mark.asyncio
@pytest.mark.parametrize('policy', ['steer', 'model'])
async def test_live_monitor_automatically_routes_real_artifact_correction(world, tmp_path, monkeypatch, policy):
    from jiuwenswarm.benchmarks.duplex_metrics import Events
    from jiuwenswarm.benchmarks.duplex_workload_team import WorkloadTeam
    from jiuwenswarm.benchmarks import duplex_live_review

    w = world
    second_model = asyncio.Event()
    w.endpoint.block_stream_call = 2
    w.endpoint.model_gate.clear()
    artifact = tmp_path / 'queue.txt'

    class Environment:
        live_review = True
        owner = 'implementer'
        roles = {'implementer': 'Write the requested queue configuration.', 'reviewer': 'Monitor.'}
        instructions = ''

        async def execute(self, member, command, seconds):
            artifact.write_text('Redis' if 'Redis' in command else 'Kafka')
            return 'exit_code=0\nqueue=' + artifact.read_text()

        async def review_evidence(self, command, output):
            return {'revision': artifact.read_text(), 'command': command, 'tool_output': output}

    async def review(model, evidence, *, verification_model):
        assert verification_model.model_request_config.model_name == 'slow'
        if evidence['revision'] == 'Kafka':
            await second_model.wait()
            return {'status': 'problem', 'evidence': evidence['tool_output'],
                    'correction': 'The actual Kafka configuration violates the user requirement. Use Redis.'}
        return {'status': 'ok', 'evidence': '', 'correction': ''}

    monkeypatch.setattr(duplex_live_review, 'model_review', review)

    def respond(body):
        assert all(tool["function"]["name"] != "SendMessage" for tool in body.get("tools", []))
        assert "A passive reviewer" in json.dumps(body["messages"])
        prompt = json.dumps(body['messages'])
        if 'queue=Redis' in prompt:
            return {'role': 'assistant', 'content': 'Redis configuration verified.'}
        if 'Observed problem at artifact' in prompt:
            return Endpoint.tool_message('Bash', {'command': 'write Redis'})
        if 'queue=Kafka' in prompt:
            second_model.set()
            return {'role': 'assistant', 'content': 'Continue obsolete Kafka plan.'}
        return Endpoint.tool_message('Bash', {'command': 'write Kafka'})

    w.endpoint.slow_responder = respond
    fast = w.host.tiny_agent_model_resolver('fast')
    slow = fast.model_copy(update={'model_request_config': fast.model_request_config.model_copy(
        update={'model_name': 'slow'})})
    events = Events(tmp_path / ('live-' + policy + '.jsonl'))
    team = WorkloadTeam(suite='development', task='Use Redis for the queue.',
        models={'slow': slow, 'fast': fast}, policy=policy, environment=Environment(),
        output=tmp_path / policy, events=events, timeout=15, max_model_calls=8)
    job = asyncio.create_task(team.run())
    try:
        await wait_until(lambda: any(e['event'] == 'delivery_effective' for e in events.records))
        if policy == 'steer':
            w.endpoint.model_gate.set()
        result = await asyncio.wait_for(job, 18)
        assert result['status'] == 'completed', result
        assert artifact.read_text() == 'Redis'
        deliveries = [e for e in events.records if e['event'] == 'delivery_effective']
        assert sum(e['action'] == 'INTERRUPT' for e in deliveries) == (1 if policy == 'model' else 0)
        assert not any(e['event'] == 'model_start' and e.get('member') == 'reviewer' for e in events.records)
        if policy == 'model':
            assert any(e['event'] == 'route_input' and 'write Kafka' in e['last_action']
                       and 'write Kafka' not in e['next_action'] for e in events.records)
    finally:
        w.endpoint.model_gate.set()
        if not job.done():
            job.cancel()
        await asyncio.gather(job, return_exceptions=True)
