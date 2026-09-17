from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.agents.harness.team import kv_cache_hooks as team_kv_cache_hooks
from jiuwenswarm.agents.harness.team.team_manager import TeamManager
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.gateway.channel_manager.tui.tui_connect import (
    CliHandlersBindParams,
    register_cli_handlers,
)
from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import (
    WebHandlersBindParams,
    _register_web_handlers,
)
from jiuwenswarm.server import agent_ws_server as agent_ws_server_module
from jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle import (
    KVCacheLifecycleResult,
)


class _WireWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


class _WebChannel:
    def __init__(self) -> None:
        self.methods: dict[str, object] = {}
        self.responses: list[dict] = []

    def register_method(self, name, handler) -> None:
        self.methods[name] = handler

    def on_connect(self, _handler) -> None:
        return None

    async def send_response(self, _ws, req_id, *, ok, payload=None, error=None, code=None):
        self.responses.append(
            {"id": req_id, "ok": ok, "payload": payload, "error": error, "code": code}
        )


class _TuiChannel:
    def __init__(self) -> None:
        self.local_handlers: dict[str, dict[str, object]] = {}
        self.responses: list[dict] = []

    def register_local_handler(self, path, method, handler) -> None:
        self.local_handlers.setdefault(path, {})[method] = handler

    async def send_response(self, _ws, req_id, *, ok, payload=None, error=None, code=None):
        self.responses.append(
            {"id": req_id, "ok": ok, "payload": payload or {}, "error": error, "code": code}
        )


class _FailingAgentClient:
    server_ready = True

    async def send_request(self, _request):
        raise RuntimeError("agent server unavailable")


class _AgentServer(agent_ws_server_module.AgentWebSocketServer):
    def __init__(self) -> None:
        super().__init__()
        self.team_session_ids: list[str] = []
        self._agent_manager = SimpleNamespace(get_agent_nowait=lambda *_: object())

    async def _ensure_persistent_checkpointer_response(self, _request):
        return None

    async def _find_team_session_ids(self, _team_name: str) -> list[str]:
        return list(self.team_session_ids)


def _wire_response(response, *, response_id):
    return {"response_id": response_id, "payload": response.payload, "ok": response.ok}


@pytest.mark.asyncio
@pytest.mark.parametrize("hook_raises", [False, True])
async def test_plan_agentserver_delete_evicts_self_parent_and_preserves_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    hook_raises: bool,
) -> None:
    sessions_root = tmp_path / "sessions"
    (sessions_root / "plan-root").mkdir(parents=True)
    server = _AgentServer()
    ws = _WireWebSocket()
    evict_calls: list[dict] = []
    release_calls: list[str] = []

    async def fake_evict(**kwargs):
        evict_calls.append(kwargs)
        if hook_raises:
            raise RuntimeError("hook broken")
        return KVCacheLifecycleResult(status="ok")

    async def fake_release(session_id: str) -> None:
        release_calls.append(session_id)

    monkeypatch.setattr(agent_ws_server_module, "get_agent_sessions_dir", lambda: sessions_root)
    monkeypatch.setattr(agent_ws_server_module, "encode_agent_response_for_wire", _wire_response)
    monkeypatch.setattr(agent_ws_server_module, "remove_session_metadata_cache", lambda _sid: None)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda _sid: {"mode": "agent.plan", "channel_id": "web"},
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle.evict_session_kv_cache",
        fake_evict,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle."
        "is_kv_cache_affinity_enabled",
        lambda: True,
    )
    monkeypatch.setattr("openjiuwen.core.runner.Runner.release", fake_release)

    request = AgentRequest(
        request_id="delete-plan",
        channel_id="web",
        req_method=ReqMethod.SESSION_DELETE,
        params={"session_id": "plan-root"},
    )
    await server._handle_session_delete(ws, request, asyncio.Lock())

    assert len(evict_calls) == 1
    assert evict_calls[0]["session_id"] == "plan-root"
    assert evict_calls[0]["parent_session_id"] == "plan-root"
    assert release_calls == ["plan-root"]
    assert not (sessions_root / "plan-root").exists()
    assert ws.sent[-1]["ok"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("hook_raises", [False, True])
@pytest.mark.parametrize("forward_raises", [False, True])
async def test_web_plan_fallback_evicts_root_without_changing_local_delete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    hook_raises: bool,
    forward_raises: bool,
) -> None:
    sessions_root = tmp_path / "sessions"
    (sessions_root / "web-root").mkdir(parents=True)
    channel = _WebChannel()
    calls: list[dict] = []

    async def fake_evict(**kwargs):
        calls.append(kwargs)
        if hook_raises:
            raise RuntimeError("hook broken")
        return KVCacheLifecycleResult(status="ok")

    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_agent_sessions_dir",
        lambda: sessions_root,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda _sid: {"mode": "agent.plan"},
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle.evict_session_kv_cache",
        fake_evict,
    )
    _register_web_handlers(
        WebHandlersBindParams(
            channel=channel,
            agent_client=_FailingAgentClient() if forward_raises else None,
        )
    )

    await channel.methods["session.delete"](
        object(), "delete-web", {"session_id": "web-root"}, "web-root"
    )

    assert calls == [{"session_id": "web-root", "parent_session_id": "web-root"}]
    assert not (sessions_root / "web-root").exists()
    assert channel.responses[-1] == {
        "id": "delete-web",
        "ok": True,
        "payload": {"session_id": "web-root"},
        "error": None,
        "code": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("hook_raises", [False, True])
@pytest.mark.parametrize("forward_raises", [False, True])
async def test_tui_plan_fallback_evicts_root_without_changing_local_delete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    hook_raises: bool,
    forward_raises: bool,
) -> None:
    sessions_root = tmp_path / "sessions"
    (sessions_root / "tui-root").mkdir(parents=True)
    channel = _TuiChannel()
    calls: list[dict] = []

    async def fake_evict(**kwargs):
        calls.append(kwargs)
        if hook_raises:
            raise RuntimeError("hook broken")
        return KVCacheLifecycleResult(status="ok")

    monkeypatch.setattr("jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: sessions_root)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda _sid: {"mode": "agent.plan"},
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle.evict_session_kv_cache",
        fake_evict,
    )
    register_cli_handlers(
        CliHandlersBindParams(
            channel=channel,
            agent_client=_FailingAgentClient() if forward_raises else None,
            path="/tui",
        )
    )

    await channel.local_handlers["/tui"]["session.delete"](
        object(), "delete-tui", {"session_id": "tui-root"}, "tui-root"
    )

    assert calls == [{"session_id": "tui-root", "parent_session_id": "tui-root"}]
    assert not (sessions_root / "tui-root").exists()
    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"] == {"session_id": "tui-root"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled", "supported", "evict_ok", "expected_calls"),
    [
        (False, True, True, 0),
        (True, False, True, 0),
        (True, True, True, 1),
        (True, True, False, 1),
    ],
)
async def test_web_plan_fallback_uses_lifecycle_gate_without_changing_delete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    enabled: bool,
    supported: bool,
    evict_ok: bool,
    expected_calls: int,
) -> None:
    from jiuwenswarm.server.runtime.session import kv_cache_affinity_lifecycle as lifecycle

    sessions_root = tmp_path / "sessions"
    (sessions_root / "gated-root").mkdir(parents=True)
    channel = _WebChannel()

    class GateModel:
        model_client_config = None

        def __init__(self) -> None:
            self.calls: list[dict] = []

        def supports_kv_cache_affinity(self) -> bool:
            return supported

        async def evict_kvc(self, **kwargs):
            self.calls.append(kwargs)
            return evict_ok

    model = GateModel()
    config = {
        "react": {
            "kv_cache_affinity_config": {
                "enable_kv_cache_affinity": enabled,
            }
        }
    }
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_agent_sessions_dir",
        lambda: sessions_root,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda _sid: {"mode": "agent.plan"},
    )
    monkeypatch.setattr(lifecycle, "get_config", lambda: config)
    monkeypatch.setattr(lifecycle, "resolve_kv_cache_affinity_model", lambda **_: model)
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=None))

    await channel.methods["session.delete"](
        object(), "delete-gated", {"session_id": "gated-root"}, "gated-root"
    )

    assert len(model.calls) == expected_calls
    if model.calls:
        assert model.calls[0]["target"] == "session"
        assert model.calls[0]["session_id"] == "gated-root"
        assert model.calls[0]["parent_session_id"] == "gated-root"
    assert not (sessions_root / "gated-root").exists()
    assert channel.responses[-1]["ok"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("channel_kind", ["web", "tui"])
async def test_team_fallback_remains_agent_unavailable_without_evict_or_delete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    channel_kind: str,
) -> None:
    sessions_root = tmp_path / "sessions"
    (sessions_root / "team-root").mkdir(parents=True)
    calls: list[dict] = []

    async def fake_evict(**kwargs):
        calls.append(kwargs)
        return KVCacheLifecycleResult(status="ok")

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda _sid: {"mode": "team"},
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle.evict_session_kv_cache",
        fake_evict,
    )

    if channel_kind == "web":
        channel = _WebChannel()
        monkeypatch.setattr(
            "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_agent_sessions_dir",
            lambda: sessions_root,
        )
        _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=None))
        handler = channel.methods["session.delete"]
    else:
        channel = _TuiChannel()
        monkeypatch.setattr("jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: sessions_root)
        register_cli_handlers(CliHandlersBindParams(channel=channel, agent_client=None, path="/tui"))
        handler = channel.local_handlers["/tui"]["session.delete"]

    await handler(object(), "delete-team", {"session_id": "team-root"}, "team-root")

    assert calls == []
    assert (sessions_root / "team-root").exists()
    assert channel.responses[-1]["ok"] is False
    assert channel.responses[-1]["code"] == "AGENT_UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize("channel_kind", ["web", "tui"])
async def test_missing_plan_fallback_preserves_not_found_without_evict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    channel_kind: str,
) -> None:
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    calls: list[dict] = []

    async def fake_evict(**kwargs):
        calls.append(kwargs)
        return KVCacheLifecycleResult(status="ok")

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda _sid: {"mode": "agent.plan"},
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle.evict_session_kv_cache",
        fake_evict,
    )

    if channel_kind == "web":
        channel = _WebChannel()
        monkeypatch.setattr(
            "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_agent_sessions_dir",
            lambda: sessions_root,
        )
        _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=None))
        handler = channel.methods["session.delete"]
    else:
        channel = _TuiChannel()
        monkeypatch.setattr("jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: sessions_root)
        register_cli_handlers(CliHandlersBindParams(channel=channel, agent_client=None, path="/tui"))
        handler = channel.local_handlers["/tui"]["session.delete"]

    await handler(object(), "delete-missing", {"session_id": "missing-root"}, "missing-root")

    assert calls == []
    assert channel.responses[-1]["ok"] is False
    assert channel.responses[-1]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_team_session_delete_delegates_terminal_kvc_to_agent_core(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    sessions_root = tmp_path / "sessions"
    (sessions_root / "team-root").mkdir(parents=True)
    manager = TeamManager()
    server = _AgentServer()
    ws = _WireWebSocket()
    calls: list[dict] = []
    deleted_teams: list[dict] = []

    async def fake_evict(**kwargs):
        calls.append(kwargs)
        return KVCacheLifecycleResult(status="ok")

    async def fake_delete_agent_team(**kwargs):
        deleted_teams.append(kwargs)
        return True

    monkeypatch.setattr(manager, "_resolve_delete_session_team_name", lambda _sid: "demo-team")
    monkeypatch.setattr(manager, "stop_session_runtime", AsyncMock(return_value=True))
    monkeypatch.setattr(agent_ws_server_module, "get_agent_sessions_dir", lambda: sessions_root)
    monkeypatch.setattr(agent_ws_server_module, "encode_agent_response_for_wire", _wire_response)
    monkeypatch.setattr(agent_ws_server_module, "remove_session_metadata_cache", lambda _sid: None)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda _sid: {"mode": "team", "channel_id": "web"},
    )
    monkeypatch.setattr("jiuwenswarm.agents.harness.team.get_team_manager", lambda _cid: manager)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle.evict_session_kv_cache",
        fake_evict,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle.is_kv_cache_affinity_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.Runner.delete_agent_team",
        fake_delete_agent_team,
    )

    request = AgentRequest(
        request_id="delete-team-session",
        channel_id="web",
        req_method=ReqMethod.SESSION_DELETE,
        params={"session_id": "team-root"},
    )
    await server._handle_session_delete(ws, request, asyncio.Lock())

    assert calls == []
    assert deleted_teams == [
        {"team_name": "demo-team", "session_ids": ["team-root"], "force": True}
    ]
    assert not (sessions_root / "team-root").exists()
    assert ws.sent[-1]["ok"] is True


@pytest.mark.asyncio
async def test_team_delete_delegates_terminal_kvc_to_agent_core(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    sessions_root = tmp_path / "sessions"
    for session_id in ("team-root-1", "team-root-2"):
        (sessions_root / session_id).mkdir(parents=True)
    server = _AgentServer()
    server.team_session_ids = ["team-root-1", "team-root-2"]
    ws = _WireWebSocket()
    calls: list[dict] = []
    stop_calls: list[str] = []

    async def fake_evict(**kwargs):
        calls.append(kwargs)
        return KVCacheLifecycleResult(status="ok")

    async def fake_stop(session_id: str, reason: str = "", **kwargs):
        stop_calls.append(session_id)
        assert kwargs == {"stop_runner": False}
        return True

    monkeypatch.setattr(agent_ws_server_module, "get_agent_sessions_dir", lambda: sessions_root)
    monkeypatch.setattr(agent_ws_server_module, "encode_agent_response_for_wire", _wire_response)
    monkeypatch.setattr(agent_ws_server_module, "remove_session_metadata_cache", lambda _sid: None)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle.evict_session_kv_cache",
        fake_evict,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle.is_kv_cache_affinity_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.stop_team_session_runtime_across_managers",
        fake_stop,
    )
    monkeypatch.setattr(
        "openjiuwen.core.runner.Runner.delete_agent_team",
        AsyncMock(return_value=True),
    )

    request = AgentRequest(
        request_id="delete-team",
        channel_id="web",
        req_method=ReqMethod.TEAM_DELETE,
        params={"mode": "team", "team_name": "demo-team"},
    )
    await server._handle_team_delete(ws, request, asyncio.Lock())

    assert calls == []
    assert stop_calls == ["team-root-1", "team-root-2"]
    assert ws.sent[-1]["ok"] is True


@pytest.mark.asyncio
async def test_team_delete_keeps_original_stop_order_when_affinity_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    sessions_root = tmp_path / "sessions"
    (sessions_root / "team-root").mkdir(parents=True)
    server = _AgentServer()
    server.team_session_ids = ["team-root"]
    ws = _WireWebSocket()
    stop_kwargs: list[dict] = []

    async def fake_stop(_session_id: str, reason: str = "", **kwargs):
        stop_kwargs.append(kwargs)
        return True

    monkeypatch.setattr(agent_ws_server_module, "get_agent_sessions_dir", lambda: sessions_root)
    monkeypatch.setattr(agent_ws_server_module, "encode_agent_response_for_wire", _wire_response)
    monkeypatch.setattr(agent_ws_server_module, "remove_session_metadata_cache", lambda _sid: None)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle.is_kv_cache_affinity_enabled",
        lambda: False,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.stop_team_session_runtime_across_managers",
        fake_stop,
    )
    monkeypatch.setattr(
        "openjiuwen.core.runner.Runner.delete_agent_team",
        AsyncMock(return_value=True),
    )

    request = AgentRequest(
        request_id="delete-team",
        channel_id="web",
        req_method=ReqMethod.TEAM_DELETE,
        params={"mode": "team", "team_name": "demo-team"},
    )
    await server._handle_team_delete(ws, request, asyncio.Lock())

    assert stop_kwargs == [{}]
    assert ws.sent[-1]["ok"] is True


@pytest.mark.asyncio
async def test_plain_disconnect_does_not_emit_root_evict(monkeypatch: pytest.MonkeyPatch) -> None:
    server = agent_ws_server_module.AgentWebSocketServer()
    server._agent_manager = SimpleNamespace(
        cancel_all_inflight_work=AsyncMock(return_value=None),
    )
    server._stop_scheduler = AsyncMock(return_value=None)
    calls: list[dict] = []

    async def fake_evict(**kwargs):
        calls.append(kwargs)
        return KVCacheLifecycleResult(status="ok")

    class EmptyWebSocket:
        remote_address = ("127.0.0.1", 10000)

        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, payload: str) -> None:
            self.sent.append(payload)

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle.evict_session_kv_cache",
        fake_evict,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.cancel_all_team_stream_tasks_across_managers",
        AsyncMock(return_value=None),
    )

    await server._connection_handler(EmptyWebSocket())

    assert calls == []


@pytest.mark.asyncio
async def test_team_switch_dispatches_offload_before_baseline_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TeamManager()
    events: list[str] = []

    async def _dispatch(*_args, **_kwargs) -> bool:
        events.append("offload")
        return True

    async def _stop(*_args, **_kwargs) -> bool:
        events.append("baseline-stop")
        return True

    manager._active_team_names["old-session"] = "demo-team"
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.kv_cache_hooks.dispatch_for_session",
        _dispatch,
    )
    monkeypatch.setattr(manager, "_is_distributed_mode", lambda _cfg: True)
    monkeypatch.setattr(manager, "stop_session_runtime", _stop)

    await manager.prepare_session_switch(
        "new-session",
        reason="test: ",
        previous_session_id="old-session",
    )

    assert events == ["offload", "baseline-stop"]


@pytest.mark.asyncio
async def test_local_team_switch_offloads_previous_without_stopping_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TeamManager()
    events: list[str] = []

    async def _dispatch(*_args, **_kwargs) -> bool:
        events.append("offload")
        return True

    async def _stop(*_args, **_kwargs) -> bool:
        events.append("baseline-stop")
        return True

    manager._active_team_names["old-session"] = "demo-team"
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.kv_cache_hooks.dispatch_for_session",
        _dispatch,
    )
    monkeypatch.setattr(manager, "_is_distributed_mode", lambda _cfg: False)
    monkeypatch.setattr(manager, "stop_session_runtime", _stop)

    await manager.prepare_session_switch(
        "new-session",
        reason="test: ",
        previous_session_id="old-session",
    )

    assert events == ["offload"]


@pytest.mark.asyncio
async def test_team_session_delete_keeps_baseline_stop_before_runner_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TeamManager()
    events: list[str] = []

    async def _stop(*_args, **_kwargs) -> bool:
        events.append("baseline-stop")
        return True

    async def _delete(**_kwargs) -> bool:
        events.append("baseline-delete")
        return True

    monkeypatch.setattr(manager, "_resolve_delete_session_team_name", lambda _sid: "demo-team")
    monkeypatch.setattr(manager, "stop_session_runtime", _stop)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.Runner.delete_agent_team",
        _delete,
    )

    assert await manager.delete_session_runtime("team-session", reason="test: ")
    assert events == ["baseline-stop", "baseline-delete"]


@pytest.mark.asyncio
@pytest.mark.parametrize("gate_raises", [False, True])
async def test_team_kvc_owner_hook_contains_disabled_or_failed_affinity_gate(
    monkeypatch: pytest.MonkeyPatch,
    gate_raises: bool,
) -> None:
    manager = TeamManager()
    dispatch = AsyncMock(return_value=True)
    binding_lookup = Mock(
        side_effect=AssertionError("disabled affinity must not resolve Team binding")
    )

    def affinity_gate() -> bool:
        if gate_raises:
            raise RuntimeError("config broken")
        return False

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle."
        "is_kv_cache_affinity_enabled",
        affinity_gate,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.kv_cache_hooks.Runner."
        "dispatch_agent_team_kv_cache",
        dispatch,
    )
    monkeypatch.setattr(manager, "_lookup_session_team_name", binding_lookup)

    assert await manager.offload_session_kv_cache("team-session", reason="test") is False
    assert await manager.prefetch_session_kv_cache("team-session", reason="test") is False
    assert (
        await team_kv_cache_hooks.dispatch_signal(
            "offload",
            session_id="team-session",
            team_name="demo-team",
            reason="test",
        )
        is False
    )
    binding_lookup.assert_not_called()
    dispatch.assert_not_awaited()
