"""Runtime rewind resolution prefers the Session-scoped DeepAgent used by chat."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def runtime_cls():
    from jiuwenswarm.runtime import AgentRuntime

    return AgentRuntime


def _returning(value):
    """Build an async stand-in for the wrapper's ``ensure_instance``.

    The root DeepAgent is built on demand now, so the rewind path awaits it
    instead of reading a plain accessor.
    """

    async def _ensure_instance():
        return value

    return _ensure_instance


def test_resolve_rewind_agent_prefers_session_scoped_instance(runtime_cls):
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

    runtime = runtime_cls.__new__(runtime_cls)
    runtime._agent_manager = MagicMock()
    runtime._agent_manager.get_agent_for_session_nowait.return_value = agent
    runtime._agent_manager.get_agent_nowait.return_value = agent

    pair = asyncio.run(
        runtime._resolve_rewind_context_agent(
            channel_id="tui",
            session_id="sess-1",
            descriptor=None,
            ensure=False,
        )
    )
    assert pair is not None
    deep, react = pair
    assert deep is session_deep
    assert react is session_deep.react_agent


def test_resolve_rewind_agent_falls_back_to_root_when_no_session_adapter(runtime_cls):
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

    runtime = runtime_cls.__new__(runtime_cls)
    runtime._agent_manager = MagicMock()
    runtime._agent_manager.get_agent_for_session_nowait.return_value = None
    runtime._agent_manager.get_agent_nowait.return_value = agent

    pair = asyncio.run(
        runtime._resolve_rewind_context_agent(
            channel_id="tui",
            session_id="missing",
            descriptor=None,
            ensure=False,
        )
    )
    assert pair is not None
    deep, _react = pair
    assert deep is root_deep


def test_resolve_rewind_agent_finds_session_owner_across_cached_roots(runtime_cls):
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
        _get_cached_session_adapter=lambda sid: session_adapter if sid == "sess-code" else None,
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
    runtime = runtime_cls.__new__(runtime_cls)
    runtime._agent_manager = manager

    pair = asyncio.run(
        runtime._resolve_rewind_context_agent(
            channel_id="tui",
            session_id="sess-code",
            descriptor=None,
            ensure=False,
        )
    )
    assert pair is not None
    deep, react = pair
    assert deep is session_deep
    assert react is session_deep.react_agent
