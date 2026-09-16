"""Keep old manual answers on their runtime after the desired mode changes."""

# Imported pytest fixtures intentionally share test parameter names.
# ruff: noqa: F811

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from openjiuwen.core.foundation.llm import AssistantMessage

from jiuwenswarm.agents.harness.common.rails.permissions.auto_permission_rail import AutoPermissionInterruptRail
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue import RootPermissionQueueError
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime import agent_manager as manager_module
from jiuwenswarm.server.runtime.agent_manager import AgentManager, _make_agent_cache_key
from tests.unit_tests.agentserver.permissions.test_permission_answer_cutover import (
    _interrupt,
    _request,
    answer_host,  # noqa: F401
    cold,  # noqa: F401
)


def _manager(h, monkeypatch):
    manager = AgentManager()
    h.facade._adapter = h.adapter
    h.facade._session_manager = SimpleNamespace(has_session_runtime=lambda _sid: False)
    manager.agents = {"web": {_make_agent_cache_key("agent", None, str(h.root)): h.facade}}
    monkeypatch.setattr(manager_module, "get_config", lambda: deepcopy(h.raw))
    monkeypatch.setattr(manager, "get_agent", AsyncMock(side_effect=AssertionError("no new answer owner")))
    monkeypatch.setattr(h.facade, "prepare_session", AsyncMock(side_effect=AssertionError("no answer prepare")))
    return manager


def _answer_request(h, answer, **overrides):
    values = dict(
        request_id="manager-answer", session_id=h.adapter._parent_session_id,
        channel_id="web", req_method=ReqMethod.CHAT_SEND,
        params={"mode": "agent", "query": "", "project_dir": str(h.root), **answer},
    )
    values.update(overrides)
    return AgentRequest(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["ask", "permission"])
@pytest.mark.parametrize("reload_state", ["pending", "failed", "cancelled"])
async def test_manager_resumes_manual_sdk_call_before_installing_smart(
    answer_host, monkeypatch, kind, reload_state,
):
    h = answer_host
    answer = await _interrupt(h, monkeypatch, mode="manual", kind=kind)
    manager = _manager(h, monkeypatch)
    old_rail = h.adapter._permission_rail
    h.raw["permissions"]["mode"] = "auto"
    h.change()
    tail = asyncio.get_running_loop().create_future()
    manager._permissions_reload_tail = tail
    if reload_state == "failed":
        tail.set_exception(RuntimeError("desired reload failed"))
        tail.exception()
    elif reload_state == "cancelled":
        tail.cancel()
    admit = Mock(return_value=str(h.root))
    try:
        owner = await asyncio.wait_for(manager.get_agent_for_request(
            _answer_request(h, answer), admit_request=admit,
        ), 1)
        assert owner is h.facade
        admit.assert_called_once_with()
        manager.get_agent.assert_not_awaited()
        h.facade.prepare_session.assert_not_awaited()
        result = await _request(h, **answer)
        assert result.ok and len(h.script.calls) == 2
        assert h.adapter._permission_rail is old_rail
        assert not h.adapter._enable_auto_permission
        assert h.executions == ([{"value": "once"}] if kind == "permission" else [])
        h.script.responses = [AssistantMessage(content="next task completed")]
        assert (await _request(h, query="Start another task")).ok
        assert isinstance(h.adapter._permission_rail, AutoPermissionInterruptRail)
    finally:
        if not tail.done():
            tail.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["session", "channel", "missing"])
async def test_manager_does_not_create_missing_old_answer_owner(answer_host, monkeypatch, mismatch):
    h = answer_host
    answer = await _interrupt(h, monkeypatch, mode="manual", kind="ask")
    manager = _manager(h, monkeypatch)
    h.raw["permissions"]["mode"] = "auto"
    request = _answer_request(h, answer)
    if mismatch == "session":
        request.session_id = "another-session"
    elif mismatch == "channel":
        request.channel_id = "another-channel"
    else:
        manager.agents.clear()
    admit = Mock(return_value=str(h.root))
    with pytest.raises(RootPermissionQueueError, match="permission_resume_owner_missing"):
        await manager.get_agent_for_request(request, admit_request=admit)
    admit.assert_not_called()
    manager.get_agent.assert_not_awaited()
    h.facade.prepare_session.assert_not_awaited()
    assert not h.executions and len(h.script.calls) == 1


@pytest.mark.asyncio
async def test_manager_prefers_installed_smart_answer_owner(answer_host, monkeypatch):
    h = answer_host
    answer = await _interrupt(h, monkeypatch, mode="auto", kind="permission")
    manager = _manager(h, monkeypatch)
    lookup = Mock(side_effect=AssertionError("Smart owner must not use manual lookup"))
    monkeypatch.setattr(manager, "get_agent_for_session_nowait", lookup)
    admit = Mock(return_value=str(h.root))
    assert await manager.get_agent_for_request(_answer_request(h, answer), admit_request=admit) is h.facade
    lookup.assert_not_called()
    admit.assert_called_once_with()
    assert (await _request(h, **answer)).ok
    assert h.executions == [{"value": "once"}]
