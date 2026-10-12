from __future__ import annotations

from types import SimpleNamespace

import pytest

from openjiuwen.harness.security.host import PermissionConfirmationRequest

from jiuwenswarm.agents.harness.common import a4p_runtime
from jiuwenswarm.agents.harness.common.a4p_execution_context import (
    AUTHORIZATION_EXECUTION_CONTEXTS,
    AuthorizationExecutionContext,
    AuthorizerRoute,
)
from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    build_permission_rail,
)


def _permission_request() -> PermissionConfirmationRequest:
    return PermissionConfirmationRequest(
        ctx=SimpleNamespace(
            session=SimpleNamespace(get_session_id=lambda: "cron-session")
        ),
        tool_call=SimpleNamespace(
            name="bash",
            arguments={"command": "date", "description": "get time"},
        ),
        result=SimpleNamespace(),
        auto_confirm_key="",
    )


def _web_authorizer_route() -> AuthorizerRoute:
    return AuthorizerRoute(
        session_id="cron-session",
        app_id="default",
        agent_ref_mode="agent",
        agent_ref_id="default",
    )


@pytest.fixture(autouse=True)
def _cron_execution_context():
    AUTHORIZATION_EXECUTION_CONTEXTS.activate(
        AuthorizationExecutionContext(
            request_id="cron-request",
            session_id="cron-session",
            channel_id="__cron__",
            agent_id="agent-1",
            metadata={"cron": {"job_id": "job-1"}},
        )
    )
    yield
    AUTHORIZATION_EXECUTION_CONTEXTS.clear()


@pytest.mark.asyncio
async def test_cron_a4p_scope_miss_is_denied_without_session_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Runtime:
        async def find_valid_cron_intent_token(self, **kwargs):
            assert kwargs["cron_job_id"] == "job-1"
            return None

        async def find_valid_intent_token(self, **kwargs):
            raise AssertionError("cron scope miss must not fall back to session token")

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {
            "a4p": {"enabled": True},
            "permissions": {"enabled": True, "tools": {"bash": "ask"}},
        },
    )
    monkeypatch.setattr(a4p_runtime, "get_a4p_runtime", lambda: _Runtime())
    rail = build_permission_rail(
        {"permissions": {"enabled": True, "tools": {"bash": "ask"}}}
    )
    response = await rail._host.request_permission_confirmation(_permission_request())

    assert response.approved is False
    assert response.auto_confirm is False
    assert "[A4P_CRON_SCOPE_DENIED]" in response.feedback
    assert "job=job-1" in response.feedback
    assert "tool=bash" in response.feedback
    assert "不要原样重试" in response.feedback


@pytest.mark.asyncio
async def test_cron_a4p_scope_match_is_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Runtime:
        async def find_valid_cron_intent_token(self, **kwargs):
            return {"tokenId": "token-1"}

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {
            "a4p": {"enabled": True},
            "permissions": {"enabled": True, "tools": {"bash": "ask"}},
        },
    )
    monkeypatch.setattr(a4p_runtime, "get_a4p_runtime", lambda: _Runtime())
    rail = build_permission_rail(
        {"permissions": {"enabled": True, "tools": {"bash": "ask"}}}
    )
    response = await rail._host.request_permission_confirmation(_permission_request())

    assert response.approved is True


@pytest.mark.asyncio
async def test_web_a4p_scope_miss_after_approval_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Runtime:
        async def find_valid_intent_token(self, **kwargs):
            return None

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {
            "a4p": {"enabled": True},
            "permissions": {"enabled": True, "tools": {"bash": "ask"}},
        },
    )
    monkeypatch.setattr(a4p_runtime, "get_a4p_runtime", lambda: _Runtime())
    AUTHORIZATION_EXECUTION_CONTEXTS.clear()
    AUTHORIZATION_EXECUTION_CONTEXTS.activate(
        AuthorizationExecutionContext(
            request_id="web-request",
            session_id="cron-session",
            channel_id="web",
            agent_id="agent-1",
            metadata={},
            authorizer_route=_web_authorizer_route(),
        )
    )
    AUTHORIZATION_EXECUTION_CONTEXTS.set_attempt(
        "cron-session",
        request_id="web-request",
        status="approved",
    )
    rail = build_permission_rail(
        {"permissions": {"enabled": True, "tools": {"bash": "ask"}}}
    )

    response = await rail._host.request_permission_confirmation(_permission_request())

    assert response.approved is False
    assert "[A4P_SCOPE_DENIED]" in response.feedback


@pytest.mark.asyncio
async def test_web_a4p_verification_error_after_request_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Runtime:
        async def find_valid_intent_token(self, **kwargs):
            raise RuntimeError("token verification unavailable")

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {
            "a4p": {"enabled": True},
            "permissions": {"enabled": True, "tools": {"bash": "ask"}},
        },
    )
    monkeypatch.setattr(a4p_runtime, "get_a4p_runtime", lambda: _Runtime())
    AUTHORIZATION_EXECUTION_CONTEXTS.clear()
    AUTHORIZATION_EXECUTION_CONTEXTS.activate(
        AuthorizationExecutionContext(
            request_id="web-request",
            session_id="cron-session",
            channel_id="web",
            agent_id="agent-1",
            metadata={},
            authorizer_route=_web_authorizer_route(),
        )
    )
    AUTHORIZATION_EXECUTION_CONTEXTS.set_attempt(
        "cron-session",
        request_id="web-request",
        status="approved",
    )
    rail = build_permission_rail(
        {"permissions": {"enabled": True, "tools": {"bash": "ask"}}}
    )

    response = await rail._host.request_permission_confirmation(_permission_request())

    assert response.approved is False
    assert "[A4P_SCOPE_DENIED]" in response.feedback
    assert "token verification unavailable" in response.feedback


@pytest.mark.asyncio
async def test_web_without_a4p_attempt_falls_back_to_normal_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Runtime:
        async def find_valid_intent_token(self, **kwargs):
            return None

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {
            "a4p": {"enabled": True},
            "permissions": {"enabled": True, "tools": {"bash": "ask"}},
        },
    )
    monkeypatch.setattr(a4p_runtime, "get_a4p_runtime", lambda: _Runtime())
    AUTHORIZATION_EXECUTION_CONTEXTS.clear()
    AUTHORIZATION_EXECUTION_CONTEXTS.activate(
        AuthorizationExecutionContext(
            request_id="web-request",
            session_id="cron-session",
            channel_id="web",
            agent_id="agent-1",
            metadata={},
            authorizer_route=_web_authorizer_route(),
        )
    )
    rail = build_permission_rail(
        {"permissions": {"enabled": True, "tools": {"bash": "ask"}}}
    )

    response = await rail._host.request_permission_confirmation(_permission_request())

    assert response == "interrupt"


@pytest.mark.asyncio
async def test_background_web_request_does_not_inspect_session_a4p_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Runtime:
        async def find_valid_intent_token(self, **kwargs):
            raise AssertionError("background requests must not inspect session A4P tokens")

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {
            "a4p": {"enabled": True},
            "permissions": {"enabled": True, "tools": {"bash": "ask"}},
        },
    )
    monkeypatch.setattr(a4p_runtime, "get_a4p_runtime", lambda: _Runtime())
    AUTHORIZATION_EXECUTION_CONTEXTS.clear()
    AUTHORIZATION_EXECUTION_CONTEXTS.activate(
        AuthorizationExecutionContext(
            request_id="goal-request",
            session_id="cron-session",
            channel_id="web",
            agent_id="agent-1",
            metadata={},
            authorizer_route=None,
        )
    )
    rail = build_permission_rail(
        {"permissions": {"enabled": True, "tools": {"bash": "ask"}}}
    )

    response = await rail._host.request_permission_confirmation(_permission_request())

    assert response == "interrupt"
