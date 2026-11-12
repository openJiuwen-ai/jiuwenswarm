"""In-process transport for the real checkpoint protocol and Gateway writer."""

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agent_gateway.extensions.video_duplex.backend.tasks.bridge import (
    GatewayTaskEndpoint,
    handle_checkpoint_push,
)

from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_chunk
from jiuwenswarm.server.runtime.video_task import bridge
from jiuwenswarm.server.gateway_push.wire import build_server_push_wire


class LocalCheckpoint:
    def __init__(self, store, task):
        self.gateway = GatewayTaskEndpoint(store)
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
        return await handle_checkpoint_push(
            SimpleNamespace(send_request=acknowledge), chunk, wire.get("session_id")
        )

    monkeypatch.setattr(bridge, "send_runtime_push", push)
    if store is not None:
        endpoint = GatewayTaskEndpoint(store)
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


@pytest.fixture(name="routed_task")
def routed_task_fixture(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from agent_gateway.message_handler.message_handler import MessageHandler
    from jiuwenswarm.extensions.video_duplex.backend.tasks import TaskService, TaskStore
    from jiuwenswarm.server.runtime.video_task.server_adapter import VoiceTaskServerAdapter
    store = TaskStore(tmp_path / "gateway" / "tasks.sqlite")
    service = TaskService(store, None)
    service.started = True
    service.kick = Mock()
    task = service.submit("alice", "voice", "create", "Plan a meeting")
    task = store.update(task["id"], lambda t: t.update(status="running", checkpoint_open=True))
    endpoint = GatewayTaskEndpoint(store)
    request = SimpleNamespace(
        channel_id="video_tool", session_id=task["core_session_id"],
        request_id=task["request_id"], user_id="alice",
        params={"managed_task_binding": endpoint.binding(task)},
    )
    frames, acks = [], []

    async def acknowledge(env):
        acks.append(env)
        return await VoiceTaskServerAdapter().handle(e2a_to_agent_request(env))

    handler = SimpleNamespace(
        agent_client=SimpleNamespace(send_request=acknowledge), _stream_sessions={},
        _handle_login_credential_refresh_push=AsyncMock(return_value=False),
        publish_robot_messages=AsyncMock(),
    )
    # The production push boundary now delegates ordering to a separate method.

    async def ordered(wire):
        await MessageHandler._handle_agent_server_push_ordered(handler, wire)  # pylint: disable=protected-access

    handler._handle_agent_server_push_ordered = ordered  # pylint: disable=protected-access

    async def push(message):
        frames.append(message)
        # Exercise the installed Gateway push boundary, including its internal-frame interception.
        await MessageHandler._handle_agent_server_push(  # pylint: disable=protected-access
            handler, build_server_push_wire(message)
        )
        return True

    monkeypatch.setattr(bridge, "send_runtime_push", push)
    server_root = tmp_path / "agent-server"
    server_root.mkdir()
    from jiuwenswarm.common import utils
    monkeypatch.setattr(utils, "get_agent_root_dir", lambda tenant_key=None: server_root)
    yield SimpleNamespace(
        store=store, task=task, endpoint=endpoint, request=request, frames=frames,
        acks=acks, handler=handler, push=push, server_root=server_root,
    )
    endpoint.close()


async def until(predicate, timeout=3, interval=0.005):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(interval)


def tool_proposal(name, arguments):
    return json.dumps({"tool": {"name": name, "arguments": arguments}})


def management_call(instruction="调整任务"):
    from jiuwenswarm.extensions.video_duplex.backend.qwen_omni_tools import parse_qwen_omni_tool_call

    return parse_qwen_omni_tool_call({
        "name": "jiuwen_queue_manage", "call_id": "manage-1",
        "arguments": {"instruction": instruction},
    })


async def run_queue(
    monkeypatch, responses, receipts, *, allow_answers=True, reply_language="match",
    instruction="调整任务", cron=None,
):
    """Exercise real management with controlled model responses and operation receipts."""
    from jiuwenswarm.extensions.video_duplex.backend import queue_manager

    operate = AsyncMock(side_effect=receipts) if isinstance(receipts, list) else AsyncMock(return_value=receipts)
    step = AsyncMock(return_value=responses) if isinstance(responses, str) else AsyncMock(side_effect=responses)
    monkeypatch.setattr(queue_manager, "request_step", step)
    result = await queue_manager.manage_queue(
        SimpleNamespace(operate=operate), "alice", "voice", management_call(instruction),
        instruction, allow_answers=allow_answers, reply_language=reply_language, cron=cron,
    )
    return result, operate, step


def agent_client(payload, *, ok=True):
    """Record unary Agent requests and return the supplied protocol receipt."""
    return SimpleNamespace(send_request=AsyncMock(return_value=SimpleNamespace(ok=ok, payload=payload)))



_NEW_WEBSOCKET = object()



class RecordingChannel:
    """Record Gateway handlers, replies and events without replacing their dispatch."""

    def __init__(self):
        self.handlers, self.registration_options = {}, {}
        self.local_only = set()
        self.responses, self.events, self.dispatched = [], [], []
        self.manager = None

    def register_method(self, method, handler, **options):
        self.handlers[method] = handler
        self.registration_options[method] = options
        if options.get("local_only"):
            self.local_only.add(method)

    async def send_response(self, ws, request_id, **response):
        self.responses.append((request_id, response))

    async def send_event(self, ws, event, payload):
        self.events.append((event, payload))

    async def request(self, method, params, *, request_id="request", ws=_NEW_WEBSOCKET, session_id=None):
        """Call the registered production handler with a fresh connection by default."""
        if ws is _NEW_WEBSOCKET:
            ws = object()
        return await self.handlers[method](ws, request_id, params, session_id)
