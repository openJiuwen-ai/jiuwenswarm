# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exercise Symphony dispatch through the single-agent model and tool loop."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from openjiuwen.core.foundation.llm import (
    AssistantMessage,
    AssistantMessageChunk,
    ModelClientConfig,
    ModelRequestConfig,
    ToolCall,
    ToolMessage,
    UsageMetadata,
    UserMessage,
)
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.harness import create_deep_agent
from openjiuwen.harness.rails import AskUserRail, SkillUseRail

from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail
from jiuwenswarm.agents.harness.common.rails.symphony import task_tool as task_tool_module
from jiuwenswarm.agents.harness.common.rails.symphony.expert_task_plan import ExpertTaskPlan
from jiuwenswarm.agents.harness.common.tools.symphony_toolkits import SymphonyToolkit
from jiuwenswarm.agents.harness.team.experts import catalog
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


_CONFIG = {"symphony": {"orchestration": {"enabled": True}}, "react": {"subagent_runtime": {"enabled": True}}}


def _reply(content="", *calls):
    return AssistantMessage(
        content=content,
        tool_calls=[ToolCall(id=call_id, type="function", name=name, arguments=json.dumps(args))
                    for call_id, name, args in calls],
        usage_metadata=UsageMetadata(model_name="symphony-test"),
    )


class _Model:
    model_config = ModelRequestConfig(model_name="symphony-test")
    model_client_config = ModelClientConfig(client_provider="OpenAI", api_key="test", api_base="http://test.invalid")

    def __init__(self, reply):
        self.reply = reply

    async def invoke(self, messages, *, tools=None, **kwargs):
        return await self.reply(messages, tools or [])

    async def stream(self, messages, **kwargs):
        result = await self.invoke(messages, **kwargs)
        yield AssistantMessageChunk(
            content=result.content,
            tool_calls=result.tool_calls,
            finish_reason="tool_calls" if result.tool_calls else "stop",
            usage_metadata=result.usage_metadata,
        )


def _node(task, output, *, expert=None, input_name=None):
    identity = {"type": "expert", "expert_id": expert} if expert else {
        "type": "inline", "role": {"name": task, "description": f"Role for {task}"},
    }
    return {"label": task, "metadata": {
        **identity,
        "task": task,
        "inputs": [{"name": input_name, "type": "text"}] if input_name else [],
        "outputs": [{"name": output, "type": "text"}],
    }}


def _plan(nodes, edges=()):
    return {"success": True, "planned_graph": {"graph": {
        "id": "test-plan", "type": "planned_graph", "directed": True,
        "metadata": {"capability_type": "expert", "plan_kind": "expert_tasks", "status": "ready"},
        "nodes": nodes,
        "edges": [
            {"source": source, "target": target, "relation": "can_feed",
             "source_output": output, "target_input": input_name}
            for source, target, output, input_name in edges
        ],
    }}}


def _task_call(node, *, call_id=None, plan_id="test-plan"):
    return call_id or f"dispatch-{node}", "task_tool", {"plan_id": plan_id, "node_id": node}


def _install_experts(tmp_path: Path, monkeypatch) -> dict[str, Path]:
    local = tmp_path / "installed"
    marketplace = tmp_path / "marketplace.json"
    monkeypatch.setattr(catalog, "expert_package_roots", lambda _kind: (marketplace, local, tmp_path / "builtin"))
    receipts = {}
    for expert in ("researcher", "reporter"):
        package = local / f"package-{expert}"
        (package / "persona").mkdir(parents=True)
        (package / "persona" / "identity.md").write_text(f"Persona from {expert} template", encoding="utf-8")
        skill = package / "skills" / f"{expert}-method"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {expert}-method\ndescription: The expert method\n---\n\nMethod for {expert}\n",
            encoding="utf-8",
        )
        receipt = tmp_path / f"{expert}.txt"
        receipts[expert] = receipt
        (package / "manifest.json").write_text(json.dumps({
            "package_type": "agent_template", "name": expert, "description": "Test expert",
            "persona": {"dir": "persona"}, "tools": [{"file": "receipt.py", "class": "Receipt"}],
            "skills": [{"dir": f"skills/{expert}-method"}],
        }), encoding="utf-8")
        (package / "receipt.py").write_text(
            "from pathlib import Path\n"
            "from openjiuwen.core.foundation.tool import LocalFunction, ToolCard\n\n"
            "class Receipt(LocalFunction):\n"
            "    def __init__(self):\n"
            "        def write():\n"
            f"            Path({str(receipt)!r}).write_text('template tool ran', encoding='utf-8')\n"
            f"            return {expert!r}\n"
            "        super().__init__(ToolCard(name='expert_receipt', description='Write a receipt'), write)\n",
            encoding="utf-8",
        )
    marketplace.write_text(json.dumps({"plugins": [
        {"id": f"package-{expert}", "installed": True} for expert in receipts
    ]}), encoding="utf-8")
    return receipts


def _chat_agent(tmp_path, monkeypatch, plan, reply, *, extra_rails=(), persistent=True):
    async def compose(query, **kwargs):
        assert query and kwargs["capability_type"] == "expert"
        return plan(query) if callable(plan) else plan

    adapter = JiuWenSwarmDeepAdapter()
    config = {**_CONFIG, "react": {"subagent_runtime": {"enabled": persistent}}}
    adapter._config_base_cache = config
    toolkit = SymphonyToolkit(service=SimpleNamespace(plan=compose), llm_config_provider=lambda: None)
    monkeypatch.setattr(toolkit, "is_enabled", lambda config=None: True)
    compose_tool = next(tool for tool in toolkit.get_tools(config) if tool.card.name == "symphony_compose_graph")
    assert set(compose_tool.card.input_params["properties"]) == {
        "query", "type", "mode", "candidate_ids", "candidate_skill_ids",
    }
    subagent_rail = adapter._build_subagent_rail(config)
    skill_rail = SkillUseRail(skills_dir=str(tmp_path / "skills"), include_tools=False, skill_mode="all")
    return create_deep_agent(
        _Model(reply),
        tools=[compose_tool],
        rails=[subagent_rail, adapter._build_symphony_orchestration_rail(), JiuSwarmStreamEventRail(),
               skill_rail, *extra_rails],
        workspace=str(tmp_path),
        enable_subagent_runtime=persistent,
        add_general_purpose_agent=True,
        enable_read_image_multimodal=False,
        auto_create_workspace=False,
        max_iterations=12,
    )


def _chat_input(query):
    return {
        "query": query,
        "conversation_id": f"symphony-dispatch-{uuid4().hex}",
        "run": {"context": {"extra": {"capture_mode": "agent", "member_id": "root"}}},
    }


def _tool_results(messages):
    return {message.tool_call_id: message.content for message in messages if isinstance(message, ToolMessage)}


@pytest.mark.asyncio
@pytest.mark.parametrize("execute", [True, False])
async def test_chat_returns_dag_then_delegates_native_and_inline_nodes(tmp_path, monkeypatch, execute):
    receipts = _install_experts(tmp_path, monkeypatch)
    graph = _plan({
        "task-1": _node("research", "findings", expert="researcher"),
        "task-2": _node("organize", "notes"),
        "task-3": _node("verify", "verified", input_name="evidence"),
        "task-4": _node("report", "report", expert="reporter", input_name="verified"),
    }, [
        ("task-1", "task-3", "FINDINGS", "Evidence"),
        ("task-2", "task-3", "notes", "evidence"),
        ("task-3", "task-4", "verified", "verified"),
    ])
    both_started = asyncio.Event()
    started = set()
    root_round = 0
    events = []
    query = "Research and write the report" if execute else "Show only the assignment graph"

    async def reply(messages, tools):
        nonlocal root_round
        user_input = next(msg.content for msg in reversed(messages) if isinstance(msg, UserMessage))
        results = _tool_results(messages)
        if user_input.startswith("Complete the assigned task"):
            system = str(messages[0].content)
            assert "You are executing a single Symphony task" in system or "你负责执行 Symphony 的一个任务节点" in system
            assignment = json.loads(user_input.split("\n\n", 1)[1])
            task = assignment["task"]
            if task in {"research", "organize"} and not results:
                started.add(task)
                if len(started) == 2:
                    both_started.set()
                await asyncio.wait_for(both_started.wait(), timeout=5)
            if task == "verify":
                assert assignment["inputs"] == {"evidence": ["researcher", "organized notes"]}
                return _reply('{"outputs":{"verified":"verified facts"}}')
            if task == "organize":
                assert assignment["role_description"] == "Role for organize"
                return _reply("", ("submit", task_tool_module.SUBMIT_OUTPUTS_TOOL, {
                    "outputs": {"notes": "organized notes"},
                }))
            if task == "report":
                assert assignment["inputs"] == {"verified": "verified facts"}
            assert f"Persona from {assignment['role']} template" in str(messages[0].content)
            if "method" not in results:
                return _reply("", ("method", "skill_tool", {"skill_name": f"{assignment['role']}-method"}))
            assert f"Method for {assignment['role']}" in results["method"]
            if "receipt" not in results:
                return _reply("", ("receipt", "expert_receipt", {}))
            return _reply("", ("submit", task_tool_module.SUBMIT_OUTPUTS_TOOL, {
                "outputs": {assignment["outputs"][0]: results["receipt"]},
            }))

        root_round += 1
        assert "Batch up to five" in str(messages[0].content)
        assert "researcher-method" not in str(messages[0].content)
        assert "reporter-method" not in str(messages[0].content)
        if root_round == 1:
            by_name = {tool.name: tool for tool in tools}
            assert by_name["task_tool"].parameters["properties"].keys() - {"call_goal"} == {"plan_id", "node_id"}
            assert "subagent_spawn" in by_name
            assert task_tool_module.SUBMIT_OUTPUTS_TOOL not in by_name
            return _reply("", ("compose", "symphony_compose_graph", {"query": query, "type": "expert"}))
        if root_round == 2:
            assert "test-plan" in results["compose"]
            assert any(event.type == "tool_result" and "test-plan" in str(event.payload) for event in events)
            if not execute:
                return _reply("Plan displayed")
            research = _task_call("task-1")
            research[2]["call_goal"] = "Collect evidence"
            return _reply("", research, _task_call("task-2"), _task_call("task-3", call_id="early"))
        if root_round == 3:
            assert json.loads(results["early"])["status"] == "waiting"
            return _reply("", _task_call("task-3"), _task_call("task-1", call_id="duplicate"))
        if root_round == 4:
            assert results["duplicate"] == results["dispatch-task-1"]
            return _reply("", _task_call("task-4"))
        assert json.loads(results["dispatch-task-4"])["outputs"] == {"report": "reporter"}
        return _reply("Report delivered")

    agent = _chat_agent(tmp_path, monkeypatch, graph, reply)
    async for event in agent.stream(_chat_input(query)):
        events.append(event)
    assert (events[-1].payload["output"] if "output" in events[-1].payload else events[-1].payload["content"]) == (
        "Report delivered" if execute else "Plan displayed"
    )
    if execute:
        assert started == {"research", "organize"}
        assert all(receipt.read_text() == "template tool ran" for receipt in receipts.values())
        subagent_events = [event for event in events if event.type == "subagent_updated"]
        assert subagent_events
        for event in subagent_events:
            data = event.payload["subagent_updated"]
            assert Runner.resource_mgr.get_tool(f"expert_receipt_{data['subagent_id']}") is None
            assert Runner.resource_mgr.get_tool(f"{task_tool_module.SUBMIT_OUTPUTS_TOOL}_{data['subagent_id']}") is None
    else:
        assert not started and not any(receipt.exists() for receipt in receipts.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["missing_output", "uninstalled", "external_runtime", "timeout"])
async def test_node_failure_blocks_its_branch_only(tmp_path, monkeypatch, problem):
    expert = "researcher" if problem in {"uninstalled", "external_runtime"} else None
    receipts = _install_experts(tmp_path, monkeypatch)
    if problem == "uninstalled":
        (tmp_path / "marketplace.json").write_text('{"plugins":[]}', encoding="utf-8")
    if problem == "external_runtime":
        manifest = tmp_path / "installed" / "package-researcher" / "manifest.json"
        payload = json.loads(manifest.read_text())
        payload["runtime"] = {"provider_name": "codex"}
        manifest.write_text(json.dumps(payload), encoding="utf-8")
    if problem == "timeout":
        monkeypatch.setattr(task_tool_module, "resolve_subagent_task_timeout_s", lambda: 0.3)

    graph = _plan({
        "upstream": _node("upstream", "deliverable", expert=expert),
        "dependent": _node("dependent", "report", input_name="evidence"),
        "independent": _node("independent", "independent"),
    }, [("upstream", "dependent", "deliverable", "evidence")])
    started = []
    stopped = asyncio.Event()

    async def reply(messages, _tools):
        results = _tool_results(messages)
        user_input = next(msg.content for msg in reversed(messages) if isinstance(msg, UserMessage))
        if user_input.startswith("Complete the assigned task"):
            task = json.loads(user_input.split("\n\n", 1)[1])["task"]
            if not results:
                started.append(task)
            if task == "independent":
                return _reply('{"outputs":{"independent":"done"}}')
            assert task == "upstream"
            if problem == "timeout":
                try:
                    await asyncio.Future()
                finally:
                    stopped.set()
            if "missing-output" not in results:
                return _reply("", ("missing-output", task_tool_module.SUBMIT_OUTPUTS_TOOL, {
                    "outputs": {"unexpected": "invalid"},
                }))
            assert "Missing declared outputs" in results["missing-output"]
            return _reply('{"outputs":{"unexpected":"invalid"}}')
        if "compose" not in results:
            return _reply("", ("compose", "symphony_compose_graph", {"query": user_input, "type": "expert"}))
        if "dispatch-upstream" not in results:
            return _reply("", _task_call("upstream"), _task_call("independent"))
        upstream = json.loads(results["dispatch-upstream"])
        assert upstream["status"] == "failed"
        assert {
            "missing_output": "Missing declared outputs: deliverable",
            "uninstalled": "not installed",
            "external_runtime": "requires an external runtime",
            "timeout": "timed out",
        }[problem] in upstream["reason"]
        assert json.loads(results["dispatch-independent"])["status"] == "completed"
        if "dispatch-dependent" not in results:
            return _reply("", _task_call("dependent"), _task_call("upstream", call_id="retry"))
        assert json.loads(results["dispatch-dependent"])["status"] == "blocked"
        assert results["retry"] == results["dispatch-upstream"]
        return _reply("Independent result delivered")

    agent = _chat_agent(tmp_path, monkeypatch, graph, reply)
    assert (await agent.invoke(_chat_input("Do the work")))["output"] == "Independent result delivered"
    assert "dependent" not in started and started.count("upstream") <= 1
    assert not any(receipt.exists() for receipt in receipts.values())
    if problem == "timeout":
        assert stopped.is_set()


@pytest.mark.asyncio
async def test_cancelled_chat_closes_its_node_and_invalidates_the_plan(tmp_path, monkeypatch):
    receipts = _install_experts(tmp_path, monkeypatch)
    graph = _plan({"research": _node("research", "result", expert="researcher")})
    running, stopped = asyncio.Event(), asyncio.Event()
    events = []

    async def reply(messages, _tools):
        results = _tool_results(messages)
        user_input = next(msg.content for msg in reversed(messages) if isinstance(msg, UserMessage))
        if user_input.startswith("Complete the assigned task"):
            if "receipt" not in results:
                return _reply("", ("receipt", "expert_receipt", {}))
            running.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()
        if user_input == "Try the previous plan":
            if "old-plan" not in results:
                return _reply("", _task_call("research", call_id="old-plan"))
            assert json.loads(results["old-plan"])["status"] == "rejected"
            return _reply("Previous plan is unavailable")
        if "compose" not in results:
            return _reply("", ("compose", "symphony_compose_graph", {"query": user_input, "type": "expert"}))
        return _reply("", _task_call("research"))

    agent = _chat_agent(tmp_path, monkeypatch, graph, reply)
    inputs = _chat_input("Run the research")

    async def chat():
        async for event in agent.stream(inputs):
            events.append(event)

    turn = asyncio.create_task(chat())
    try:
        await asyncio.wait_for(running.wait(), timeout=5)
    finally:
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn
    assert stopped.is_set()
    assert receipts["researcher"].read_text() == "template tool ran"
    for event in events:
        if event.type == "subagent_updated":
            subagent_id = event.payload["subagent_updated"]["subagent_id"]
            assert Runner.resource_mgr.get_tool(f"expert_receipt_{subagent_id}") is None
    result = await agent.invoke({**inputs, "query": "Try the previous plan"})
    assert result["output"] == "Previous plan is unavailable"


@pytest.mark.asyncio
async def test_clarification_recomposes_before_dispatch(tmp_path, monkeypatch):
    ready = _plan({"task-1": _node("report", "report")})
    clarification = _plan({})
    clarification["planned_graph"]["graph"]["metadata"].update(
        status="needs_input", missing_inputs=["Audience"],
    )
    compose_queries, assignments = [], []

    def compose(query):
        compose_queries.append(query)
        return ready if "Engineering" in query else clarification

    async def reply(messages, _tools):
        results = _tool_results(messages)
        user_input = next(msg.content for msg in reversed(messages) if isinstance(msg, UserMessage))
        if user_input.startswith("Complete the assigned task"):
            assignments.append(json.loads(user_input.split("\n\n", 1)[1]))
            return _reply('{"outputs":{"report":"done"}}')
        if not compose_queries:
            return _reply("", ("compose-1", "symphony_compose_graph", {"query": "Write a report", "type": "expert"}))
        if "ask" not in results:
            return _reply("", ("ask", "ask_user", {"question": "Audience"}))
        if "compose-2" not in results:
            return _reply("", ("compose-2", "symphony_compose_graph", {"query": "Write a report", "type": "expert"}))
        if "dispatch-task-1" not in results:
            return _reply("", _task_call("task-1"))
        assert json.loads(results["dispatch-task-1"])["status"] == "completed"
        return _reply("Delivered")

    agent = _chat_agent(tmp_path, monkeypatch, compose, reply, extra_rails=[AskUserRail()])
    inputs = _chat_input("Write a report")
    assert (await agent.invoke(inputs))["result_type"] == "interrupt"
    assert not assignments
    answer = InteractiveInput()
    answer.update("ask", {"answers": {"Audience": "Engineering"}})
    assert (await agent.invoke({**inputs, "query": answer}))["output"] == "Delivered"
    assert len(compose_queries) == 2
    assert "Engineering" in compose_queries[-1]
    assert len(assignments) == 1 and "Engineering" in assignments[0]["user_request"]


def test_plan_allows_five_running_nodes_and_never_claims_a_node_twice():
    graph = _plan({str(index): _node(f"work {index}", "result") for index in range(6)})
    plan = ExpertTaskPlan.from_result(graph, "Do the work")
    assert plan is not None
    for index in range(5):
        assert not plan.claim(str(index))
    assert plan.claim("0")["status"] == "running"
    assert plan.claim("5")["status"] == "waiting"
    plan.finish("0", {"status": "completed", "outputs": {"result": "done"}})
    assert not plan.claim("5")


@pytest.mark.asyncio
async def test_new_plan_in_same_turn_keeps_previous_dispatch_idempotent(tmp_path, monkeypatch):
    plans = {name: _plan({"task-1": _node(name, "result")}) for name in ("first", "second")}
    for name, plan in plans.items():
        plan["planned_graph"]["graph"]["id"] = name
    started = []

    async def reply(messages, _tools):
        user_input = next(msg.content for msg in reversed(messages) if isinstance(msg, UserMessage))
        if user_input.startswith("Complete the assigned task"):
            task = json.loads(user_input.split("\n\n", 1)[1])["task"]
            started.append(task)
            return _reply(json.dumps({"outputs": {"result": task}}))
        results = _tool_results(messages)
        for name in plans:
            if f"compose-{name}" not in results:
                return _reply("", (f"compose-{name}", "symphony_compose_graph", {"query": name, "type": "expert"}))
            if f"dispatch-{name}" not in results:
                calls = [_task_call("task-1", call_id=f"dispatch-{name}", plan_id=name)]
                if name == "second":
                    calls.append(_task_call("task-1", call_id="duplicate", plan_id="first"))
                return _reply("", *calls)
        assert results["duplicate"] == results["dispatch-first"]
        assert json.loads(results["dispatch-second"])["outputs"] == {"result": "second"}
        return _reply("Delivered")

    agent = _chat_agent(tmp_path, monkeypatch, plans.__getitem__, reply)
    assert (await agent.invoke(_chat_input("Do both parts of the task")))["output"] == "Delivered"
    assert started == ["first", "second"]


@pytest.mark.asyncio
async def test_task_tool_preserves_ordinary_synchronous_delegation(tmp_path, monkeypatch):
    async def reply(messages, _tools):
        user_input = next(msg.content for msg in reversed(messages) if isinstance(msg, UserMessage))
        if user_input == "Ordinary delegation":
            return _reply("ordinary result")
        results = _tool_results(messages)
        if "ordinary" not in results:
            return _reply("", ("ordinary", "task_tool", {
                "subagent_type": "general-purpose", "task_description": "Ordinary delegation",
            }))
        assert results["ordinary"] == "ordinary result"
        return _reply("Delivered")

    agent = _chat_agent(tmp_path, monkeypatch, None, reply, persistent=False)
    assert (await agent.invoke(_chat_input("Delegate a task")))["output"] == "Delivered"
