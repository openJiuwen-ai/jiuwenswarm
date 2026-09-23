"""Real Core interruption and Host answer conversion; only model/transport are local."""

import asyncio
import json
from types import SimpleNamespace

from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.single_agent.agents.react_agent import ReActAgent, ReActAgentConfig
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.deep_agent import DeepAgent

from jiuwenswarm.agents.harness.common.rails.ask_user_rail import StructuredAskUserRail
from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    convert_interactions_to_ask_user_question,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions.video_duplex.backend.task_adapter import AgentTaskExecutor
from jiuwenswarm.extensions.video_duplex.backend.tasks import TaskService, TaskStore
from jiuwenswarm.extensions.video_duplex.backend.tasks.service import TaskModification
from jiuwenswarm.extensions.video_duplex.backend.tasks.execution import bind_task_execution
from jiuwenswarm.extensions.video_duplex.backend.tasks.rail import VoiceAgentTaskRail
from jiuwenswarm.extensions.video_duplex.tests.backend.task_bridge_support import install_bridge
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm


class CoreInputNormalizer(DeepAgent):
    """Expose Core's inherited normalization for this local transport test."""

    def normalize(self, inputs):
        return self._normalize_inputs(inputs)


async def wait_for(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.005)


async def test_native_answer_and_linked_revision_reach_actual_model_inputs(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter import interface

    store = TaskStore(tmp_path / "native.sqlite")
    install_bridge(monkeypatch)
    monkeypatch.setattr(interface, "get_config", lambda: {})
    await Runner.start()
    agent = ReActAgent(AgentCard(id="voice-native-resume", name="voice-native-resume"))
    agent.configure(ReActAgentConfig().configure_model("local-test-model"))
    await agent.register_rail(StructuredAskUserRail())
    calls, model_inputs = [], []

    async def invoke_model(*_args, **kwargs):
        model_inputs.append(kwargs["messages"])
        if len(model_inputs) == 1:
            return AssistantMessage(
                content="",
                tool_calls=[
                    ToolCall(
                        id="budget-call",
                        type="function",
                        name="ask_user",
                        arguments=json.dumps({"questions": [{"question": "预算是多少？"}]}),
                    )
                ],
            )
        assert "5000" in str(kwargs["messages"]), "Actual resumed model input must contain the user's answer"
        return AssistantMessage(content="预算66元" if "改为66元" in str(kwargs["messages"]) else "预算5000元")

    monkeypatch.setattr(agent, "_get_llm", lambda: SimpleNamespace(invoke=invoke_model))
    host = object.__new__(JiuWenSwarm)
    # Input normalization is stateless; no model/runtime setup is needed here.
    normalizer = object.__new__(CoreInputNormalizer)
    sessions = {}
    rail = VoiceAgentTaskRail()
    await agent.register_rail(rail)

    class Binding:
        _is_session_scoped_adapter = True
        task_execution_binding = (SimpleNamespace(_react_agent=agent), rail)

    class LocalAgentTransport:
        async def send_request(self, env):
            return SimpleNamespace(ok=True, payload={"files": []})

        async def send_request_stream(self, env):
            calls.append(env)
            request = AgentRequest(
                request_id=env.request_id,
                channel_id="video_tool",
                session_id=env.session_id,
                req_method=ReqMethod.CHAT_SEND,
                params=env.params,
                user_id="user",
            )
            inputs, _, _ = host.build_inputs(request)
            if env.session_id not in sessions:
                sessions[env.session_id] = create_agent_session(session_id=env.session_id, card=agent.card)
                await sessions[env.session_id].pre_run(inputs=inputs)
            session = sessions[env.session_id]
            async with bind_task_execution(request, Binding(), inputs):
                normalized = normalizer.normalize(inputs)
                result = await agent.invoke(
                    {**inputs, "run_context": normalized.run_context}, session=session
                )
            if result.get("result_type") == "interrupt":
                question = convert_interactions_to_ask_user_question(
                    [item.payload for item in result["state"]]
                )
                assert question["source"] == "ask_user_interrupt"
                yield SimpleNamespace(payload=question)
            else:
                yield SimpleNamespace(payload={"event_type": "chat.final", "content": result["output"]})

    executor = AgentTaskExecutor(LocalAgentTransport(), store, None)
    service = TaskService(store, executor, concurrency=1)
    try:
        task = service.submit("user", "voice", "create", "请先问预算", {"independent": True})
        await wait_for(lambda: store.read(task["id"])["output_closed"])
        waiting = store.read(task["id"])
        assert waiting["status"] == "waiting_user", waiting["error"]
        assert waiting["execution_settled"]
        await service.answer(
            "user",
            "voice",
            task["id"],
            "answer",
            waiting["interaction"]["id"],
            answers=["5000"],
        )
        await wait_for(lambda: store.read(task["id"])["status"] == "completed")
        final = store.read(task["id"])
        assert final["result"]["answer"] == "预算5000元"
        assert final["execution_settled"] and final["output_closed"]
        assert len(calls) == len(model_inputs) == 2
        assert calls[0].session_id == calls[1].session_id == task["core_session_id"]
        assert calls[0].request_id != calls[1].request_id
        assert len(service.snapshot("user", "voice")[0]) == 1
        revision = service.modify(
            "user", "voice", task["id"], "revise", TaskModification(final["revision"], "改为66元")
        )
        child_id = revision["successor_id"]
        await wait_for(lambda: store.read(child_id)["status"] == "completed")
        child = store.read(child_id)
        assert child["parent_id"] == task["id"]
        assert child["result"]["answer"] == "预算66元"
        assert store.read(task["id"])["result"] == final["result"]
        assert calls[-1].session_id != task["core_session_id"]
    finally:
        await service.close()
        await Runner.stop()
