"""Production assembly, wire consumption and background ownership regressions."""

import asyncio
import contextlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from jiuwenswarm.agents.harness.common.rsi import build_rsi_service_context
from jiuwenswarm.common.e2a.gateway_normalize import e2a_response_to_agent_response
from jiuwenswarm.common.e2a.models import E2AEnvelope, E2AResponse
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.rsi import RsiAgentServerHandlers


async def wait_status(context, task_id, expected):
    async with asyncio.timeout(40):
        while context.store.get(task_id).status not in expected:
            await asyncio.sleep(0.01)
    return context.store.get(task_id).status


@pytest.mark.asyncio
async def test_server_stop_cancels_owned_rsi_tasks(tmp_path):
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.agents.harness.common.rsi.harness_adapter import HarnessEngineAdapter
    from jiuwenswarm.agents.harness.common.rsi.mock_harness_provider import MockHarnessProvider

    context = build_rsi_service_context(tmp_path / "tasks")
    context.register_adapters({"HARNESS": HarnessEngineAdapter(
        MockHarnessProvider(context.tasks_root, iteration_delay=3600))})
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._rsi_handlers = RsiAgentServerHandlers(context)
    server._stop_main_services = AsyncMock()
    server._stop_personal_context_best_effort = AsyncMock()
    dataset = tmp_path / "dataset.json"
    dataset.write_text('{"cases":[{"case_id":"one"}]}', encoding="utf-8")
    task_id = context.task_service.create({
        "scenario": "HARNESS", "name": "shutdown regression", "input_file": str(dataset),
        "model_refs": {"optimizer": "controlled", "tester": "controlled"},
    })["task_id"]
    context.worker.enqueue(task_id)
    await wait_status(context, task_id, {"RUNNING"})
    # Ensure the provider, rather than just its queue supervisor, is running.
    async with asyncio.timeout(5):
        while not context.worker._execution_runners:
            await asyncio.sleep(0)
    owned = {context.worker._run_task, *context.worker._execution_tasks.values(),
             *context.worker._execution_runners.values()}
    queued_id = context.task_service.create({
        "scenario": "HARNESS", "name": "queued at shutdown", "input_file": str(dataset),
        "model_refs": {"optimizer": "controlled", "tester": "controlled"},
    })["task_id"]
    context.worker.enqueue(queued_id)
    try:
        await server.stop()
        assert all(task.done() for task in owned)
        assert context.store.get(task_id).status == "TERMINATED"
        from jiuwenswarm.agents.harness.common.rsi.errors import RsiNotReady
        with pytest.raises(RsiNotReady, match="closed"):
            context.worker.enqueue(queued_id)
        restarted = build_rsi_service_context(context.tasks_root)
        RsiAgentServerHandlers(restarted)
        assert restarted.store.get(queued_id).status == "PAUSED"
        assert restarted.worker._run_task is None
        await server.stop()  # Shutdown must remain safe when repeated.
    finally:
        for task in owned:
            task.cancel()
        await asyncio.gather(*owned, return_exceptions=True)


@pytest.mark.asyncio
async def test_production_factory_executes_real_engine_through_registered_wire_handler(tmp_path, monkeypatch):
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.handlers import rsi
    from jiuwenswarm.agents.harness.common.rsi import context as context_module
    from jiuwenswarm.agents.harness.common.rsi import model_resolver
    from openjiuwen.rsi.harness_rsi.evaluation_result_analyzer.analyzer import EvaluationResultAnalyzer
    from openjiuwen.core.runner import Runner
    from openjiuwen.harness import create_deep_agent
    from jiuwenswarm.server.runtime.agent_manager import AgentManager
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    from tests.unit.deep_agent import test_browser_subagent_integration as fixture
    clients = []

    class ControlledClient(fixture.MockLLMModel):
        async def invoke(self, messages, **kwargs):
            if "image_url" in str(messages) and not kwargs.get("tools"):
                return fixture.create_text_response("unsupported")
            return await super().invoke(messages, **kwargs)

    def client_factory(**kwargs):
        client = ControlledClient()
        client.set_responses([fixture.create_tool_call_response(
            "write_file", '{"file_path":"artifacts/answer.txt","content":"service evidence"}',
            tool_call_id="service-write"),
            fixture.create_text_response("done")] * 8)
        clients.append(client)
        return client

    monkeypatch.setattr("openjiuwen.core.foundation.llm.model.create_model_client", client_factory)
    monkeypatch.setattr(rsi, "get_config", lambda: {"rsi": {"enabled": True}})
    monkeypatch.setattr(context_module, "get_rsi_workspace_root", lambda: tmp_path / "rsi")
    monkeypatch.setenv("RSI_PROVIDER_MODE", "real")
    monkeypatch.setattr(model_resolver, "_default_config", lambda: {})
    monkeypatch.setattr(model_resolver, "_default_zen_models", lambda: [])
    monkeypatch.setattr(model_resolver, "_default_models", lambda _: [{
        "model_client_config": {"model_name": "controlled", "client_provider": "OpenAI",
                                "api_base": "http://unused", "api_key": "test-only"},
        "model_config_obj": {}, "is_default": True,
    }])

    async def no_issues(self, invocation):
        path = Path(invocation.output_dir) / "analysis_ref.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("issues: []\n", encoding="utf-8")
        return str(path)

    # This scenario tests the no-change publication path. Analysis decisions
    # are controlled; the evaluator, model wrapper, agent and tools are real.
    monkeypatch.setattr(EvaluationResultAnalyzer, "analyze", no_issues)
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    live_agent = create_deep_agent(model=MagicMock(), workspace=str(tmp_path / "live"),
                                   agent_ras=False, auto_create_workspace=False)
    adapter = JiuWenSwarmDeepAdapter(workspace_dir=str(tmp_path / "live"))
    adapter._instance = live_agent
    manager = AgentManager.__new__(AgentManager)
    manager.agents = {"web": {"agent:service": adapter}}
    server._agent_manager = manager
    pushes = []
    server.send_push = lambda message: pushes.append(message)
    dataset = tmp_path / "dataset.json"
    dataset.write_text(json.dumps({"cases": [{"case_id": "one", "input": "Write answer.txt then say done",
                                             "expected_output": "done"}]}), encoding="utf-8")
    seq = 0

    async def call(method, params=None, *, expected_ok=True):
        nonlocal seq
        seq += 1
        ws = AsyncMock()
        request = E2AEnvelope(request_id=f"wire-{seq}", channel="web", session_id="service-session",
                              method=method.value, params=params or {})
        await server._handle_message(ws, json.dumps(request.to_dict()), asyncio.Lock())
        wire = ws.send.call_args.args[0]
        response = e2a_response_to_agent_response(E2AResponse.from_dict(
            json.loads(wire) if isinstance(wire, (str, bytes)) else wire))
        assert response.request_id == request.request_id
        assert response.ok is expected_ok, response.payload
        return response.payload

    try:
        assert (await call(ReqMethod.RSI_CAPABILITIES))["enabled"] is True
        created = await call(ReqMethod.RSI_TASK_CREATE, {
            "scenario": "HARNESS", "name": "production-wire", "input_file": str(dataset),
            "model_refs": {"optimizer": "controlled", "tester": "controlled"},
            "max_epochs": 1, "evaluation_method": "exact_match",
        })
        task_id = created["task_id"]
        context = server._get_rsi_handlers().context
        await call(ReqMethod.RSI_TRAINING_START, {"task_id": task_id})
        status = await wait_status(context, task_id, {"COMPLETED", "FAILED"})
        detail = await call(ReqMethod.RSI_TASK_GET, {"task_id": task_id})
        assert status == "COMPLETED", detail
        assert (await call(ReqMethod.RSI_REPORT_GET, {"task_id": task_id}))["status"] == "COMPLETED"
        tree = await call(ReqMethod.RSI_TREE_GET, {"task_id": task_id})
        snapshot_id = next(node["snapshot_artifact_id"] for node in tree["nodes"] if node.get("snapshot_artifact_id"))
        preview = await call(ReqMethod.RSI_ARTIFACT_FILES_LIST, {"task_id": task_id, "artifact_id": snapshot_id})
        assert Path(preview["initial_path"]).name == "harness_config.yaml"
        config = await call(ReqMethod.RSI_ARTIFACT_FILES_GET, {"task_id": task_id, "path": preview["initial_path"]})
        assert yaml.safe_load(config["content"])["id"] == "rsi-native-agent-baseline"
        task_root = context.tasks_root / task_id
        answers = list((task_root / "run").rglob("answer.txt"))
        assert answers and all(path.read_text(encoding="utf-8") == "service evidence" for path in answers)
        state = yaml.safe_load((task_root / "run/single_harness_state.yaml").read_text(encoding="utf-8"))
        assert state["publication_status"] == "not_published_no_improvement"
        assert sum(client.call_count for client in clients) >= 2
        assert any(push["payload"]["event_type"] == "rsi.training.progress" for push in pushes)
        assert all(push.get("session_id") == "service-session" for push in pushes)
        rejected = await call(ReqMethod.RSI_HARNESS_INSTALL, {"task_id": task_id}, expected_ok=False)
        assert rejected["code"] == "RSI_HARNESS_NOT_PUBLISHED"
        versions = await call(ReqMethod.RSI_HARNESS_VERSIONS_LIST)
        assert versions["active_installation_id"] is None
        # A fresh production assembly must read durable results without running models.
        calls_before = sum(client.call_count for client in clients)
        del server._rsi_handlers
        assert (await call(ReqMethod.RSI_TASK_GET, {"task_id": task_id}))["status"] == "COMPLETED"
        assert (await call(ReqMethod.RSI_HARNESS_VERSIONS_LIST))["active_installation_id"] is None
        assert sum(client.call_count for client in clients) == calls_before
    finally:
        handlers = getattr(server, "_rsi_handlers", None)
        if handlers and handlers.context.worker._run_task:
            handlers.context.worker._run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await handlers.context.worker._run_task
        live_agent.ability_manager.teardown_tools()
        await Runner.stop()


@pytest.mark.asyncio
async def test_published_package_installs_and_rolls_back_on_real_cached_agent(tmp_path):
    from tests.unit_tests.rsi.test_harness_activation import _completed_harness_task, _write_published_state
    from jiuwenswarm.server.runtime.agent_manager import AgentManager
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
    from openjiuwen.harness import create_deep_agent

    agent = create_deep_agent(model=MagicMock(), workspace=str(tmp_path / "live"),
                              agent_ras=False, auto_create_workspace=False)
    adapter = JiuWenSwarmDeepAdapter(workspace_dir=str(tmp_path / "live"))
    adapter._instance = agent
    manager = AgentManager.__new__(AgentManager)
    manager.agents = {"web": {"agent:service": adapter}}
    context = build_rsi_service_context(tmp_path / "tasks", agent_manager=manager)
    handlers = RsiAgentServerHandlers(context)

    async def call(method, params=None):
        result = await handlers.handle_async(AgentRequest(request_id="install-test", req_method=method,
                                                          params=params or {}))
        assert result["ok"], result
        return result["payload"]

    try:
        installations = []
        # Published fixtures isolate the install transaction from optimization
        # decisions. The installer, manager fan-out and DeepAgent are unpatched.
        for index in (1, 2):
            task_id = f"rsi-published-{index}"
            context.store.create(_completed_harness_task(context, task_id))
            refs = _write_published_state(context, task_id, extension_name=f"policy-{index}")
            package = refs.parent / "current_harnesses/policy_harness"
            (package / "harness_config.yaml").write_text(yaml.safe_dump({
                "schema_version": "expert_harness.v1", "id": f"policy-{index}",
                "prompt_sections": [{"name": "rsi.policy", "content": {"en": f"INSTALLED_POLICY_{index}"}}],
            }), encoding="utf-8")
            installed = await call(ReqMethod.RSI_HARNESS_INSTALL, {"task_id": task_id})
            installations.append(installed["installation_id"])
            assert installed["hot_load"]["succeeded"] == 1
            assert adapter._rsi_harness_install_id == installations[-1]
            assert f"INSTALLED_POLICY_{index}" in agent.system_prompt_builder.build()
        assert "INSTALLED_POLICY_1" not in agent.system_prompt_builder.build()
        await call(ReqMethod.RSI_HARNESS_ROLLBACK, {"installation_id": installations[0]})
        assert "INSTALLED_POLICY_1" in agent.system_prompt_builder.build()
        assert "INSTALLED_POLICY_2" not in agent.system_prompt_builder.build()
        assert context.harness_activation_store.get_active()["installation_id"] == installations[0]
        restarted = build_rsi_service_context(context.tasks_root)
        assert restarted.harness_activation_store.get_active()["installation_id"] == installations[0]
    finally:
        agent.ability_manager.teardown_tools()


@pytest.mark.asyncio
async def test_real_server_constructs_with_paired_stable_personal_context(tmp_path, monkeypatch):
    from jiuwenswarm.server import agent_ws_server
    monkeypatch.setattr(agent_ws_server, "get_user_workspace_dir", lambda: tmp_path)
    server = agent_ws_server.AgentWebSocketServer(port=0)
    try:
        await server._personal_context_host.start()
        assert server._personal_context_host._home == tmp_path / ".personal_context"
        assert server._agent_manager is not None
    finally:
        await server._personal_context_host.stop()
