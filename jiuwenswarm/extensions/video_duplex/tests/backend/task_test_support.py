"""Shared transports, model proposals and bounded waits for duplex tests."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock


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
