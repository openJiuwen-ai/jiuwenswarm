"""A real Gateway store is queried over E2A with a separate Agent data directory."""
import asyncio
import json
import multiprocessing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _gateway_process(connection, directory):
    async def run():
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
        from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_chunk
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.cron.controller import CronController
        from jiuwenswarm.gateway.cron.store import CronJobStore
        from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler

        store = CronJobStore(path=Path(directory) / "jobs.json")
        await store.create_job(job_id="remote", name="remote", cron_expr="0 9 * * *",
                               timezone="UTC", description="test", targets="web", user_id="alice")
        handler = object.__new__(MessageHandler)

        class Client:
            async def send_request(self, envelope, **kwargs):
                connection.send(json.dumps(envelope.to_dict()))
                return SimpleNamespace(ok=True, payload={})

        handler.agent_client = Client()
        controller = CronController(
            store=store, scheduler=SimpleNamespace(reload=AsyncMock())
        )
        handler._cron_controller = controller

        async def ack(*, command_id, data, user_id=None):
            envelope = e2a_from_agent_fields(
                request_id=command_id, channel_id="", req_method=ReqMethod.CRON_COMMAND_ACK,
                params={"command_id": command_id, "data": data}, user_id=user_id,
            )
            connection.send(json.dumps(envelope.to_dict()))

        handler._push_cron_command_ack = ack
        connection.send("ready")
        while True:
            raw = connection.recv()
            if raw == "stop":
                return
            wire = json.loads(raw)
            chunk = parse_agent_server_wire_chunk(wire)
            await handler._handle_cron_push_payload(
                payload=chunk.payload, request_id=chunk.request_id, channel_id="",
                session_id=None, metadata=None,
                user_id=wire["metadata"].get("_jiuwenswarm_cron_owner_user_id"),
            )
    try:
        asyncio.run(run())
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_remote_query_does_not_read_agent_store(tmp_path, monkeypatch):
    from jiuwenswarm.agents.harness.common.tools.cron import cron_tools
    from jiuwenswarm.agents.harness.common.tools.cron.cron_runtime import query_authoritative_cron_job
    from jiuwenswarm.runtime.host_services import install_runtime_push_handler, restore_runtime_push_handler
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
    from jiuwenswarm.common.e2a.models import E2AEnvelope
    from jiuwenswarm.server.gateway_push.wire import build_server_push_wire

    gateway_dir, agent_dir = tmp_path / "gateway", tmp_path / "agent"
    gateway_dir.mkdir()
    agent_dir.mkdir()
    monkeypatch.setattr(cron_tools, "get_cron_jobs_path", lambda: agent_dir / "jobs.json")
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_gateway_process, args=(child, str(gateway_dir)))
    process.start()
    child.close()
    server = object.__new__(AgentWebSocketServer)
    ws = SimpleNamespace(send=AsyncMock())

    async def receive():
        assert await asyncio.to_thread(parent.poll, 30), "Gateway did not respond"
        return parent.recv()

    async def push(message):
        parent.send(json.dumps(build_server_push_wire(message)))
        request = e2a_to_agent_request(E2AEnvelope.from_dict(json.loads(await receive())))
        await server._handle_gateway_cron_callback(ws, request, asyncio.Lock())
        return True

    previous = install_runtime_push_handler(push)
    try:
        assert await receive() == "ready"
        job = await query_authoritative_cron_job("remote", user_id="alice")
        assert job["id"] == "remote"
        with pytest.raises(RuntimeError, match="forbidden"):
            await query_authoritative_cron_job("remote", user_id="bob")
        assert await query_authoritative_cron_job("missing", user_id="alice") is None
        assert not (agent_dir / "jobs.json").exists()
    finally:
        restore_runtime_push_handler(push, previous)
        if process.is_alive():
            parent.send("stop")
        await asyncio.to_thread(process.join, 5)
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, 5)
        parent.close()
    assert process.exitcode == 0
