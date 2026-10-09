"""_resolve_rewind_agent must prefer the session-scoped DeepAgent used by chat."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

AgentWebSocketServer = None


@pytest.fixture
def server_cls():
    from jiuwenswarm.server.handlers import session as session_handlers

    return session_handlers


@pytest.fixture(autouse=True)
def _ws_server_cls():
    global AgentWebSocketServer
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer as _cls

    AgentWebSocketServer = _cls
    return _cls


def _services(server):
    """把裸 server 包成业务层真正拿到的门面。

    handler 见到的 ``ctx.services`` 一律是 ``AgentServerServices``：它只认
    ``SERVICE_MEMBERS`` 登记的**公有名**，并在内部映射到 server 上的私有属性。
    直接把裸 server 当 ``ctx.services`` 传会绕开这层映射，测出来的是假的接口。
    """
    from jiuwenswarm.server.context import AgentServerServices

    return AgentServerServices(server)


def _returning(value):
    """Build an async stand-in for the wrapper's ``ensure_instance``.

    The root DeepAgent is built on demand now, so the rewind path awaits it
    instead of reading a plain accessor.
    """

    async def _ensure_instance():
        return value

    return _ensure_instance


def test_resolve_rewind_agent_prefers_session_scoped_instance(server_cls):
    root_deep = MagicMock(name="root_deep")
    root_deep.react_agent = MagicMock(name="root_react")

    session_deep = MagicMock(name="session_deep")
    session_deep.react_agent = MagicMock(name="session_react")

    session_adapter = SimpleNamespace(_instance=session_deep)
    root_adapter = SimpleNamespace(
        _is_session_scoped_adapter=False,
        _instance=root_deep,
        _get_cached_session_adapter=lambda sid: session_adapter if sid == "sess-1" else None,
        apply_sandbox_runtime_patch=lambda *a, **k: None,
    )

    agent = SimpleNamespace(_adapter=root_adapter)
    agent.get_instance = lambda: root_deep
    agent.ensure_instance = _returning(root_deep)

    server = MagicMock()
    server._agent_manager = MagicMock()
    server._agent_manager.get_agent_for_session_nowait.return_value = agent
    server._agent_manager.get_agent_nowait.return_value = agent
    server._resolve_adapter = AgentWebSocketServer._resolve_adapter

    ctx = SimpleNamespace(services=_services(server))
    pair = asyncio.run(server_cls._resolve_rewind_agent(ctx, "tui", session_id="sess-1"))
    assert pair is not None
    deep, react = pair
    assert deep is session_deep
    assert react is session_deep.react_agent


def test_resolve_rewind_agent_falls_back_to_root_when_no_session_adapter(server_cls):
    root_deep = MagicMock(name="root_deep")
    root_deep.react_agent = MagicMock(name="root_react")

    root_adapter = SimpleNamespace(
        _is_session_scoped_adapter=False,
        _instance=root_deep,
        _get_cached_session_adapter=lambda sid: None,
        apply_sandbox_runtime_patch=lambda *a, **k: None,
    )

    agent = SimpleNamespace(_adapter=root_adapter)
    agent.get_instance = lambda: root_deep
    agent.ensure_instance = _returning(root_deep)

    server = MagicMock()
    server._agent_manager = MagicMock()
    server._agent_manager.get_agent_for_session_nowait.return_value = None
    server._agent_manager.get_agent_nowait.return_value = agent
    server._resolve_adapter = AgentWebSocketServer._resolve_adapter

    ctx = SimpleNamespace(services=_services(server))
    pair = asyncio.run(server_cls._resolve_rewind_agent(ctx, "tui", session_id="missing"))
    assert pair is not None
    deep, _react = pair
    assert deep is root_deep


def test_resolve_rewind_agent_finds_session_owner_across_cached_roots(server_cls):
    from jiuwenswarm.server.runtime.agent_manager import AgentManager

    wrong_root = MagicMock(name="wrong_root")
    wrong_root.react_agent = MagicMock(name="wrong_react")
    wrong_adapter = SimpleNamespace(
        _is_session_scoped_adapter=False,
        _get_cached_session_adapter=lambda _sid: None,
        apply_sandbox_runtime_patch=lambda *a, **k: None,
    )
    wrong_agent = SimpleNamespace(
        _adapter=wrong_adapter,
        _jiuwenswarm_agent_mode="agent",
        _jiuwenswarm_agent_sub_mode="",
        _jiuwenswarm_agent_project_dir="",
        has_session_runtime=lambda _sid: False,
        ensure_instance=_returning(wrong_root),
    )

    session_deep = MagicMock(name="session_deep")
    session_deep.react_agent = MagicMock(name="session_react")
    session_adapter = SimpleNamespace(_instance=session_deep)
    code_adapter = SimpleNamespace(
        _is_session_scoped_adapter=False,
        _get_cached_session_adapter=(
            lambda sid: session_adapter if sid == "sess-code" else None
        ),
        apply_sandbox_runtime_patch=lambda *a, **k: None,
    )
    code_agent = SimpleNamespace(
        _adapter=code_adapter,
        _jiuwenswarm_agent_mode="code",
        _jiuwenswarm_agent_sub_mode="normal",
        _jiuwenswarm_agent_project_dir="D:/workspace",
        has_session_runtime=lambda sid: sid == "sess-code",
        ensure_instance=_returning(session_deep),
    )

    manager = AgentManager()
    manager.agents["tui"] = {
        "agent::": wrong_agent,
        "code:normal:D:/workspace": code_agent,
    }
    server = SimpleNamespace(
        _agent_manager=manager,
        _resolve_adapter=server_cls._resolve_adapter,
    )
    ctx = SimpleNamespace(services=_services(server))

    pair = asyncio.run(
        server_cls._resolve_rewind_agent(ctx, "tui", session_id="sess-code")
    )
    assert pair is not None
    deep, react = pair
    assert deep is session_deep
    assert react is session_deep.react_agent
