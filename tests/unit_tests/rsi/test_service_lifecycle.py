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
