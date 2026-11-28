# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exercise Symphony dispatch through the single-agent model and tool loop."""

from __future__ import annotations

import ast
import asyncio
import json
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image

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
from openjiuwen.symphony import OrchestrationConfig, OrchestrationService, SymphonyGraphEngine

from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail
from jiuwenswarm.agents.harness.common.rails.symphony import task_tool as task_tool_module
from jiuwenswarm.agents.harness.common.rails.symphony.expert_task_plan import ExpertTaskPlan
from jiuwenswarm.agents.harness.common.tools.symphony_toolkits import SymphonyToolkit
from jiuwenswarm.agents.harness.team.experts import catalog
from jiuwenswarm.runtime.symphony.planning_inputs import supports_planning_inputs
from jiuwenswarm.runtime.symphony.service import SwarmSymphonyService
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.session.session_history import flush_history_writes, load_history_records
from jiuwenswarm.server.wire_truncate import split_history_record_for_stream


_CONFIG = {"symphony": {"orchestration": {"enabled": True}}, "react": {"subagent_runtime": {"enabled": True}}}
_RESEARCHER_NAMES = {"zh": "调研专家", "en": "Research Expert"}


def _reset_core_fs_lock_state() -> None:
    """Detach openjiuwen's process-global file locks from whichever loop built them.

    The core lock manager caches loop-bound asyncio primitives (its ``_state_lock``
    and each entry's ``Condition``) in class attributes, while pytest-asyncio gives
    every test a fresh event loop. A cache inherited from an earlier test fails
    with "bound to a different event loop" once a file tool runs again, so the
    dispatch below cannot assume it owns a clean manager.
    """
    from openjiuwen.core.sys_operation.local._rw_lock_manager import ReadWriteLockManager

    for entry in tuple(ReadWriteLockManager._locks.values()):  # noqa: SLF001
        entry.lock.evict_singleton()
    ReadWriteLockManager._locks.clear()  # noqa: SLF001
    ReadWriteLockManager._idle_heap.clear()  # noqa: SLF001
    ReadWriteLockManager._idle_deadlines.clear()  # noqa: SLF001
    ReadWriteLockManager._state_lock = None  # noqa: SLF001


@pytest.fixture(autouse=True)
def _isolated_core_fs_locks():
    """Keep this file's file-tool locks from leaking into or out of its tests."""
    _reset_core_fs_lock_state()
    yield
    _reset_core_fs_lock_state()


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
        (package / "avatars").mkdir()
        avatar = package / "avatars" / "avatar.png"
        if expert == "researcher":
            Image.new("RGB", (32, 32), "blue").save(avatar)
        else:
            avatar.write_bytes(b"corrupt image")
        (package / "manifest.json").write_text(json.dumps({
            "package_type": "agent_template", "name": expert, "description": "Test expert",
            "persona": {"dir": "persona"}, "tools": [{"file": "receipt.py", "class": "Receipt"}],
            "skills": [{"dir": f"skills/{expert}-method"}],
            "avatar": "avatars/avatar.png",
            **({"display_name": _RESEARCHER_NAMES} if expert == "researcher" else {}),
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


def _chat_agent(tmp_path, monkeypatch, plan, reply, *, extra_rails=(), persistent=True, service=None):
    async def compose(query, **kwargs):
        assert query and kwargs["capability_type"] == "expert"
        return plan(query) if callable(plan) else plan

    adapter = JiuWenSwarmDeepAdapter()
    config = {**_CONFIG, "react": {"subagent_runtime": {"enabled": persistent}}}
    adapter._config_base_cache = config
    toolkit = SymphonyToolkit(service=service or SimpleNamespace(plan=compose), llm_config_provider=lambda: None)
    monkeypatch.setattr(toolkit, "is_enabled", lambda config=None: True)
    compose_tool = next(tool for tool in toolkit.get_tools(config) if tool.card.name == "symphony_compose_graph")
    properties = {"query", "type", "mode", "candidate_ids", "candidate_skill_ids"}
    if supports_planning_inputs():
        properties.add("inputs")
    assert set(compose_tool.card.input_params["properties"]) == properties
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
@pytest.mark.skipif(not supports_planning_inputs(), reason="Requires Core planning inputs support")
@pytest.mark.parametrize("availability", ["complete", "no-price", "no-attachments"])
async def test_three_platform_launch_compose_and_dispatch(tmp_path, monkeypatch, availability):
    query = "根据事实表写小红书首发笔记、公众号活动稿和 B 站脚本，再独立审查。可以查阅平台公开规则。"
    source_text = "T1 有三个亮度档。"
    facts = tmp_path / "product-facts.md"
    facts.write_text(source_text, encoding="utf-8")
    materials = []
    if availability != "no-attachments":
        materials.append({"name": "product_facts", "kind": "file", "value": facts.name})
    if availability == "complete":
        materials.append({"name": "approved_price", "kind": "text", "value": "199 元"})
    names = {item["name"] for item in materials}
    drafts = {"xhs": "xhs_draft", "wechat": "wechat_draft", "bilibili": "bilibili_script"}
    missing = {"product_facts", "approved_price"} - names
    calls = []
    started = []
    plan_id = ""

    async def plan_from_model(messages, **_kwargs):
        payload = json.loads(messages[-1]["content"])
        calls.append(payload)
        assert payload["query"] == query
        if payload.get("operation") == "task_routing":
            assert set(payload["context"]) == {"skills", "experts", "tools", "context_complete"}
            return SimpleNamespace(content=json.dumps({
                "strategy": "multi_expert", "reason": "Independent drafts and review",
            }))
        assert payload.get("inputs", []) == [
            {**item, "value": str(facts.resolve())} if item["kind"] == "file" else item for item in materials
        ]
        if not materials:
            missing_only = {"status": "needs_input", "missing_inputs": sorted(missing)}
            if "validation_feedback" not in payload:
                return SimpleNamespace(content=json.dumps(missing_only))
            assert json.loads(payload["validation_feedback"]["previous_output"]) == missing_only
        tasks = []
        for task, output in {**drafts, "audit": "review"}.items():
            external_inputs = ["product_facts"]
            if task in {"wechat", "audit"}:
                external_inputs.append("approved_price")
            if task == "audit":
                # A model may repeat incoming drafts among the reviewer's external materials.
                external_inputs.extend(drafts.values())
            tasks.append({
                "id": task, "task": task, "role": {"name": task, "description": f"Role for {task}"},
                "external_inputs": external_inputs, "outputs": [{"name": output, "type": "text"}],
            })
        handoffs = [
            {"source": task, "target": "audit", "source_output": name}
            for task, name in drafts.items()
        ]
        return SimpleNamespace(content=json.dumps({"tasks": tasks, "handoffs": handoffs}))

    core = OrchestrationService(
        graph_artifact_root=tmp_path / "graph",
        capability_provider=[],
        model=SimpleNamespace(invoke=plan_from_model),
        config=OrchestrationConfig(enabled=True),
    )
    host = SwarmSymphonyService()
    host_config = SimpleNamespace(
        capability=SimpleNamespace(sources=[]), orchestration=core.config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.resolve_preferred_language",
        lambda: "zh",
    )
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.service.load_symphony_config", lambda: host_config)
    monkeypatch.setattr(host, "_runtime_for", lambda _config: SimpleNamespace(orchestration=SymphonyGraphEngine(core)))

    async def reply(messages, _tools):
        nonlocal plan_id
        user_input = next(msg.content for msg in reversed(messages) if isinstance(msg, UserMessage))
        results = _tool_results(messages)
        if user_input.startswith("Complete the assigned task"):
            assignment = json.loads(user_input.split("\n\n", 1)[1])
            task = assignment["task"]
            external = assignment["external_inputs"]
            assert external["product_facts"]["value"] == str(facts.resolve())
            assert ("approved_price" in external) == (task in {"wechat", "audit"})
            read_call_id = f"read-facts-{task}"
            if read_call_id not in results:
                return _reply("", (read_call_id, "read_file", {"file_path": external["product_facts"]["value"]}))
            assert source_text in results[read_call_id]
            started.append(task)
            if task == "audit":
                assert assignment["inputs"] == {name: f"draft from {platform}" for platform, name in drafts.items()}
                return _reply('{"outputs":{"review":"checked"}}')
            return _reply(json.dumps({"outputs": {drafts[task]: f"draft from {task}"}}))
        if user_input == "试用上一轮计划":
            if "old-plan" not in results:
                return _reply("", _task_call("task-1", call_id="old-plan", plan_id=plan_id))
            assert json.loads(results["old-plan"])["status"] == "rejected"
            return _reply("上一轮的图已失效")
        if "compose" not in results:
            args = {"query": user_input}
            if materials:
                args["inputs"] = materials
            return _reply("", ("compose", "symphony_compose_graph", args))
        compose_result = ast.literal_eval(results["compose"])
        assert "execution_route" not in compose_result
        graph = compose_result["planned_graph"]["graph"]
        assert all("external_inputs" not in node["metadata"] for node in graph["nodes"].values())
        plan_id = graph["id"]
        assert graph["metadata"]["status"] == "ready"
        assert {hint["name"] for hint in graph["metadata"]["missing_inputs"]} == missing
        assert list(graph["nodes"]) == ["task-1", "task-2", "task-3", "task-4"]
        if availability == "no-attachments":
            return _reply("先展示分工")
        if "early-audit" not in results:
            return _reply("", _task_call("task-4", call_id="early-audit", plan_id=plan_id))
        assert json.loads(results["early-audit"])["status"] == "waiting"
        chosen = ["task-1", "task-2", "task-3"] if availability == "complete" else ["task-1", "task-3"]
        pending = [_task_call(node, plan_id=plan_id) for node in chosen if f"dispatch-{node}" not in results]
        if pending:
            return _reply("", *pending)
        dispatched = {node: json.loads(results[f"dispatch-{node}"]) for node in chosen}
        assert all(result["status"] == "completed" for result in dispatched.values()), dispatched
        if availability == "complete" and "dispatch-task-4" not in results:
            return _reply("", _task_call("task-4", plan_id=plan_id))
        return _reply("交付当前结果")

    agent = _chat_agent(tmp_path, monkeypatch, None, reply, service=host)
    inputs = _chat_input(query)
    result = await agent.invoke(inputs)
    assert result["output"] == ("先展示分工" if availability == "no-attachments" else "交付当前结果")
    if availability == "complete":
        assert set(started) == {"xhs", "wechat", "bilibili", "audit"}
        assert started[-1] == "audit"
        assert (await agent.invoke({**inputs, "query": "试用上一轮计划"}))["output"] == "上一轮的图已失效"
    else:
        assert set(started) == ({"xhs", "bilibili"} if availability == "no-price" else set())
    assert len(calls) == (3 if availability == "no-attachments" else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("execute", [True, False])
async def test_chat_returns_dag_then_delegates_native_and_inline_nodes(tmp_path, monkeypatch, execute):
    receipts = _install_experts(tmp_path, monkeypatch)
    report_task = "Write the report." + " Cite the verified sources in the final report." * 12
    expected_roles = {
        "research": "researcher",
        "organize": "organize",
        "verify": "verify",
        report_task: "reporter",
        "check citations": "researcher",
    }
    graph = _plan({
        "task-1": _node("research", "findings", expert="researcher"),
        "task-2": _node("organize", "notes"),
        "task-3": _node("verify", "verified", input_name="evidence"),
        "task-4": _node(report_task, "report", expert="reporter", input_name="verified"),
        "task-5": _node("check citations", "citations", expert="researcher"),
    }, [
        ("task-1", "task-3", "FINDINGS", "Evidence"),
        ("task-2", "task-3", "notes", "evidence"),
        ("task-5", "task-3", "citations", "evidence"),
        ("task-3", "task-4", "verified", "verified"),
    ])
    graph["execution_route"] = {"strategy": "multi_expert", "requires_planning": True}
    graph["planned_graph"]["graph"]["nodes"]["task-1"]["metadata"]["external_inputs"] = []
    both_started = asyncio.Event()
    started = set()
    root_round = 0
    events = []
    query = (
        "Research and write the report." + " Use the supplied materials to verify the facts." * 14
        if execute else "Show only the assignment graph"
    )

    async def reply(messages, tools):
        nonlocal root_round
        user_input = next(msg.content for msg in reversed(messages) if isinstance(msg, UserMessage))
        results = _tool_results(messages)
        if user_input.startswith("Complete the assigned task"):
            system = str(messages[0].content)
            assert "You are executing a single Symphony task" in system or "你负责执行 Symphony 的一个任务节点" in system
            assignment = json.loads(user_input.split("\n\n", 1)[1])
            task = assignment["task"]
            assert assignment["user_request"] == query
            assert assignment["role"] == expected_roles[task]
            if task in {"research", "organize", "check citations"} and not results:
                started.add(task)
                if len(started) == 3:
                    both_started.set()
                await asyncio.wait_for(both_started.wait(), timeout=5)
            if task == "verify":
                assert assignment["inputs"] == {"evidence": ["researcher", "organized notes", "researcher"]}
                return _reply('{"outputs":{"verified":"verified facts"}}')
            if task == "organize":
                assert assignment["role_description"] == "Role for organize"
                return _reply("", ("submit", task_tool_module.SUBMIT_OUTPUTS_TOOL, {
                    "outputs": {"notes": "organized notes"},
                }))
            if task == report_task:
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
            compose_event = next(
                event.payload["tool_result"] for event in events
                if event.type == "tool_result" and event.payload["tool_result"]["tool_call_id"] == "compose"
            )
            assert compose_event["rendered_result"] == results["compose"]
            public_result = compose_event["raw_output"]
            assert public_result == ast.literal_eval(results["compose"])
            assert "execution_route" not in public_result
            public_graph = public_result["planned_graph"]["graph"]
            assert public_graph["id"] == "test-plan"
            assert "external_inputs" not in public_graph["nodes"]["task-1"]["metadata"]
            if not execute:
                return _reply("Plan displayed")
            research = _task_call("task-1")
            research[2]["call_goal"] = "Collect evidence"
            return _reply("", research, _task_call("task-2"), _task_call("task-5"), _task_call("task-3", call_id="early"))
        if root_round == 3:
            assert json.loads(results["early"])["status"] == "waiting"
            return _reply("", _task_call("task-3"), _task_call("task-1", call_id="duplicate"))
        if root_round == 4:
            assert results["duplicate"] == results["dispatch-task-1"]
            return _reply("", _task_call("task-4"))
        assert json.loads(results["dispatch-task-4"])["outputs"] == {"report": "reporter"}
        return _reply("Report delivered")

    agent = _chat_agent(tmp_path, monkeypatch, graph, reply)
    chat_input = _chat_input(query)
    async for event in agent.stream(chat_input):
        events.append(event)
    assert (events[-1].payload["output"] if "output" in events[-1].payload else events[-1].payload["content"]) == (
        "Report delivered" if execute else "Plan displayed"
    )
    if execute:
        assert started == {"research", "organize", "check citations"}
        assert all(receipt.read_text() == "template tool ran" for receipt in receipts.values())
        subagent_events = [event for event in events if event.type == "subagent_updated"]
        subagent_ids_by_task = {}
        avatars_by_task = {}
        for event in subagent_events:
            data = JiuWenSwarmDeepAdapter.parse_stream_chunk(event)
            assert data is not None and data["event_type"] == "chat.subtask_update"
            task = data["task_description"]
            role = expected_roles[task]
            assert subagent_ids_by_task.setdefault(task, data["subagent_id"]) == data["subagent_id"]
            if role == "researcher":
                assert data["display_name"] in _RESEARCHER_NAMES.values()
                assert data["display_name_i18n"] == _RESEARCHER_NAMES
                if data.get("avatar"):
                    assert data["avatar"].startswith("data:image/webp;base64,")
                    assert avatars_by_task.setdefault(task, data["avatar"]) == data["avatar"]
            else:
                assert data["display_name"] == role
                assert data.get("display_name_i18n") == ({"zh": role, "en": role} if role == "reporter" else None)
                assert "avatar" not in data
            assert Runner.resource_mgr.get_tool(f"expert_receipt_{data['subagent_id']}") is None
            assert Runner.resource_mgr.get_tool(f"{task_tool_module.SUBMIT_OUTPUTS_TOOL}_{data['subagent_id']}") is None
        assert subagent_ids_by_task.keys() == expected_roles.keys()
        assert len(set(subagent_ids_by_task.values())) == len(expected_roles)
        assert avatars_by_task.keys() == {"research", "check citations"}
        assert avatars_by_task["research"] == avatars_by_task["check citations"]
        flush_history_writes()
        for task, avatar_url in avatars_by_task.items():
            records = load_history_records(chat_input["conversation_id"], subagent_id=subagent_ids_by_task[task])
            terminal = next(record for record in records if record.get("turn_outcome") == "completed")
            [replay] = split_history_record_for_stream(terminal)
            assert replay["avatar"] == avatar_url
            assert replay["display_name_i18n"] == _RESEARCHER_NAMES
            assert replay["task_description"] == task
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
    node_deadline: ContextVar[asyncio.Timeout] = ContextVar("symphony_node_deadline")
    if problem == "timeout":
        def track_node_timeout(delay: float) -> asyncio.Timeout:
            deadline = asyncio.timeout(delay)
            node_deadline.set(deadline)
            return deadline

        monkeypatch.setattr(task_tool_module, "resolve_subagent_task_timeout_s", lambda: 15.0)
        monkeypatch.setattr(task_tool_module, "timeout", track_node_timeout)

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
                    # Expire this node once its model is running, regardless of startup time.
                    node_deadline.get().reschedule(asyncio.get_running_loop().time())
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
async def test_host_can_clarify_then_dispatch_the_existing_graph(tmp_path, monkeypatch):
    ready = _plan({"task-1": _node("report", "report")})
    ready["planned_graph"]["graph"]["metadata"]["missing_inputs"] = [{"name": "Audience", "kind": "text"}]
    ready["planned_graph"]["graph"]["nodes"]["task-1"]["metadata"]["external_inputs"] = ["Audience"]
    compose_queries, assignments = [], []

    def compose(query):
        compose_queries.append(query)
        return ready

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
    assert compose_queries == ["Write a report"]
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


def test_deferred_symphony_policy_tracks_current_configuration():
    """A lazily loaded toolkit still supplies policy on the first SDK invoke."""
    adapter = JiuWenSwarmDeepAdapter()
    adapter._config_base_cache = _CONFIG
    rail = adapter._build_subagent_rail(_CONFIG)
    assert rail is not None
    assert rail._symphony_tasks_enabled() is True
    adapter._config_base_cache = {"symphony": {"orchestration": {"enabled": False}}}
    assert rail._symphony_tasks_enabled() is False
