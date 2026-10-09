# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""session.delete must stop the session's in-flight round, not just erase records.

Regression guard for TC-SESSION_DELETE-006: a zombie round used to keep running
after delete and re-persisted the session dir (session.list resurrection, chat
stream never ending). The delete handler now first cancels the round via the
same chat.interrupt cancellation routing; cancel failure must never block the
delete itself.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

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
async def test_delete_cancels_inflight_round_before_removing_records(wiring):
    session_dir, evicted, released, killed, sent, _tm = wiring
    target = session_dir.name
    spy = _SpyAgent()
    server = _make_server(session_dir.parent, target, spy)

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
async def test_cancel_failure_does_not_block_delete(wiring):
    session_dir, _evicted, _released, _killed, sent, _tm = wiring
    target = session_dir.name
    spy = _SpyAgent(raise_exc=True)
    server = _make_server(session_dir.parent, target, spy)

    await server._handle_session_delete(None, _request(target), asyncio.Lock())

    assert len(spy.requests) == 1
    assert not session_dir.exists()
    assert sent and any(f.get("status") == "succeeded" for f in sent)


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
