import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer


def request(sid="cancel-target", *, acknowledge=False, method=ReqMethod.CHAT_SEND):
    return AgentRequest(request_id=f"request-{sid}", channel_id="desktop", session_id=sid,
                        req_method=method, params={"query": "OK"},
                        metadata={"require_admission_ack": acknowledge})


def server():
    value = object.__new__(AgentWebSocketServer)
    value._active_desktop_chat_streams = {}
    value._agent_manager = SimpleNamespace(get_agent_for_session=lambda *_: None)
    value._stream_cancel_timeout = 0.03
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["admission_ack", "adapter_initialization"])
async def test_delete_cancels_preparing_owner_without_cached_adapter(stage):
    value = server()
    entered = asyncio.Event()

    async def wait_for_cancel(*_):
        entered.set()
        await asyncio.Event().wait()

    value._handle_stream_impl = wait_for_cancel
    req = request(acknowledge=stage == "admission_ack")
    ws = SimpleNamespace(send=wait_for_cancel)
    owner = asyncio.create_task(value._handle_stream(ws, req, asyncio.Lock()))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert value._desktop_chat_capacity(None)["activeSessions"] == 1
        assert await value._cancel_inflight_round_for_delete(req, req.session_id) is True
        assert owner.done()
        assert value._desktop_chat_capacity(None)["activeSessions"] == 0
    finally:
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["stop", "delete"])
async def test_repeated_stop_or_delete_retains_cleanup_and_capacity(operation):
    value = server()
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def execute(*_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    value._handle_stream_impl = execute
    value._bind_channel_conn = lambda *_: None
    value._agent_manager.get_agent_nowait = lambda *_, **__: None
    req = request()
    ws = SimpleNamespace(send=AsyncMock())

    async def stop_or_delete():
        if operation == "delete":
            return await value._cancel_inflight_round_for_delete(req, req.session_id)
        await value._handle_message(ws, json.dumps({"request_id": "stop", "channel": "desktop",
            "method": "chat.interrupt", "session_id": req.session_id,
            "params": {"session_id": req.session_id, "intent": "cancel"}, "is_stream": False}), asyncio.Lock())
        frame = json.loads(ws.send.call_args_list[-1].args[0])
        assert frame["body"]["details"]["code"] == "CANCEL_FAILED"
        return frame["status"] == "succeeded"

    owner = asyncio.create_task(value._handle_stream(None, req, asyncio.Lock()))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        for _ in range(2):
            assert await stop_or_delete() is False
            assert cleaning.is_set()
            assert owner.cancelling() == 1
            assert value._desktop_chat_capacity(None)["activeSessions"] == 1
        release.set()
        await asyncio.gather(owner, return_exceptions=True)
        assert value._desktop_chat_capacity(None)["activeSessions"] == 0
    finally:
        release.set()
        if not owner.done():
            owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
async def test_stop_then_delete_share_agent_shutdown_without_repeated_cancel():
    value = server()
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    agent_entered, release_agent = asyncio.Event(), asyncio.Event()

    async def execute(*_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    async def cancel_agent():
        agent_entered.set()
        await release_agent.wait()

    cancel = AsyncMock(side_effect=cancel_agent)
    value._handle_stream_impl = execute
    value._agent_manager.get_agent_for_session = lambda *_: SimpleNamespace(process_message=lambda _: cancel())
    req = request()
    owner = asyncio.create_task(value._handle_stream(None, req, asyncio.Lock()))
    try:
        await entered.wait()
        stop = request(method=ReqMethod.CHAT_CANCEL)
        stop.params = {'request_id': req.request_id}
        first = asyncio.create_task(value._terminate_session_streams(stop, cancel))
        await agent_entered.wait()
        await cleaning.wait()
        second = asyncio.create_task(value._cancel_inflight_round_for_delete(req, req.session_id))
        await asyncio.sleep(0.01)
        assert cancel.await_count == 1
        assert owner.cancelling() == 1
        release.set()
        release_agent.set()
        assert await first is True
        assert await second is True
    finally:
        release.set()
        release_agent.set()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
async def test_heartbeat_stops_while_cancelled_output_cleanup_is_still_pending(monkeypatch):
    from jiuwenswarm.server import agent_ws_server as module
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def output(_):
        entered.set()
        try:
            await asyncio.Event().wait()
            yield
        finally:
            cleaning.set()
            await release.wait()

    monkeypatch.setattr(module, '_STREAM_HEARTBEAT_INTERVAL_SECONDS', 0.01)
    value = server()
    value._is_stateless_method_request = lambda _: True
    value._get_stateless_agent = AsyncMock(return_value=SimpleNamespace(process_message_stream=output))
    value._check_post_process_plan_exit = AsyncMock()
    ws = SimpleNamespace(send=AsyncMock())
    req = request()
    owner = asyncio.create_task(value._handle_stream(ws, req, asyncio.Lock()))
    try:
        await entered.wait()
        assert await value._cancel_inflight_round_for_delete(req, req.session_id) is False
        assert cleaning.is_set()
        count = ws.send.await_count
        await asyncio.sleep(0.04)
        assert ws.send.await_count == count
        assert value._desktop_chat_capacity(None)['activeSessions'] == 1
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)
    assert value._desktop_chat_capacity(None)['activeSessions'] == 0


@pytest.mark.asyncio
async def test_cancel_during_foreground_registration_balances_prewarm_ownership():
    value = server()
    entered, release = asyncio.Event(), asyncio.Event()
    state = {'owners': 0}

    async def begin():
        state['owners'] += 1
        entered.set()
        await release.wait()

    async def end():
        state['owners'] -= 1

    value._agent_manager.begin_foreground_chat = begin
    value._agent_manager.end_foreground_chat = end
    value._handle_stream_impl = AsyncMock()
    req = request()
    task = asyncio.create_task(value._handle_stream(None, req, asyncio.Lock()))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(task, return_exceptions=True)
    assert state['owners'] == 0
    value._handle_stream_impl.assert_not_awaited()
    assert value._desktop_chat_capacity(None)['activeSessions'] == 0


@pytest.mark.asyncio
async def test_normal_completion_does_not_cancel_post_processing_or_prewarm(monkeypatch):
    from jiuwenswarm.common.schema.agent import AgentResponseChunk
    from jiuwenswarm.server import agent_ws_server as module
    from jiuwenswarm.server.request_context import raise_if_request_cancelled
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep
    from jiuwenswarm.server.runtime.agent_warm_pool import AgentWarmPool

    value = server()
    pump_finished = asyncio.Event()
    observations = {}

    def is_cancelled():
        try:
            raise_if_request_cancelled()
        except asyncio.CancelledError:
            return True
        return False

    async def output(req):
        yield AgentResponseChunk(request_id=req.request_id, channel_id=req.channel_id,
                                 payload={"event_type": "chat.final", "content": "OK"}, is_complete=True)

    def observe_prewarm():
        observations["prewarm"] = is_cancelled()
        pump_finished.set()

    async def observe_post_processing(*_):
        observations["post_processing"] = is_cancelled()

    pool = AgentWarmPool(SimpleNamespace(), enabled=True, background_cooldown_seconds=0)
    monkeypatch.setattr(pool, "_pump_background_locked", observe_prewarm)
    value._agent_manager.begin_foreground_chat = pool.begin_foreground
    value._agent_manager.end_foreground_chat = pool.end_foreground
    value._is_stateless_method_request = lambda _: True
    value._is_readonly_goal_get_request = lambda _: False
    value._get_stateless_agent = AsyncMock(return_value=SimpleNamespace(process_message_stream=output))
    value._check_post_process_plan_exit = observe_post_processing
    monkeypatch.setattr(interface_deep, "ensure_persistent_checkpointer", AsyncMock())
    monkeypatch.setattr(module, "_inject_session_xiaoyi_routing", lambda _: None)
    monkeypatch.setattr(module, "send_wire_payload", AsyncMock(return_value=True))
    await value._handle_stream(None, request("normal-completion"), asyncio.Lock())
    await asyncio.wait_for(pump_finished.wait(), 1)
    assert observations == {"post_processing": False, "prewarm": False}
