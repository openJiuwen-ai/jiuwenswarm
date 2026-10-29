# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""session.delete must stop the session's in-flight round, not just erase records.

Regression guard for TC-SESSION_DELETE-006: a zombie round used to keep running
after delete and re-persisted the session dir (session.list resurrection, chat
stream never ending). The delete handler now first cancels the round via the
same chat.interrupt cancellation routing; failed cancellation preserves records
so the user can retry.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server import agent_ws_server as aws


class _SpyAgent:
    def __init__(self, raise_exc: bool = False) -> None:
        self.requests: list[AgentRequest] = []
        self._raise = raise_exc

    async def process_message(self, request: AgentRequest):
        self.requests.append(request)
        if self._raise:
            raise RuntimeError("cancel exploded")
        return None


class _Manager:
    def __init__(self, agent) -> None:
        self._agent = agent
        self.lookup_calls: list[tuple[str, str]] = []

    def get_agent_for_session(self, channel_id, session_id):
        self.lookup_calls.append((channel_id, session_id))
        return self._agent


class _TeamManager:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def delete_session_runtime(self, session_id, *, reason=""):
        self.calls.append(session_id)
        return True


def _make_server(tmp_root: Path, target: str, agent, is_team: bool = False):
    server = aws.AgentWebSocketServer.__new__(aws.AgentWebSocketServer)
    server._agent_manager = _Manager(agent)

    async def _no_checkpointer_guard(request):
        return None

    server._ensure_persistent_checkpointer_response = _no_checkpointer_guard
    server._is_team_metadata_mode = lambda metadata: is_team
    return server


@pytest.fixture
def wiring(monkeypatch, tmp_path):
    """Patch every side-effecting collaborator of _handle_session_delete."""
    session_dir = tmp_path / "sessions" / "desktop_target"
    session_dir.mkdir(parents=True)

    import jiuwenswarm.server.runtime.session.session_history as sh
    import jiuwenswarm.server.runtime.session.session_metadata as sm
    import jiuwenswarm.server.runtime.session.kv_cache_product_hooks as kv
    import openjiuwen.core.runner as runner_core
    import openjiuwen.core.sys_operation.shell_process_registry as shell_reg
    import jiuwenswarm.agents.harness.team as team_mod

    monkeypatch.setattr(sh, "resolve_session_dir",
                        lambda sid, sessions_root=None: (session_dir, None))
    monkeypatch.setattr(sm, "get_session_metadata", lambda sid: {})
    evicted: list[str] = []

    async def _evict(session_id=None, agent_manager=None, channel_id=None):
        evicted.append(session_id)

    monkeypatch.setattr(kv, "evict_plan_session", _evict)
    released: list[str] = []

    async def _release(session_id, force=False):
        released.append(session_id)

    monkeypatch.setattr(runner_core.Runner, "release", staticmethod(_release))
    killed: list[str] = []
    monkeypatch.setattr(
        shell_reg, "kill_shell_processes_for_session_tree",
        lambda sid: killed.append(sid) or 0,
    )
    sent: list[dict] = []

    async def _send(ws, payload):
        sent.append(payload)

    monkeypatch.setattr(aws, "send_wire_payload", _send)
    monkeypatch.setattr(aws, "remove_session_metadata_cache", lambda sid: None)
    tm = _TeamManager()
    monkeypatch.setattr(team_mod, "get_team_manager", lambda channel: tm)

    yield session_dir, evicted, released, killed, sent, tm


def _request(target: str) -> AgentRequest:
    return AgentRequest(
        request_id="rq-del",
        channel_id="desktop",
        session_id=target,
        req_method=ReqMethod.SESSION_DELETE,
        params={"session_id": target},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("directory_exists", [True, False])
async def test_delete_cancels_inflight_round_before_removing_records(wiring, directory_exists):
    session_dir, evicted, released, killed, sent, _tm = wiring
    target = session_dir.name
    spy = _SpyAgent()
    server = _make_server(session_dir.parent, target, spy)
    if not directory_exists:
        session_dir.rmdir()

    await server._handle_session_delete(None, _request(target), asyncio.Lock())

    # 1) cancel routed through chat.interrupt cancellation, before the rest
    assert len(spy.requests) == 1
    req = spy.requests[0]
    assert req.req_method == ReqMethod.CHAT_CANCEL
    assert req.params.get("intent") == "cancel"
    assert req.session_id == target
    # 2) original cleanup still happened, in-order after cancel
    assert evicted == [target]
    assert killed == [target]
    assert released == [target]
    assert not session_dir.exists(), "session dir must be removed"
    # 3) success frame returned
    assert sent and any("succeeded" == f.get("status") for f in sent)


@pytest.mark.asyncio
async def test_delete_without_live_agent_skips_cancel(wiring):
    session_dir, _ev, _rel, _kill, sent, _tm = wiring
    target = session_dir.name
    server = _make_server(session_dir.parent, target, None)

    await server._handle_session_delete(None, _request(target), asyncio.Lock())

    assert server._agent_manager.lookup_calls == [("desktop", target)]
    assert not session_dir.exists()
    assert sent and any(f.get("status") == "succeeded" for f in sent)


@pytest.mark.asyncio
async def test_team_session_uses_team_teardown_not_new_cancel(wiring):
    session_dir, evicted, _rel, _kill, _sent, tm = wiring
    target = session_dir.name
    spy = _SpyAgent()
    server = _make_server(session_dir.parent, target, spy, is_team=True)

    await server._handle_session_delete(None, _request(target), asyncio.Lock())

    # team path: no extra round cancel, no plan evict; team runtime teardown runs
    assert spy.requests == []
    assert evicted == []
    assert tm.calls == [target]
    assert not session_dir.exists()


@pytest.mark.asyncio
async def test_delete_cancels_before_waiting_for_checkpointer(wiring):
    session_dir, *_ = wiring
    spy = _SpyAgent()
    server = _make_server(session_dir.parent, session_dir.name, spy)
    entered, release = asyncio.Event(), asyncio.Event()

    async def checkpointer(_):
        entered.set()
        await release.wait()
        return None

    server._ensure_persistent_checkpointer_response = checkpointer
    task = asyncio.create_task(server._handle_session_delete(None, _request(session_dir.name), asyncio.Lock()))
    try:
        await entered.wait()
        assert spy.requests, 'runtime cancellation must precede checkpoint initialization'
    finally:
        release.set()
        await task


async def _try_chat(server, target):
    server._handle_stream_impl = AsyncMock()
    await server._handle_stream(None, AgentRequest(
        request_id="next-round", channel_id="desktop", session_id=target,
        req_method=ReqMethod.CHAT_SEND, params={"query": "hello"}, is_stream=True,
    ), asyncio.Lock())
    return server._handle_stream_impl.await_count > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel_failed", "checkpoint_error", "reply_error"])
async def test_failed_delete_does_not_permanently_block_later_chat(wiring, monkeypatch, failure):
    session_dir, _evicted, _released, _killed, sent, _manager = wiring
    target = session_dir.name
    server = _make_server(session_dir.parent, target, _SpyAgent(raise_exc=failure == "cancel_failed"))
    if failure == "checkpoint_error":
        server._ensure_persistent_checkpointer_response = AsyncMock(side_effect=RuntimeError("checkpoint failed"))
    if failure == "reply_error":
        send = aws.send_wire_payload
        async def send_once(*args):
            monkeypatch.setattr(aws, "send_wire_payload", send)
            raise RuntimeError("reply failed")
        monkeypatch.setattr(aws, "send_wire_payload", send_once)
    if failure == "cancel_failed":
        await server._handle_session_delete(None, _request(target), asyncio.Lock())
        assert session_dir.exists(), "failed cancellation must preserve recoverable records"
        assert sent[-1]["status"] == "failed"
        assert sent[-1]["body"]["details"]["code"] == "DELETE_FAILED"
    else:
        with pytest.raises(RuntimeError):
            await server._handle_session_delete(None, _request(target), asyncio.Lock())
    assert await _try_chat(server, target), "completed failed deletion must not leave a permanent BUSY marker"


@pytest.mark.asyncio
async def test_overlapping_deletes_keep_chat_blocked_until_last_cleanup(wiring):
    session_dir, *_ = wiring
    target = session_dir.name
    server = _make_server(session_dir.parent, target, None)
    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    calls = 0
    async def cleanup(**_):
        nonlocal calls
        index = calls
        calls += 1
        entered[index].set()
        await release[index].wait()
    server._agent_manager.cleanup_session_runtime = cleanup
    first = asyncio.create_task(server._handle_session_delete(None, _request(target), asyncio.Lock()))
    second = None
    try:
        await asyncio.wait_for(entered[0].wait(), 1)
        second = asyncio.create_task(server._handle_session_delete(None, _request(target), asyncio.Lock()))
        await asyncio.sleep(0)
        release[0].set()
        await asyncio.wait_for(entered[1].wait(), 1)
        await first
        assert not await _try_chat(server, target), "one response must not unlock another active deletion"
        release[1].set()
        await second
        assert await _try_chat(server, target)
    finally:
        for event in release: event.set()
        await asyncio.gather(*[task for task in (first, second) if task is not None], return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_delete_keeps_its_entire_cleanup_then_unlocks(wiring):
    session_dir, *_ = wiring
    target = session_dir.name
    server = _make_server(session_dir.parent, target, None)
    entered, release = asyncio.Event(), asyncio.Event()
    async def cleanup(**_):
        entered.set()
        await release.wait()
    server._agent_manager.cleanup_session_runtime = cleanup
    task = asyncio.create_task(server._handle_session_delete(None, _request(target), asyncio.Lock()))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert not await _try_chat(server, target)
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert not session_dir.exists(), "RPC cancellation must not abandon later record cleanup"
        assert await _try_chat(server, target)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_delete_timeout_keeps_busy_only_while_stream_shutdown_is_running(wiring):
    session_dir, *_ = wiring
    target = session_dir.name
    server = _make_server(session_dir.parent, target, None)
    server._stream_cancel_timeout = 0.02
    entered, release = asyncio.Event(), asyncio.Event()
    async def execute(*_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await release.wait()
    server._handle_stream_impl = execute
    owner = asyncio.create_task(server._handle_stream(None, AgentRequest(
        request_id="old-round", channel_id="desktop", session_id=target,
        req_method=ReqMethod.CHAT_SEND, params={"query": "hello"}, is_stream=True,
    ), asyncio.Lock()))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await server._handle_session_delete(None, _request(target), asyncio.Lock())
        assert not owner.done()
        assert not await _try_chat(server, target)
        release.set()
        await asyncio.gather(owner, return_exceptions=True)
        await asyncio.sleep(0)
        assert await _try_chat(server, target), "finished background cleanup must release BUSY after an earlier timeout"
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)
