"""Real SQLite -> SDK mailbox -> TinyAgent HTTP -> Native/ReAct/tools -> ACK.

Only model responses are scripted. No dispatcher, harness, context, tool
executor, fast-model adapter, or message-manager method is mocked.
"""
from __future__ import annotations

import asyncio
import json
import uuid
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
        self.jev_calls = []
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

    async def handle_jev(self, request):
        self.jev_calls.append(await request.json())
        self.fast_entered.set()
        await self.fast_gate.wait()
        return web.json_response({"answers": {"action": {
            "type": "choice", "choice": self.fast_action, "confidence": 0.95,
            "probabilities": {"INTERRUPT": 0.95, "APPEND": 0.05},
        }}})

    async def handle(self, request):
        body = await request.json()
        # SDK capability probes are setup traffic, not task model turns.
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
    app.router.add_post("/v1/systemone", endpoint.handle_jev)
    server = web.AppRunner(app, shutdown_timeout=0.1)
    await server.setup()
    site = web.TCPSite(server, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    if settings["duplex_router"].get("backend") == "jev":
        settings["duplex_router"]["jev"] = {"api_base": f"http://127.0.0.1:{port}/v1"}
        monkeypatch.setenv("TYPESAFE_API_KEY", "local-jev-test")
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
        await asyncio.wait_for(harness.dispose(), 10)
        await asyncio.wait_for(collector, 3)
        await db.close()
        reset_session_id(token)
        await asyncio.wait_for(Runner.stop(), 10)
        await asyncio.wait_for(server.cleanup(), 10)


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
@pytest.mark.parametrize("world", [{"mode": "off"}], indirect=True)
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
@pytest.mark.parametrize("world", [{"backend": "sdk"}, {"backend": "jev"}], indirect=True)
async def test_pending_decision_survives_new_input_and_preserves_both_messages(world):
    """A state hash change must not discard a supervisor decision or either input."""
    w = world
    w.endpoint.fast_gate.clear()
    await w.harness.send("Implement the order event system with Kafka.")
    await asyncio.wait_for(w.endpoint.model_entered.wait(), 6)
    before = snapshot_from_native(w.native)
    mid = await send_message(w)
    delivery = asyncio.create_task(poll_and_apply(w))
    try:
        await asyncio.wait_for(w.endpoint.fast_entered.wait(), 6)
        assert snapshot_from_native(w.native) == before
        await w.harness.send("Also include migration steps.", immediate=True)
        assert snapshot_from_native(w.native).context_version != before.context_version
        w.endpoint.fast_gate.set()
        await asyncio.wait_for(delivery, 6)
        await wait_until(lambda: w.harness.state is HarnessState.IDLE)
        assert w.tool.path.read_text() == "committed\n"
        assert not await w.manager.get_messages(to_member_name="A2", unread_only=True)
        recovered = json.dumps([c for c in w.endpoint.calls if c["model"] == "slow"][-1]["messages"])
        assert "Also include migration steps." in recovered
        assert recovered.count(mid) == 1
        assert w.native._st.round_id_counter > int(before.round_id)
        requests = (w.endpoint.jev_calls if w.settings["duplex_router"]["backend"] == "jev"
                    else [c for c in w.endpoint.calls if c["model"] == "fast"])
        assert len(requests) == 1
        assert mid in json.dumps(requests[0])
    finally:
        w.endpoint.fast_gate.set()
        await asyncio.wait_for(delivery, 6)
