"""In-process transport for the real checkpoint protocol and Gateway writer."""

import uuid
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_chunk
from jiuwenswarm.extensions.video_duplex.backend.tasks import bridge
from jiuwenswarm.server.gateway_push.wire import build_server_push_wire


class LocalCheckpoint:
    def __init__(self, store, task):
        self.gateway = bridge.GatewayTaskEndpoint(store)
        self.identity = {
            **self.gateway.binding(task), "owner": task["owner"],
            "session_id": task["core_session_id"], "request_id": task["request_id"],
        }
        store.update(task["id"], lambda t: t.update(execution_bound=True))

    async def call(self, action, **data):
        return self.gateway.execute({
            **self.identity, **data, "action": action, "command_id": uuid.uuid4().hex,
        })


def install_bridge(monkeypatch, store=None, request=None):
    async def acknowledge(env):
        bridge.resolve_checkpoint_ack(e2a_to_agent_request(env))
        return SimpleNamespace(ok=True, payload={})

    async def push(message):
        wire = build_server_push_wire(message)
        chunk = parse_agent_server_wire_chunk(wire)
        return await bridge.handle_checkpoint_push(
            SimpleNamespace(send_request=acknowledge), chunk, wire.get("session_id")
        )

    monkeypatch.setattr(bridge, "send_runtime_push", push)
    if store is not None:
        endpoint = bridge.GatewayTaskEndpoint(store)
        with store.transaction() as db:
            task = next(t for t in store.rows(db) if t["request_id"] == request.request_id)
        request.params = {"managed_task_binding": endpoint.binding(task)}
        request.user_id = task["owner"] or None
        # Keep this test's Gateway endpoint alive for the request lifetime.
        request.test_gateway_endpoint = endpoint
        return endpoint
    return None


@pytest.fixture(autouse=True)
def empty_task_file_query(monkeypatch):
    # These tests cover execution/provider behavior; remote file lookup has its own tests.
    from jiuwenswarm.extensions.video_duplex.backend import task_adapter
    original = task_adapter.task_agent_query

    async def query(client, method, params, session, owner):
        if method.value == "voice.task.files":
            return {"files": []}
        return await original(client, method, params, session, owner)

    monkeypatch.setattr(task_adapter, "task_agent_query", query)
