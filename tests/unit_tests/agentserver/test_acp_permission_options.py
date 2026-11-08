"""hosted ACP 授权卡的 edition 裁剪。

企业版不提供 always 档：下发侧去掉 ``allow-always``；陈旧 host 仍回送该档时降级为
会话内记住，与 ``interface.py`` 对「永久记住」的企业版解码保持同一语义。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.core.sys_operation.cwd import _cwd_state, init_cwd
from openjiuwen.harness.security.host import PermissionConfirmationRequest

from jiuwenswarm.agents.harness.common.a4p.execution_context import (
    AUTHORIZATION_EXECUTION_CONTEXTS,
    AuthorizationExecutionContext,
)
from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    _acp_permission_options,
    build_permission_rail,
)
from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers
from jiuwenswarm.agents.harness.common.tools import acp_output_tools

SESSION_ID = "acp-session"
PERMISSION_CONFIG = {
    "enabled": True,
    "mode": "manual",
    "tools": {"bash": "ask"},
}


class _FakeSession:
    """``resolve_interrupt`` 往 session state 里写 auto-confirm 时用到的最小实现。"""

    def __init__(self) -> None:
        self.state: dict[str, object] = {}

    def get_session_id(self) -> str:
        return SESSION_ID

    def get_state(self, key: str) -> object:
        return self.state.get(key)

    def update_state(self, patch: dict[str, object]) -> None:
        self.state.update(patch)


class _Calls:
    def __init__(self) -> None:
        self.user: list[tuple] = []
        self.session: list[tuple] = []


@pytest.fixture(autouse=True)
def _execution_context():
    AUTHORIZATION_EXECUTION_CONTEXTS.activate(
        AuthorizationExecutionContext(
            request_id="acp-request",
            session_id=SESSION_ID,
            channel_id="acp",
            agent_id="agent-1",
            metadata={},
        )
    )
    try:
        yield
    finally:
        AUTHORIZATION_EXECUTION_CONTEXTS.clear()


@pytest.fixture
def workspace(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    token = _cwd_state.set(None)
    init_cwd(str(project), project_root=str(project), workspace=str(project))
    try:
        yield project
    finally:
        _cwd_state.reset(token)


def _answer_acp_with(monkeypatch: pytest.MonkeyPatch, option_id: str | None) -> dict:
    """替 ACP host 应答 ``session/request_permission``，并记录下发的选项集。"""
    sent: dict = {}

    async def fake_send(self, method, params, **kwargs):
        sent["method"] = method
        sent["option_ids"] = [option["optionId"] for option in params["options"]]
        return {
            "result": {
                "outcome": {"outcome": "selected", "optionId": option_id},
            }
        }

    monkeypatch.setattr(
        acp_output_tools.AcpOutputManager, "send_jsonrpc_request", fake_send
    )
    return sent


def _build_rail(monkeypatch: pytest.MonkeyPatch, workspace, calls: _Calls):
    """建一条真实 rail：持久化层换成计数器，绝不落到本机 user overlay。"""

    def _record(bucket: list[tuple]) -> object:
        def _inner(*args, **kwargs):
            bucket.append((args, kwargs))
            return True

        return _inner

    monkeypatch.setattr(permissions_layers, "load_user_permissions", lambda: {})
    monkeypatch.setattr(permissions_layers, "load_session_permissions", lambda _sid: {})
    monkeypatch.setattr(
        permissions_layers, "persist_user_overlay_from_effective", _record(calls.user)
    )
    monkeypatch.setattr(
        permissions_layers,
        "persist_session_overlay_from_effective",
        _record(calls.session),
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"permissions": dict(PERMISSION_CONFIG)},
    )
    return build_permission_rail(
        {"permissions": dict(PERMISSION_CONFIG)},
        session_id=SESSION_ID,
        workspace_root=workspace,
        channel_id="acp",
    )


def _permission_request() -> PermissionConfirmationRequest:
    return PermissionConfirmationRequest(
        ctx=SimpleNamespace(session=_FakeSession(), extra={}),
        tool_call=SimpleNamespace(
            name="bash", arguments={"command": "ls", "description": "list files"}
        ),
        result=SimpleNamespace(),
        auto_confirm_key="",
    )


def test_personal_acp_offers_always_allow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    assert [option["optionId"] for option in _acp_permission_options()] == [
        "allow-once",
        "allow-always",
        "reject-once",
    ]


def test_enterprise_acp_hides_always_allow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    assert [option["optionId"] for option in _acp_permission_options()] == [
        "allow-once",
        "reject-once",
    ]


@pytest.mark.asyncio
async def test_personal_acp_always_answer_stays_permanent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    sent = _answer_acp_with(monkeypatch, "allow-always")
    rail = _build_rail(monkeypatch, SimpleNamespace(), _Calls())

    response = await rail._host.request_permission_confirmation(_permission_request())

    assert sent["option_ids"] == ["allow-once", "allow-always", "reject-once"]
    assert response.approved is True
    assert response.auto_confirm is True
    assert response.wants_permanent_persist() is True
    assert response.wants_session_persist() is False


@pytest.mark.asyncio
async def test_enterprise_acp_stale_always_answer_degrades_to_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    sent = _answer_acp_with(monkeypatch, "allow-always")
    rail = _build_rail(monkeypatch, SimpleNamespace(), _Calls())

    response = await rail._host.request_permission_confirmation(_permission_request())

    assert sent["option_ids"] == ["allow-once", "reject-once"]
    assert response.approved is True
    assert response.auto_confirm is True
    assert response.wants_permanent_persist() is False
    assert response.wants_session_persist() is True


@pytest.mark.asyncio
async def test_personal_acp_always_answer_persists_user_overlay(
    monkeypatch: pytest.MonkeyPatch, workspace
) -> None:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    _answer_acp_with(monkeypatch, "allow-always")
    calls = _Calls()
    rail = _build_rail(monkeypatch, workspace, calls)

    decision = await rail.resolve_interrupt(
        SimpleNamespace(session=_FakeSession(), extra={}),
        ToolCall(
            id="call", type="function", name="bash", arguments=json.dumps({"command": "ls"})
        ),
        None,
        {},
    )

    assert type(decision).__name__ == "ApproveResult"
    assert len(calls.user) == 1
    assert calls.session == []


@pytest.mark.asyncio
async def test_enterprise_acp_always_answer_writes_no_user_overlay(
    monkeypatch: pytest.MonkeyPatch, workspace
) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    _answer_acp_with(monkeypatch, "allow-always")
    calls = _Calls()
    rail = _build_rail(monkeypatch, workspace, calls)

    decision = await rail.resolve_interrupt(
        SimpleNamespace(session=_FakeSession(), extra={}),
        ToolCall(
            id="call", type="function", name="bash", arguments=json.dumps({"command": "ls"})
        ),
        None,
        {},
    )

    assert type(decision).__name__ == "ApproveResult"
    assert calls.user == []
    assert len(calls.session) == 1
