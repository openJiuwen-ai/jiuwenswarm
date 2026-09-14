from __future__ import annotations

import asyncio
import calendar
from contextlib import closing
import inspect
import json
import sqlite3
import time
from types import SimpleNamespace
from typing import Any

import pytest

from a4p.user_authorizer import approve_user_mandate
from a4p.credential_store import UserCredentialRecord
from a4p.types import (
    IntentAuthorizationResponse,
    UserAuthorizationResponse,
)
from openjiuwen.core.foundation.tool import ToolExposure

from jiuwenswarm.agents.harness.common import a4p_runtime
from jiuwenswarm.agents.harness.common import a4p_rpc
from jiuwenswarm.agents.harness.common import a4p_display
from jiuwenswarm.agents.harness.common.a4p_authorizer import (
    WebAuthorizationRequest,
    WebAuthorizerBroker,
)
from jiuwenswarm.agents.harness.common.a4p_cron_token_store import (
    CRON_INTENT_TOKENS_DB_FILENAME,
    SQLiteCronIntentTokenStore,
)
from jiuwenswarm.agents.harness.common.a4p_execution_context import (
    AUTHORIZATION_EXECUTION_CONTEXTS,
    AuthorizationExecutionContext,
    AuthorizerRoute,
    build_authorization_execution_context,
)
from jiuwenswarm.agents.harness.common.tools import a4p_tools as a4p_tools_module
from jiuwenswarm.agents.harness.common.a4p_runtime import (
    A4PIdentity,
    A4PRuntime,
    DEFAULT_CRON_INTENT_VALIDITY_SECONDS,
    INTERNAL_A4P_USER_ID,
    is_a4p_enabled,
    remove_cron_intent_token_for_job,
    resolve_a4p_identity,
)
from jiuwenswarm.agents.harness.common.tools.a4p_tools import (
    _normalize_actions,
    get_tools,
    request_a4p_intent_authorization,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod


@pytest.fixture(autouse=True)
def _isolate_a4p_runtime_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    monkeypatch.setattr(a4p_runtime, "get_config_dir", lambda: tmp_path)
    AUTHORIZATION_EXECUTION_CONTEXTS.clear()
    a4p_runtime.reset_a4p_runtime_for_tests()
    yield
    AUTHORIZATION_EXECUTION_CONTEXTS.clear()
    a4p_runtime.reset_a4p_runtime_for_tests()


def _activate_execution_context(
    *,
    session_id: str = "session-1",
    channel_id: str = "web",
    request_id: str = "request-1",
    metadata: dict[str, Any] | None = None,
) -> AuthorizerRoute | None:
    route = (
        AuthorizerRoute(
            session_id=session_id,
            app_id="default",
            agent_ref_mode="agent",
            agent_ref_id="agent-1",
        )
        if channel_id == "web"
        else None
    )
    AUTHORIZATION_EXECUTION_CONTEXTS.activate(
        AuthorizationExecutionContext(
            request_id=request_id,
            session_id=session_id,
            channel_id=channel_id,
            agent_id="agent-1",
            metadata=dict(metadata or {}),
            authorizer_route=route,
        )
    )
    return route


def test_background_goal_does_not_create_authorizer_route() -> None:
    request = SimpleNamespace(
        request_id="goal-1",
        session_id="session-1",
        channel_id="web",
        req_method=ReqMethod.COMMAND_GOAL,
        params={"action": "resume"},
        metadata={"app_id": "default", "ws_id": "ws-1"},
        agent_ref={"mode": "agent", "id": "main"},
    )

    context = build_authorization_execution_context(
        request,
        agent_id="main_agent",
        mode="agent",
    )

    assert context.authorizer_route is None
    assert context.is_interactive_web is False


def test_authorizer_route_preserves_logical_agent_reference() -> None:
    request = SimpleNamespace(
        request_id="request-1",
        session_id="session-1",
        channel_id="web",
        req_method=ReqMethod.CHAT_SEND,
        params={},
        metadata={"app_id": "app-1", "ws_id": "ws-1"},
        agent_ref={"mode": "team", "id": "research"},
    )

    context = build_authorization_execution_context(
        request,
        agent_id="main_agent",
        mode="agent",
    )

    assert context.authorizer_route == AuthorizerRoute(
        session_id="session-1",
        app_id="app-1",
        agent_ref_mode="team",
        agent_ref_id="research",
        ws_id="ws-1",
    )


def test_a4p_default_disabled() -> None:
    assert is_a4p_enabled({}) is False
    assert is_a4p_enabled({"a4p": {"enabled": False}}) is False


def test_a4p_enabled_is_the_only_feature_flag() -> None:
    assert is_a4p_enabled({"a4p": {"enabled": True}}) is True
    assert (
        is_a4p_enabled({"a4p": {"enabled": True, "intent_authorization_enabled": False}})
        is True
    )


def test_a4p_tool_schema_exposes_only_security_critical_params() -> None:
    tool_card = get_tools()[0].card
    assert tool_card.exposure is ToolExposure.DIRECT
    assert "exposure" in tool_card.model_fields_set
    assert tool_card.parallel_safe is False
    actions_schema = tool_card.input_params["properties"]["actions"]
    action_schema = actions_schema["items"]
    properties = action_schema["properties"]

    assert "Request user approval for one currently known A4P" in tool_card.description
    assert "bash, mcp_exec_command, create_terminal, write_file, edit_file, acp_chat" in tool_card.description
    assert "only the current stage" in tool_card.description
    assert "Runtime-dependent arguments do not require another authorization" in tool_card.description
    assert "only when a later stage is not covered by an approved scope" in tool_card.description
    assert "cron authorization cannot be staged" in tool_card.description
    assert "each token is added" in tool_card.description
    assert "current Web session" in tool_card.description
    assert "existing disabled cron job" in tool_card.description
    assert "does not execute them" in tool_card.description
    for workflow_rule in (
        "Normal Web selection rule",
        "two or more total calls",
        "MUST call this before any of those tools",
        "One setup call plus N repeated or batch calls counts as N+1",
        "Cron selection rule",
        "MANDATORY FIRST TOOL",
        "cron_toggle_job",
        "payment",
        "external-operation",
    ):
        assert workflow_rule not in tool_card.description

    assert actions_schema["description"] == "Non-empty list of supported tool action scopes."
    assert action_schema["required"] == ["name", "params"]
    assert "allowExtraParams" not in properties
    assert properties["params"]["type"] == "object"
    assert properties["params"]["minProperties"] == 1
    params_description = properties["params"]["description"]
    assert "security-critical constraints" in params_description
    assert "bash -> command" in params_description
    assert "mcp_exec_command -> command" in params_description
    assert "create_terminal -> cmd" in params_description
    assert "write_file -> file_path" in params_description
    assert "edit_file -> file_path" in params_description
    assert "acp_chat -> agent" in params_description
    assert "file_path must be a concrete filename or a filename glob under a fixed directory" in params_description
    assert "a directory path alone is invalid" in params_description
    assert "Other ordinary call parameters are allowed automatically" in params_description
    assert "authorize each complete exact command by default" in params_description
    assert "cannot absorb whitespace, Shell operators, redirections, or extra arguments" in params_description
    assert "Never use a bare * as the entire parameter" in params_description
    assert "Runtime values need not be known yet if their allowed scope can already be expressed" in params_description
    assert "consolidate related operations" not in params_description
    assert "&&" not in params_description
    assert set(tool_card.input_params["properties"]) == {
        "actions",
        "validitySeconds",
        "cronJobId",
        "reason",
    }
    assert "executionPolicy" not in inspect.signature(
        request_a4p_intent_authorization
    ).parameters
    cron_description = get_tools()[0].card.input_params["properties"]["cronJobId"]["description"]
    assert "existing job must be disabled" in cron_description
    assert "cron_toggle_job" not in cron_description
    reason_description = get_tools()[0].card.input_params["properties"]["reason"]["description"]
    assert reason_description == "Optional display-only explanation; it does not constrain the token."


@pytest.mark.asyncio
@pytest.mark.parametrize("optional_arguments", [{}, {"validitySeconds": 120, "cronJobId": "job-1"}])
async def test_a4p_tool_translates_wire_parameter_names(monkeypatch, optional_arguments) -> None:
    received = {}

    async def _request(actions, validity_seconds=None, cron_job_id=None, reason=None, *, _session_id=None):
        received.update(
            actions=actions,
            validity_seconds=validity_seconds,
            cron_job_id=cron_job_id,
            reason=reason,
            session_id=_session_id,
        )
        return {"ok": True}

    monkeypatch.setattr(a4p_tools_module, "request_a4p_intent_authorization", _request)
    actions = [{"name": "bash", "params": {"command": "pwd"}}]
    result = await get_tools(" session-1 ")[0].invoke(
        {"actions": actions, "reason": "test", **optional_arguments}
    )

    assert result == {"ok": True}
    assert received == {
        "actions": actions,
        "validity_seconds": optional_arguments.get("validitySeconds"),
        "cron_job_id": optional_arguments.get("cronJobId"),
        "reason": "test",
        "session_id": "session-1",
    }


def test_a4p_identity_resolves_agent_only() -> None:
    identity = resolve_a4p_identity(
        metadata={},
        agent_name="agent-1",
    )

    assert identity.agent_id == "agent-1"


def test_a4p_identity_ignores_user_metadata() -> None:
    identity = resolve_a4p_identity(
        metadata={"user_id": "metadata-user"},
        agent_name="agent-1",
    )

    assert identity == A4PIdentity(agent_id="agent-1")


def test_a4p_identity_metadata_agent_overrides_agent_name() -> None:
    identity = resolve_a4p_identity(
        metadata={"agent_id": "metadata-agent", "principal_user_id": "principal-user"},
        agent_name="agent-1",
    )

    assert identity.agent_id == "metadata-agent"


def test_a4p_status_reports_intent_only_state() -> None:
    runtime = A4PRuntime(
        {
            "a4p": {
                "enabled": False,
            }
        }
    )
    payload = runtime.status()

    assert payload["enabled"] is False
    assert payload["requireUserSignature"] is False
    assert "intentAuthorizationEnabled" not in payload
    assert payload["intentValiditySeconds"] == 3600
    assert payload["cronIntentValiditySeconds"] == DEFAULT_CRON_INTENT_VALIDITY_SECONDS
    assert payload["authorizationTimeoutSeconds"] == 300
    assert runtime.authorizer.timeout_seconds == 300
    assert "intentTokenUsagePath" not in payload
    assert "userId" not in payload
    assert "userName" not in payload
    assert payload["webauthnCredentialStorePath"].endswith(
        "a4p/webauthn_credentials.json"
    )
    assert payload["webauthnCredentialsCount"] == 0


@pytest.mark.asyncio
async def test_a4p_runtime_prepares_unsigned_intent_mandate() -> None:
    runtime = A4PRuntime({"a4p": {"enabled": True}})

    prepared = await runtime.server.prepare_intent_authorization(
        {
            "agentId": "agent-1",
            "userId": "user-1",
            "intent": {"actions": [{"name": "bash", "params": {"command": "pwd"}}]},
            "validitySeconds": 60,
        }
    )

    assert prepared.mandate is not None
    assert prepared.mandate["userAuthorization"] == {"required": False}


@pytest.mark.asyncio
async def test_a4p_signed_mode_requires_registered_credential_before_push() -> None:
    runtime = A4PRuntime(
        {"a4p": {"enabled": True, "require_user_signature": True}}
    )
    pushed = False

    async def _authorize(
        _request: WebAuthorizationRequest,
    ) -> UserAuthorizationResponse:
        nonlocal pushed
        pushed = True
        raise AssertionError("missing credentials must not create an approval card")

    runtime.authorizer.request_authorization = _authorize
    payload = await runtime.request_intent_authorization(
        session_id="session-1",
        channel_id="web",
        authorizer_route=_activate_execution_context(),
        identity=A4PIdentity(agent_id="agent-1"),
        actions=[{"name": "bash", "params": {"command": "pwd"}}],
        validity_seconds=60,
    )

    assert pushed is False
    assert payload["approved"] is False
    assert payload["verificationResult"]["code"] == "USER_CREDENTIAL_NOT_REGISTERED"


@pytest.mark.asyncio
async def test_a4p_signed_mode_pushes_hardened_webauthn_options() -> None:
    runtime = A4PRuntime(
        {"a4p": {"enabled": True, "require_user_signature": True}}
    )
    runtime.webauthn.credential_store.save(
        UserCredentialRecord(
            userId=INTERNAL_A4P_USER_ID,
            credentialId="Y3JlZGVudGlhbC0x",
            signatureMethod="webauthn",
            publicKey={"format": "cose", "value": "cHVibGljLWtleQ"},
            details={
                "signCount": 0,
                "rpId": "localhost",
                "origin": "http://localhost:5173",
            },
            createdAt="2026-07-27T00:00:00Z",
        )
    )
    seen: dict[str, Any] = {}

    async def _reject(
        request: WebAuthorizationRequest,
    ) -> UserAuthorizationResponse:
        seen["request"] = request
        return UserAuthorizationResponse(approved=False, rejectReason="test")

    runtime.authorizer.request_authorization = _reject
    await runtime.request_intent_authorization(
        session_id="session-1",
        channel_id="web",
        authorizer_route=_activate_execution_context(),
        identity=A4PIdentity(agent_id="agent-1"),
        actions=[{"name": "bash", "params": {"command": "pwd"}}],
        validity_seconds=60,
    )

    request = seen["request"]
    assert request.mandate["userAuthorization"] == {
        "required": True,
        "signatureMethod": "webauthn",
        "methodPolicy": {"userVerification": "required"},
    }
    assert request.signing_options["signatureMethod"] == "webauthn"
    assert request.signing_options["methodOptions"]["userVerification"] == "required"
    assert request.signing_options["methodOptions"]["challenge"]
    assert request.signing_options["methodOptions"]["rpId"] == "localhost"


def test_a4p_webauthn_status_returns_sanitized_credential_summaries() -> None:
    runtime = A4PRuntime({"a4p": {"enabled": False}})
    runtime.webauthn.credential_store.save(
        UserCredentialRecord(
            userId=INTERNAL_A4P_USER_ID,
            credentialId="credential-1",
            signatureMethod="webauthn",
            publicKey={"format": "cose", "value": "secret-public-key"},
            details={
                "signCount": 4,
                "transports": ["internal"],
                "credentialDeviceType": "multi_device",
                "credentialBackedUp": True,
            },
            createdAt="2026-07-27T00:00:00Z",
        )
    )

    payload = runtime.webauthn_credential_status()

    assert payload["enabled"] is False
    assert payload["rpId"] == "localhost"
    assert payload["expectedOrigin"] == "http://localhost:5173"
    assert payload["credentials"] == [
        {
            "credentialId": "credential-1",
            "createdAt": "2026-07-27T00:00:00Z",
            "signatureMethod": "webauthn",
            "publicKeyFormat": "cose",
            "signCount": 4,
            "transports": ["internal"],
            "credentialDeviceType": "multi_device",
            "credentialBackedUp": True,
        }
    ]
    assert "publicKey" not in payload["credentials"][0]


@pytest.mark.asyncio
async def test_a4p_signed_broker_requires_assertion_and_keeps_pending() -> None:
    def _sign(
        mandate: dict[str, Any],
        assertion: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if assertion is None:
            raise ValueError("WebAuthn assertion missing")
        return {**mandate, "assertionId": assertion["id"]}

    broker = WebAuthorizerBroker(approve_mandate=_sign)
    route = _activate_execution_context()
    assert route is not None

    async def _send(*_args, **_kwargs) -> None:
        return None

    broker._send_push = _send
    task = asyncio.create_task(
        broker.request_authorization(
            WebAuthorizationRequest(
                request_id="signed-1",
                mandate={"mandateId": "signed-1"},
                signing_options={"signatureMethod": "webauthn"},
                ui_context={"sessionId": "session-1"},
                route=route,
            )
        )
    )
    await asyncio.sleep(0)

    missing = broker.complete("signed-1", route)
    assert missing["code"] == "A4P_WEBAUTHN_ASSERTION_REQUIRED"
    assert "signed-1" in broker._pending

    completed = broker.complete(
        "signed-1",
        route,
        assertion={"id": "credential-1"},
    )
    response = await task

    assert completed["ok"] is True
    assert response.signedMandate["assertionId"] == "credential-1"


@pytest.mark.parametrize(
    ("language", "expected_lines", "forbidden_line"),
    [
        (
            "zh",
            (
                "授权原因\nCreate workspace reports",
                "授权操作\n",
                "执行 Shell 命令：mkdir -p /workspace/reports",
                "写入文件：/workspace/reports/*.md",
                "有效时间\n北京时间：",
            ),
            "执行次数限制",
        ),
        (
            "en",
            (
                "Authorization reason\nCreate workspace reports",
                "Authorized actions\n",
                "Run shell command: mkdir -p /workspace/reports",
                "Write file: /workspace/reports/*.md",
                "Valid time\nBeijing Time:",
            ),
            "Execution limit",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a4p_intent_display_text_is_localized_and_readable(
    monkeypatch: pytest.MonkeyPatch,
    language: str,
    expected_lines: tuple[str, ...],
    forbidden_line: str,
) -> None:
    monkeypatch.setattr(
        a4p_display,
        "get_config",
        lambda: {"preferred_language": language},
    )
    runtime = A4PRuntime({"a4p": {"enabled": True}})
    seen: dict[str, dict] = {}

    async def _authorize(
        request: WebAuthorizationRequest,
    ) -> UserAuthorizationResponse:
        seen["mandate"] = request.mandate
        return UserAuthorizationResponse(
            approved=False,
            rejectReason="test",
        )

    runtime.authorizer.request_authorization = _authorize
    await runtime.request_intent_authorization(
        session_id="session-1",
        channel_id="web",
        authorizer_route=_activate_execution_context(),
        identity=A4PIdentity(agent_id="main_agent"),
        actions=[
            {"name": "bash", "params": {"command": "mkdir -p /workspace/reports"}},
            {"name": "write_file", "params": {"file_path": "/workspace/reports/*.md"}},
        ],
        validity_seconds=60,
        reason="Create workspace reports",
    )

    display_text = seen["mandate"]["displayText"]
    assert all(line in display_text for line in expected_lines)
    assert forbidden_line not in display_text
    assert "\n\n" in display_text
    assert "JiuwenSwarm" not in display_text
    assert "agent:main_agent" not in display_text
    assert "Z" not in display_text


def test_a4p_display_time_uses_beijing_timezone_without_utc_suffix() -> None:
    assert a4p_display.format_beijing_time("2026-07-20T09:50:57Z") == (
        "2026-07-20 17:50:57"
    )
    assert a4p_display.format_beijing_time("2026-07-20T10:50:57Z") == (
        "2026-07-20 18:50:57"
    )


@pytest.mark.asyncio
async def test_a4p_intent_display_context_is_reset_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        a4p_display,
        "get_config",
        lambda: {"preferred_language": "en"},
    )
    runtime = A4PRuntime({"a4p": {"enabled": True}})

    async def _raise(_request: dict) -> UserAuthorizationResponse:
        raise RuntimeError("test")

    runtime.server.prepare_intent_authorization = _raise
    with pytest.raises(RuntimeError, match="test"):
        await runtime.request_intent_authorization(
            session_id="session-1",
            channel_id="web",
            authorizer_route=_activate_execution_context(),
            identity=A4PIdentity(agent_id="main_agent"),
            actions=[{"name": "bash", "params": {"command": "pwd"}}],
        )

    assert a4p_display.INTENT_DISPLAY_CONTEXT.get() is None


@pytest.mark.asyncio
async def test_a4p_complete_authorization_builds_unsigned_approval() -> None:
    runtime = A4PRuntime({"a4p": {"enabled": True}})
    route = _activate_execution_context()
    assert route is not None

    async def _send(*_args, **_kwargs) -> None:
        return None

    runtime.authorizer._send_push = _send
    task = asyncio.create_task(
        runtime.authorizer.request_authorization(
            WebAuthorizationRequest(
                request_id="req-1",
                mandate={
                    "type": "a4p/v1/intent-mandate",
                    "intent": {
                        "actions": [{"name": "bash", "params": {"command": "pwd"}}]
                    },
                    "signatures": {"server": {}, "user": {}},
                },
                signing_options={},
                ui_context={"sessionId": "session-1"},
                route=route,
            )
        )
    )
    await asyncio.sleep(0)

    result = runtime.authorizer.complete("req-1", route)

    assert result["ok"] is True
    response = await task
    assert response.approved is True
    assert response.signedMandate is not None
    assert response.signedMandate["signatures"]["user"] == {}


@pytest.mark.asyncio
async def test_a4p_pending_authorization_is_scoped_to_logical_route() -> None:
    runtime = A4PRuntime({"a4p": {"enabled": True}})
    route = _activate_execution_context()
    wrong_route = AuthorizerRoute(
        session_id="other-session",
        app_id="default",
        agent_ref_mode="agent",
        agent_ref_id="agent-1",
    )
    async def _send(*_args, **_kwargs) -> None:
        return None

    runtime.authorizer._send_push = _send
    request = WebAuthorizationRequest(
        request_id="req-1",
        mandate={"mandateId": "req-1", "intent": {"actions": []}},
        signing_options={},
        ui_context={"kind": "intent", "sessionId": "session-1"},
        route=route,
    )
    task = asyncio.create_task(runtime.authorizer.request_authorization(request))
    await asyncio.sleep(0)

    assert runtime.authorizer.pending_for_route(wrong_route)["pending"] is None
    restored = runtime.authorizer.pending_for_route(route)["pending"]
    assert restored["requestId"] == "req-1"
    assert runtime.authorizer.complete("req-1", wrong_route) == {
        "ok": False,
        "error": "A4P authorization route mismatch",
    }
    assert "req-1" in runtime.authorizer._pending
    runtime.authorizer.reject("req-1", route, "done")
    await task


@pytest.mark.asyncio
async def test_a4p_complete_rpc_does_not_accept_external_signed_mandate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _Authorizer:
        def complete(
            self,
            request_id: str,
            route: AuthorizerRoute,
            *,
            assertion: dict[str, Any] | None = None,
        ) -> dict:
            assert route.logical_key == ("session-1", "default", "agent", "agent-1")
            assert assertion is None
            calls.append(request_id)
            return {"ok": True, "requestId": request_id}

    class _Runtime:
        authorizer = _Authorizer()

    monkeypatch.setattr(a4p_rpc, "is_a4p_enabled", lambda: True)
    monkeypatch.setattr(a4p_rpc, "get_a4p_runtime", lambda: _Runtime())

    response = await a4p_rpc.dispatch_a4p_request(
        AgentRequest(
            request_id="rpc-1",
            channel_id="web",
            session_id="session-1",
            req_method=ReqMethod.A4P_AUTHORIZATION_COMPLETE,
            metadata={
                "app_id": "default",
                "agent_ref": {"mode": "agent", "id": "agent-1"},
            },
            params={
                "requestId": "intent-1",
                "signedMandate": {"untrusted": True},
            },
        )
    )

    assert response.ok is True
    assert calls == ["intent-1"]


@pytest.mark.asyncio
async def test_a4p_pending_rpc_returns_only_current_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Authorizer:
        def pending_for_route(self, route: AuthorizerRoute) -> dict:
            assert route.logical_key == ("session-1", "default", "agent", "agent-1")
            return {"ok": True, "pending": {"requestId": "intent-1"}}

    class _Runtime:
        authorizer = _Authorizer()

    monkeypatch.setattr(a4p_rpc, "is_a4p_enabled", lambda: True)
    monkeypatch.setattr(a4p_rpc, "get_a4p_runtime", lambda: _Runtime())
    response = await a4p_rpc.dispatch_a4p_request(
        AgentRequest(
            request_id="rpc-1",
            channel_id="web",
            session_id="session-1",
            req_method=ReqMethod.A4P_AUTHORIZATION_PENDING,
            metadata={
                "app_id": "default",
                "agent_ref": {"mode": "agent", "id": "agent-1"},
            },
        )
    )

    assert response.ok is True
    assert response.payload["pending"]["requestId"] == "intent-1"


@pytest.mark.asyncio
async def test_a4p_pending_rpc_clears_stale_card_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(a4p_rpc, "is_a4p_enabled", lambda: False)

    response = await a4p_rpc.dispatch_a4p_request(
        AgentRequest(
            request_id="rpc-1",
            channel_id="web",
            session_id="session-1",
            req_method=ReqMethod.A4P_AUTHORIZATION_PENDING,
        )
    )

    assert response.ok is True
    assert response.payload == {"ok": True, "pending": None}


@pytest.mark.asyncio
async def test_a4p_webauthn_management_rpc_remains_available_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Runtime:
        @staticmethod
        def webauthn_credential_status() -> dict[str, Any]:
            return {
                "enabled": False,
                "requireUserSignature": False,
                "credentials": [],
            }

    monkeypatch.setattr(a4p_rpc, "is_a4p_enabled", lambda: False)
    monkeypatch.setattr(a4p_rpc, "get_a4p_runtime", lambda: _Runtime())

    response = await a4p_rpc.dispatch_a4p_request(
        AgentRequest(
            request_id="rpc-credentials",
            channel_id="web",
            session_id="session-1",
            req_method=ReqMethod.A4P_WEBAUTHN_CREDENTIALS_GET,
            metadata={
                "app_id": "default",
                "agent_ref": {"mode": "agent", "id": "agent-1"},
            },
        )
    )

    assert response.ok is True
    assert response.payload["enabled"] is False
    assert response.payload["credentials"] == []


def test_a4p_webauthn_registration_uses_internal_user_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = A4PRuntime({"a4p": {"enabled": False}})
    captured: dict[str, Any] = {}
    route = _activate_execution_context()
    assert route is not None

    def _options(request: dict[str, Any]) -> dict[str, Any]:
        captured["options"] = request
        return {
            "registrationRequestId": "registration-1",
            "options": {"challenge": "challenge-1"},
        }

    def _verify(request: dict[str, Any]) -> dict[str, Any]:
        captured["verify"] = request
        return {
            "registered": True,
            "created": True,
            "credential": {"credentialId": "credential-1"},
        }

    monkeypatch.setattr(runtime.server, "webauthn_registration_options", _options)
    monkeypatch.setattr(runtime.server, "verify_webauthn_registration", _verify)
    monkeypatch.setattr(
        runtime.webauthn,
        "registered_credential_summary",
        lambda credential_id: {"credentialId": credential_id},
    )

    options = runtime.webauthn_registration_options(route)
    verified = runtime.verify_webauthn_registration(
        registration_request_id="registration-1",
        credential={"id": "credential-1"},
        route=route,
    )

    assert options["registrationRequestId"] == "registration-1"
    assert captured["options"] == {
        "userId": INTERNAL_A4P_USER_ID,
        "userName": "jiuwenswarm-owner",
        "userDisplayName": "JiuwenSwarm Owner",
    }
    assert captured["verify"] == {
        "registrationRequestId": "registration-1",
        "userId": INTERNAL_A4P_USER_ID,
        "credential": {"id": "credential-1"},
    }
    assert verified["credential"] == {"credentialId": "credential-1"}
    with pytest.raises(ValueError, match="No WebAuthn registration request"):
        runtime.verify_webauthn_registration(
            registration_request_id="registration-1",
            credential={"id": "credential-1"},
            route=route,
        )


def test_a4p_webauthn_registration_rejects_wrong_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = A4PRuntime({"a4p": {"enabled": False}})
    route = _activate_execution_context()
    assert route is not None
    wrong_route = AuthorizerRoute(
        session_id="other-session",
        app_id=route.app_id,
        agent_ref_mode=route.agent_ref_mode,
        agent_ref_id=route.agent_ref_id,
    )
    monkeypatch.setattr(
        runtime.server,
        "webauthn_registration_options",
        lambda _request: {
            "registrationRequestId": "registration-1",
            "options": {"challenge": "challenge-1"},
        },
    )

    runtime.webauthn_registration_options(route)

    with pytest.raises(
        ValueError,
        match="WebAuthn registration route mismatch",
    ):
        runtime.verify_webauthn_registration(
            registration_request_id="registration-1",
            credential={"id": "credential-1"},
            route=wrong_route,
        )
    assert "registration-1" in runtime._webauthn_registration_routes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cron_job_id", "validity_seconds", "expected_validity"),
    [(None, None, 3600), ("cron-1", None, 2592000),
     (None, 60, 60), ("cron-1", 120, 120)],
)
async def test_a4p_request_intent_authorization_uses_action_only_intent(
    monkeypatch: pytest.MonkeyPatch,
    cron_job_id: str | None,
    validity_seconds: int | None,
    expected_validity: int,
) -> None:
    runtime = A4PRuntime({"a4p": {"enabled": True}})
    captured: dict[str, dict] = {}

    async def _prepare_intent_authorization(request: dict):
        captured["request"] = request
        return IntentAuthorizationResponse(approved=False)

    monkeypatch.setattr(
        runtime.server,
        "prepare_intent_authorization",
        _prepare_intent_authorization,
    )

    await runtime.request_intent_authorization(
        session_id="session-1",
        channel_id="web",
        authorizer_route=_activate_execution_context(),
        identity=A4PIdentity(agent_id="agent-1"),
        actions=[{"name": "bash", "params": {"command": "pwd"}}],
        validity_seconds=validity_seconds,
        cron_job_id=cron_job_id,
    )

    assert captured["request"]["validitySeconds"] == expected_validity
    assert captured["request"]["userId"] == INTERNAL_A4P_USER_ID
    assert captured["request"]["intent"] == {
        "actions": [{"name": "bash", "params": {"command": "pwd"}}],
    }


@pytest.mark.asyncio
async def test_a4p_request_intent_authorization_uses_explicit_prepare_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = A4PRuntime({"a4p": {"enabled": True}})
    seen: dict[str, dict] = {}

    prepare_intent_authorization = runtime.server.prepare_intent_authorization
    complete_intent_authorization = runtime.server.complete_intent_authorization

    async def _prepare(request: dict):
        seen["prepareRequest"] = request
        return await prepare_intent_authorization(request)

    async def _complete(request: dict):
        seen["completeRequest"] = request
        return await complete_intent_authorization(request)

    async def _approve(
        request: WebAuthorizationRequest,
    ) -> UserAuthorizationResponse:
        seen["uiContext"] = request.ui_context
        return UserAuthorizationResponse(
            approved=True,
            signedMandate=approve_user_mandate(request.mandate),
        )

    runtime.authorizer.request_authorization = _approve
    monkeypatch.setattr(runtime.server, "prepare_intent_authorization", _prepare)
    monkeypatch.setattr(runtime.server, "complete_intent_authorization", _complete)

    payload = await runtime.request_intent_authorization(
        session_id="session-1",
        channel_id="web",
        authorizer_route=_activate_execution_context(),
        identity=A4PIdentity(agent_id="agent-1"),
        actions=[{"name": "bash", "params": {"command": "pwd"}}],
        validity_seconds=60,
    )

    assert seen["uiContext"]["kind"] == "intent"
    assert seen["uiContext"]["sessionId"] == "session-1"
    assert seen["uiContext"]["channelId"] == "web"
    assert seen["prepareRequest"]["intent"]["actions"][0]["name"] == "bash"
    assert payload["requestId"] == seen["completeRequest"]["signedMandate"]["mandateId"]
    assert seen["completeRequest"]["signedMandate"]["signatures"]["user"] == {}
    assert payload["approved"] is True
    assert runtime._session_tokens["session-1"][0]["tokenId"] == payload["intentToken"]["tokenId"]


@pytest.mark.asyncio
async def test_a4p_request_intent_authorization_rejection_skips_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = A4PRuntime({"a4p": {"enabled": True}})
    complete_called = False
    prepared = await runtime.server.prepare_intent_authorization(
        {
            "agentId": "agent-1",
            "userId": INTERNAL_A4P_USER_ID,
            "intent": {
                "actions": [{"name": "bash", "params": {"command": "pwd"}}]
            },
            "validitySeconds": 60,
        }
    )

    async def _prepare(_request: dict) -> IntentAuthorizationResponse:
        return prepared

    async def _reject(
        _request: WebAuthorizationRequest,
    ) -> UserAuthorizationResponse:
        return UserAuthorizationResponse(
            approved=False,
            rejectReason="User rejected test authorization",
        )

    async def _complete(_request: dict) -> IntentAuthorizationResponse:
        nonlocal complete_called
        complete_called = True
        raise AssertionError("rejected authorization must not be completed")

    monkeypatch.setattr(runtime.server, "prepare_intent_authorization", _prepare)
    monkeypatch.setattr(runtime.server, "complete_intent_authorization", _complete)
    runtime.authorizer.request_authorization = _reject

    payload = await runtime.request_intent_authorization(
        session_id="session-1",
        channel_id="web",
        authorizer_route=_activate_execution_context(),
        identity=A4PIdentity(agent_id="agent-1"),
        actions=[{"name": "bash", "params": {"command": "pwd"}}],
        validity_seconds=60,
    )

    assert complete_called is False
    assert payload["approved"] is False
    assert payload["rejectReason"] == "User rejected test authorization"
    assert payload["verificationResult"]["code"] == "AUTHORIZATION_REJECTED"
    assert runtime._session_tokens == {}


@pytest.mark.asyncio
async def test_a4p_cron_authorization_exposes_target_and_uses_cron_validity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        a4p_display,
        "get_config",
        lambda: {"preferred_language": "zh"},
    )
    runtime = A4PRuntime({"a4p": {"enabled": True}})
    seen: dict[str, dict] = {}

    async def _authorize(
        request: WebAuthorizationRequest,
    ) -> UserAuthorizationResponse:
        seen["uiContext"] = request.ui_context
        seen["mandate"] = request.mandate
        return UserAuthorizationResponse(
            approved=False,
            rejectReason="test",
        )

    runtime.authorizer.request_authorization = _authorize
    target = {
        "type": "cronJob",
        "cronJobId": "cron-job-1",
        "name": "daily report",
        "description": "Generate and write the daily report",
        "persistent": True,
        "schedule": {"cronExpression": "0 9 * * *", "timezone": "Asia/Shanghai"},
    }

    await runtime.request_intent_authorization(
        session_id="session-1",
        channel_id="web",
        authorizer_route=_activate_execution_context(),
        identity=A4PIdentity(agent_id="agent-1"),
        actions=[{"name": "bash", "params": {"command": "pwd"}}],
        cron_job_id="cron-job-1",
        cron_authorization_target=target,
    )

    assert seen["uiContext"]["cronJobId"] == "cron-job-1"
    assert seen["uiContext"]["authorizationTarget"] == target
    display_text = seen["mandate"]["displayText"]
    assert "定时任务\n任务 ID：cron-job-1" in display_text
    assert "任务名称：" not in display_text
    assert "任务内容：Generate and write the daily report" in display_text
    assert "执行计划：0 9 * * *（Asia/Shanghai）" in display_text
    valid_time = seen["mandate"]["validTime"]
    start = calendar.timegm(time.strptime(valid_time["start"], "%Y-%m-%dT%H:%M:%SZ"))
    end = calendar.timegm(time.strptime(valid_time["end"], "%Y-%m-%dT%H:%M:%SZ"))
    assert end - start == DEFAULT_CRON_INTENT_VALIDITY_SECONDS


@pytest.mark.asyncio
async def test_a4p_tool_rejects_non_web_channel_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(a4p_tools_module, "get_config", lambda: {"a4p": {"enabled": True}})
    _activate_execution_context(channel_id="__cron__")
    result = await request_a4p_intent_authorization(
        actions=[{"name": "bash", "params": {"command": "pwd"}}],
        _session_id="session-1",
    )

    assert result["ok"] is False
    assert result["code"] == "A4P_WEB_SESSION_REQUIRED"


@pytest.mark.asyncio
async def test_a4p_tool_uses_context_activated_after_worker_task_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    proceed = asyncio.Event()
    captured: dict[str, Any] = {}

    class _Runtime:
        async def request_intent_authorization(self, **kwargs):
            captured.update(kwargs)
            return {
                "requestId": "request-1",
                "approved": True,
                "intentToken": {
                    "tokenId": "token-1",
                    "subject": {"type": "agent", "id": "agent:agent-1"},
                    "intent": {"actions": kwargs["actions"]},
                    "expireAt": "2099-01-01T00:00:00Z",
                },
            }

    async def _long_lived_worker() -> dict[str, Any]:
        started.set()
        await proceed.wait()
        return await request_a4p_intent_authorization(
            actions=[{"name": "bash", "params": {"command": "pwd"}}],
            _session_id="session-1",
        )

    monkeypatch.setattr(a4p_tools_module, "get_config", lambda: {"a4p": {"enabled": True}})
    monkeypatch.setattr(a4p_tools_module, "get_a4p_runtime", lambda: _Runtime())
    worker = asyncio.create_task(_long_lived_worker())
    await started.wait()

    route = _activate_execution_context()
    proceed.set()
    result = await worker

    assert result["ok"] is True
    assert captured["authorizer_route"] == route


@pytest.mark.asyncio
async def test_cancelled_authorization_wait_removes_pending_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = A4PRuntime(
        {
            "a4p": {
                "enabled": True,
            }
        }
    )

    async def _send(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(runtime.authorizer, "_send_push", _send)
    route = _activate_execution_context()
    assert route is not None
    request = WebAuthorizationRequest(
        request_id="pending-1",
        mandate={"mandateId": "pending-1"},
        signing_options={},
        ui_context={"sessionId": "session-1", "kind": "intent"},
        route=route,
    )
    task = asyncio.create_task(runtime.authorizer.request_authorization(request))
    await asyncio.sleep(0)

    assert "pending-1" in runtime.authorizer._pending
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert runtime.authorizer._pending == {}


@pytest.mark.asyncio
async def test_disabling_a4p_cancels_pending_and_pushes_terminal_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = A4PRuntime({"a4p": {"enabled": True}})
    monkeypatch.setattr(a4p_runtime, "_runtime", runtime)
    route = _activate_execution_context()
    assert route is not None
    sent: list[dict[str, Any]] = []

    class _Server:
        async def send_push(self, message: dict[str, Any]) -> None:
            sent.append(message)

    class _AgentWebSocketServer:
        @staticmethod
        def get_instance() -> _Server:
            return _Server()

    import jiuwenswarm.server.agent_ws_server as agent_ws_server

    monkeypatch.setattr(
        agent_ws_server,
        "AgentWebSocketServer",
        _AgentWebSocketServer,
    )
    task = asyncio.create_task(
        runtime.authorizer.request_authorization(
            WebAuthorizationRequest(
                request_id="pending-disable",
                mandate={"mandateId": "pending-disable"},
                signing_options={},
                ui_context={
                    "kind": "intent",
                    "sessionId": "session-1",
                    "channelId": "web",
                },
                route=route,
            )
        )
    )
    await asyncio.sleep(0)

    cancelled = await a4p_runtime.disable_a4p_runtime()
    response = await task

    assert cancelled == 1
    assert response.approved is False
    assert response.rejectReason == "A4P was disabled while authorization was pending"
    assert a4p_runtime._runtime is None
    assert sent[-1]["payload"] == {
        "event_type": "a4p.authorization_terminated",
        "requestId": "pending-disable",
        "kind": "intent",
        "uiContext": {
            "kind": "intent",
            "sessionId": "session-1",
            "channelId": "web",
        },
        "reason": "A4P was disabled while authorization was pending",
        "code": "A4P_DISABLED",
    }
    late_response = await runtime.authorizer.request_authorization(
        WebAuthorizationRequest(
            request_id="late-request",
            mandate={"mandateId": "late-request"},
            signing_options={},
            ui_context={"sessionId": "session-1"},
            route=route,
        )
    )
    assert late_response.approved is False
    assert late_response.rejectReason == "A4P authorizer is disabled"


@pytest.mark.asyncio
async def test_reconfigure_signature_mode_preserves_issued_session_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = A4PRuntime(
        {"a4p": {"enabled": True, "require_user_signature": False}}
    )
    runtime.store_intent_token(
        "session-1",
        {
            "tokenId": "token-1",
            "intent": {"actions": []},
            "expireAt": "2099-01-01T00:00:00Z",
        },
    )
    monkeypatch.setattr(a4p_runtime, "_runtime", runtime)
    route = _activate_execution_context()
    assert route is not None

    async def _send(*_args, **_kwargs) -> None:
        return None

    runtime.authorizer._send_push = _send
    pending_task = asyncio.create_task(
        runtime.authorizer.request_authorization(
            WebAuthorizationRequest(
                request_id="pending-reconfigure",
                mandate={"mandateId": "pending-reconfigure"},
                signing_options={},
                ui_context={"sessionId": "session-1"},
                route=route,
            )
        )
    )
    await asyncio.sleep(0)

    cancelled = await a4p_runtime.reconfigure_a4p_runtime(
        {"a4p": {"enabled": True, "require_user_signature": True}}
    )
    pending_response = await pending_task
    replacement = a4p_runtime._runtime

    assert cancelled == 1
    assert pending_response.rejectReason == (
        "A4P configuration changed while authorization was pending"
    )
    assert replacement is not None
    assert replacement is not runtime
    assert replacement.require_user_signature is True
    assert replacement.export_session_tokens()["session-1"][0]["tokenId"] == "token-1"


def test_agent_server_payload_preserves_agent_ref() -> None:
    from jiuwenswarm.server.agent_ws_server import _payload_to_request

    request = _payload_to_request(
        {
            "request_id": "request-1",
            "channel_id": "web",
            "session_id": "session-1",
            "req_method": "chat.send",
            "params": {"mode": "team"},
            "app_id": "app-1",
            "agent_ref": {"mode": "team", "id": "research"},
            "metadata": {"ws_id": "ws-1"},
        }
    )

    assert request.agent_ref == {"mode": "team", "id": "research"}
    assert request.metadata == {"ws_id": "ws-1", "app_id": "app-1"}


@pytest.mark.asyncio
async def test_a4p_cron_authorization_target_includes_job_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.gateway.cron.store import CronJobStore

    async def _get_job(_store, cron_job_id: str):
        return SimpleNamespace(
            id=cron_job_id,
            name="daily report",
            description="Write a daily report to the workspace",
            enabled=False,
            cron_expr="0 9 * * *",
            timezone="Asia/Shanghai",
        )

    monkeypatch.setattr(CronJobStore, "get_job", _get_job)

    target = await a4p_tools_module._resolve_cron_authorization_target("cron-job-1")

    assert target is not None
    assert target["description"] == "Write a daily report to the workspace"


@pytest.mark.asyncio
async def test_a4p_tool_rejects_unknown_cron_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _missing_target(cron_job_id: str):
        _ = cron_job_id
        return None

    monkeypatch.setattr(a4p_tools_module, "get_config", lambda: {"a4p": {"enabled": True}})
    monkeypatch.setattr(a4p_tools_module, "_resolve_cron_authorization_target", _missing_target)
    _activate_execution_context()
    result = await request_a4p_intent_authorization(
        actions=[{"name": "bash", "params": {"command": "pwd"}}],
        cron_job_id="missing-job",
        _session_id="session-1",
    )

    assert result["ok"] is False
    assert result["code"] == "CRON_JOB_NOT_FOUND"


@pytest.mark.asyncio
async def test_a4p_tool_rejects_enabled_cron_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _enabled_target(cron_job_id: str):
        return {
            "type": "cronJob",
            "cronJobId": cron_job_id,
            "enabled": True,
        }

    monkeypatch.setattr(a4p_tools_module, "get_config", lambda: {"a4p": {"enabled": True}})
    monkeypatch.setattr(
        a4p_tools_module,
        "_resolve_cron_authorization_target",
        _enabled_target,
    )
    _activate_execution_context()
    result = await request_a4p_intent_authorization(
        actions=[{"name": "bash", "params": {"command": "pwd"}}],
        cron_job_id="enabled-job",
        _session_id="session-1",
    )

    assert result["ok"] is False
    assert result["code"] == "CRON_JOB_MUST_BE_DISABLED"


@pytest.mark.asyncio
async def test_a4p_cron_authorization_success_requires_explicit_enable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _disabled_target(cron_job_id: str):
        return {
            "type": "cronJob",
            "cronJobId": cron_job_id,
            "enabled": False,
        }

    class _Runtime:
        async def request_intent_authorization(self, **kwargs):
            assert kwargs["cron_job_id"] == "disabled-job"
            return {
                "requestId": "request-1",
                "approved": True,
                "intentToken": {
                    "tokenId": "token-1",
                    "subject": {"type": "agent", "id": "agent:agent-1"},
                    "intent": {"actions": kwargs["actions"]},
                    "expireAt": "2099-01-01T00:00:00Z",
                },
            }

    monkeypatch.setattr(a4p_tools_module, "get_config", lambda: {"a4p": {"enabled": True}})
    monkeypatch.setattr(
        a4p_tools_module,
        "_resolve_cron_authorization_target",
        _disabled_target,
    )
    monkeypatch.setattr(a4p_tools_module, "get_a4p_runtime", lambda: _Runtime())
    _activate_execution_context()
    result = await request_a4p_intent_authorization(
        actions=[{"name": "bash", "params": {"command": "pwd"}}],
        cron_job_id="disabled-job",
        _session_id="session-1",
    )

    assert result["ok"] is True
    assert result["token"]["intent"]["actions"] == [
        {"name": "bash", "params": {"command": "pwd"}}
    ]
    assert result["cronJobEnabled"] is False
    assert result["nextAction"] == {
        "tool": "cron_toggle_job",
        "arguments": {"job_id": "disabled-job", "enabled": True},
    }


@pytest.mark.asyncio
async def test_a4p_intent_token_matches_session_action_and_identity() -> None:
    runtime = A4PRuntime({"a4p": {"enabled": True}})
    identity = A4PIdentity(agent_id="agent-1")

    prepared = await runtime.server.prepare_intent_authorization(
        {
            "agentId": identity.agent_id,
            "userId": INTERNAL_A4P_USER_ID,
            "intent": {"actions": [{"name": "read_file", "params": {"path": "/tmp/a.txt"}}]},
            "validitySeconds": 60,
        }
    )
    signed = approve_user_mandate(prepared.mandate or {})
    completed = await runtime.server.complete_intent_authorization(
        {"signedMandate": signed}
    )
    assert completed.intentToken is not None
    runtime.store_intent_token("session-1", completed.intentToken)

    matched = await runtime.find_valid_intent_token(
        session_id="session-1",
        identity=identity,
        action="read_file",
        params={"path": "/tmp/a.txt"},
    )
    assert matched is not None

    mismatch = await runtime.find_valid_intent_token(
        session_id="session-1",
        identity=identity,
        action="read_file",
        params={"path": "/tmp/b.txt"},
    )
    assert mismatch is None

    wrong_agent = await runtime.find_valid_intent_token(
        session_id="session-1",
        identity=A4PIdentity(agent_id="agent-2"),
        action="read_file",
        params={"path": "/tmp/a.txt"},
    )
    assert wrong_agent is None


@pytest.mark.asyncio
async def test_a4p_session_tokens_accumulate_across_authorization_stages() -> None:
    runtime = A4PRuntime({"a4p": {"enabled": True}})
    identity = A4PIdentity(agent_id="agent-1")

    async def _issue_stage_token(command: str) -> dict[str, Any]:
        prepared = await runtime.server.prepare_intent_authorization(
            {
                "agentId": identity.agent_id,
                "userId": INTERNAL_A4P_USER_ID,
                "intent": {
                    "actions": _normalize_actions(
                        [{"name": "bash", "params": {"command": command}}]
                    )
                },
                "validitySeconds": 60,
            }
        )
        signed = approve_user_mandate(prepared.mandate or {})
        completed = await runtime.server.complete_intent_authorization(
            {"signedMandate": signed}
        )
        assert completed.intentToken is not None
        runtime.store_intent_token("session-1", completed.intentToken)
        return completed.intentToken

    first_command = "python scripts/discover_endpoint.py --json"
    second_command = "python scripts/configure.py --endpoint http://127.0.0.1:9333"
    first_token = await _issue_stage_token(first_command)
    second_token = await _issue_stage_token(second_command)

    stored = runtime.export_session_tokens()["session-1"]
    assert [token["tokenId"] for token in stored] == [
        first_token["tokenId"],
        second_token["tokenId"],
    ]
    assert await runtime.find_valid_intent_token(
        session_id="session-1",
        identity=identity,
        action="bash",
        params={"command": first_command},
    ) is not None
    assert await runtime.find_valid_intent_token(
        session_id="session-1",
        identity=identity,
        action="bash",
        params={"command": second_command},
    ) is not None
    assert await runtime.find_valid_intent_token(
        session_id="session-1",
        identity=identity,
        action="bash",
        params={"command": "python scripts/unapproved.py"},
    ) is None


@pytest.mark.asyncio
async def test_a4p_intent_token_subset_glob_matches_write_file_content() -> None:
    """SDK glob support: path constraint "*.md" should match "零点之后.md".

    Locks in the a4p SDK update (commit 123a7959) that introduced
    `_param_value_matches` with fnmatch glob for string constraints.
    This mirrors the real JiuwenSwarm scenario where an Agent requests
    an intent token for write_file with a directory glob pattern.
    """
    runtime = A4PRuntime({"a4p": {"enabled": True}})
    identity = A4PIdentity(agent_id="agent-1")

    prepared = await runtime.server.prepare_intent_authorization(
        {
            "agentId": identity.agent_id,
            "userId": INTERNAL_A4P_USER_ID,
            "intent": {
                "actions": _normalize_actions(
                    [
                        {
                            "name": "write_file",
                            "params": {
                                "file_path": "/Users/yukuan/.jiuwenswarm/agent/workspace/科幻小说/*.md"
                            },
                        }
                    ]
                )
            },
            "validitySeconds": 60,
        }
    )
    signed = approve_user_mandate(prepared.mandate or {})
    completed = await runtime.server.complete_intent_authorization(
        {"signedMandate": signed}
    )
    assert completed.intentToken is not None
    runtime.store_intent_token("session-1", completed.intentToken)

    matched = await runtime.find_valid_intent_token(
        session_id="session-1",
        identity=identity,
        action="write_file",
        params={
            "file_path": "/Users/yukuan/.jiuwenswarm/agent/workspace/科幻小说/零点之后.md",
            "content": "# 《零点之后》\n\n正文...",
        },
    )
    assert matched is not None

    non_match = await runtime.find_valid_intent_token(
        session_id="session-1",
        identity=identity,
        action="write_file",
        params={
            "file_path": "/Users/yukuan/.jiuwenswarm/agent/workspace/其他/xx.txt",
            "content": "",
        },
    )
    assert non_match is None


@pytest.mark.asyncio
async def test_a4p_intent_token_allow_extra_params_tolerates_unlisted_keys() -> None:
    """SDK allowExtraParams: bash call with `description` not in token constraint should pass.

    Locks in the a4p SDK update (commit db17f3d5) that introduced
    `allowExtraParams` on intent action specs. This mirrors the real
    JiuwenSwarm scenario where an Agent requests a bash intent token with
    only `command` constrained, and the actual bash call also carries
    `description` / `timeout` etc.
    """
    runtime = A4PRuntime({"a4p": {"enabled": True}})
    identity = A4PIdentity(agent_id="agent-1")

    prepared = await runtime.server.prepare_intent_authorization(
        {
            "agentId": identity.agent_id,
            "userId": INTERNAL_A4P_USER_ID,
            "intent": {
                "actions": [
                    {
                        "name": "bash",
                        "params": {
                            "command": "mkdir -p /Users/yukuan/.jiuwenswarm/agent/workspace/科幻小说"
                        },
                        "allowExtraParams": True,
                    }
                ]
            },
            "validitySeconds": 60,
        }
    )
    signed = approve_user_mandate(prepared.mandate or {})
    completed = await runtime.server.complete_intent_authorization(
        {"signedMandate": signed}
    )
    assert completed.intentToken is not None
    runtime.store_intent_token("session-1", completed.intentToken)

    matched = await runtime.find_valid_intent_token(
        session_id="session-1",
        identity=identity,
        action="bash",
        params={
            "command": "mkdir -p /Users/yukuan/.jiuwenswarm/agent/workspace/科幻小说",
            "description": "创建科幻小说目录",
            "timeout": 30,
        },
    )
    assert matched is not None

    redundantly_quoted_path = await runtime.find_valid_intent_token(
        session_id="session-1",
        identity=identity,
        action="bash",
        params={
            "command": (
                'mkdir -p "/Users/yukuan/.jiuwenswarm/agent/workspace/科幻小说"'
            ),
            "description": "创建科幻小说目录",
        },
    )
    assert redundantly_quoted_path is not None

    appended_command = await runtime.find_valid_intent_token(
        session_id="session-1",
        identity=identity,
        action="bash",
        params={
            "command": (
                "mkdir -p /Users/yukuan/.jiuwenswarm/agent/workspace/科幻小说 "
                "&& ls -la /Users/yukuan/.jiuwenswarm/agent/workspace/科幻小说"
            ),
            "description": "创建科幻小说目录",
        },
    )
    assert appended_command is None

    quoted_glob = await runtime.find_valid_intent_token(
        session_id="session-1",
        identity=identity,
        action="bash",
        params={
            "command": 'mkdir -p "/Users/yukuan/.jiuwenswarm/agent/workspace/科幻*"',
        },
    )
    assert quoted_glob is None

    wrong_command = await runtime.find_valid_intent_token(
        session_id="session-1",
        identity=identity,
        action="bash",
        params={
            "command": "rm -rf /",
            "description": "",
        },
    )
    assert wrong_command is None


@pytest.mark.asyncio
async def test_a4p_verification_does_not_rewrite_signed_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = A4PRuntime({"a4p": {"enabled": True}})
    token = {
        "tokenId": "signed-token",
        "intent": {
            "actions": [
                {
                    "name": "bash",
                    "params": {"command": 'mkdir -p "/tmp/a4p"'},
                    "allowExtraParams": True,
                }
            ]
        },
        "signature": "signed-over-the-original-scope",
    }
    captured: dict[str, Any] = {}

    async def _verify(request: dict[str, Any]) -> SimpleNamespace:
        captured.update(request)
        return SimpleNamespace(valid=True)

    monkeypatch.setattr(runtime.server, "verify_intent_token", _verify)

    valid = await runtime._verify_intent_token(
        token=token,
        identity=A4PIdentity(agent_id="agent-1"),
        action="bash",
        params={"command": 'mkdir -p "/tmp/a4p"'},
    )

    assert valid is True
    assert captured["token"] is token
    assert captured["token"]["intent"]["actions"][0]["params"]["command"] == (
        'mkdir -p "/tmp/a4p"'
    )
    assert captured["expected"]["params"]["command"] == "mkdir -p /tmp/a4p"


@pytest.mark.asyncio
async def test_a4p_cron_intent_token_persists_and_matches_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(a4p_runtime, "get_config_dir", lambda: tmp_path)
    runtime = A4PRuntime({"a4p": {"enabled": True}})
    identity = A4PIdentity(agent_id="agent-1")

    prepared = await runtime.server.prepare_intent_authorization(
        {
            "agentId": identity.agent_id,
            "userId": INTERNAL_A4P_USER_ID,
            "intent": {"actions": [{"name": "bash", "params": {"command": "pwd"}}]},
            "validitySeconds": 60,
        }
    )
    signed = approve_user_mandate(prepared.mandate or {})
    completed = await runtime.server.complete_intent_authorization(
        {"signedMandate": signed}
    )
    assert completed.intentToken is not None

    runtime.store_cron_intent_token("cron-job-1", completed.intentToken)

    store_path = tmp_path / "a4p" / CRON_INTENT_TOKENS_DB_FILENAME
    with closing(sqlite3.connect(store_path)) as connection:
        persisted = connection.execute(
            "SELECT token_json FROM cron_intent_tokens WHERE cron_job_id = ?",
            ("cron-job-1",),
        ).fetchone()
    assert persisted is not None
    assert json.loads(persisted[0])["tokenId"] == completed.intentToken["tokenId"]

    reloaded = A4PRuntime({"a4p": {"enabled": True}})
    matched = await reloaded.find_valid_cron_intent_token(
        cron_job_id="cron-job-1",
        identity=identity,
        action="bash",
        params={"command": "pwd"},
    )
    assert matched is not None

    missing_job = await reloaded.find_valid_cron_intent_token(
        cron_job_id="cron-job-2",
        identity=identity,
        action="bash",
        params={"command": "pwd"},
    )
    assert missing_job is None

    wrong_params = await reloaded.find_valid_cron_intent_token(
        cron_job_id="cron-job-1",
        identity=identity,
        action="bash",
        params={"command": "ls"},
    )
    assert wrong_params is None


@pytest.mark.asyncio
async def test_a4p_cron_token_allows_repeated_checks_across_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(a4p_runtime, "get_config_dir", lambda: tmp_path)
    runtime = A4PRuntime({"a4p": {"enabled": True}})
    identity = A4PIdentity(agent_id="agent-1")
    prepared = await runtime.server.prepare_intent_authorization(
        {
            "agentId": identity.agent_id,
            "userId": INTERNAL_A4P_USER_ID,
            "intent": {"actions": [{"name": "bash", "params": {"command": "pwd"}}]},
            "validitySeconds": 60,
        }
    )
    signed = approve_user_mandate(prepared.mandate or {})
    completed = await runtime.server.complete_intent_authorization(
        {"signedMandate": signed}
    )
    assert completed.intentToken is not None
    runtime.store_cron_intent_token("cron-job-1", completed.intentToken)

    first = await runtime.find_valid_cron_intent_token(
        cron_job_id="cron-job-1",
        identity=identity,
        action="bash",
        params={"command": "pwd"},
    )
    assert first is not None
    second = await runtime.find_valid_cron_intent_token(
        cron_job_id="cron-job-1",
        identity=identity,
        action="bash",
        params={"command": "pwd"},
    )
    assert second is not None

    reloaded = A4PRuntime({"a4p": {"enabled": True}})
    after_restart = await reloaded.find_valid_cron_intent_token(
        cron_job_id="cron-job-1",
        identity=identity,
        action="bash",
        params={"command": "pwd"},
    )
    assert after_restart is not None
    assert sorted(path.name for path in (tmp_path / "a4p").iterdir()) == [
        CRON_INTENT_TOKENS_DB_FILENAME
    ]


def test_a4p_cron_authorization_summary_exposes_scope_without_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(a4p_runtime, "get_config_dir", lambda: tmp_path)
    runtime = A4PRuntime({"a4p": {"enabled": True}})
    token = {
        "tokenId": "token-secret-value",
        "subject": {"type": "agent", "id": "agent:agent-1"},
        "intent": {
            "actions": [
                {
                    "name": "write_file",
                    "params": {"file_path": "/workspace/reports/*.md"},
                    "allowExtraParams": True,
                }
            ],
        },
        "expireAt": "2099-01-01T00:00:00Z",
    }
    runtime.store_cron_intent_token("job-1", token)

    summary = runtime.cron_intent_authorization_summary("job-1")

    assert summary == {
        "cronJobId": "job-1",
        "tokenId": "token-secret",
        "actions": token["intent"]["actions"],
        "expireAt": "2099-01-01T00:00:00Z",
    }
    assert "token" not in summary


def test_remove_cron_intent_token_without_initializing_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(a4p_runtime, "get_config_dir", lambda: tmp_path)
    monkeypatch.setattr(a4p_runtime, "_runtime", None)
    path = tmp_path / "a4p" / CRON_INTENT_TOKENS_DB_FILENAME
    store = SQLiteCronIntentTokenStore(path)
    token = {"tokenId": "token", "expireAt": "2099-01-01T00:00:00Z"}
    store.put("job-1", token)
    store.put("job-2", token)

    remove_cron_intent_token_for_job("job-1")

    assert store.get("job-1") is None
    assert store.get("job-2") == token


def test_a4p_cron_token_store_preserves_cross_instance_updates(tmp_path) -> None:
    path = tmp_path / "a4p" / CRON_INTENT_TOKENS_DB_FILENAME
    first = SQLiteCronIntentTokenStore(path)
    second = SQLiteCronIntentTokenStore(path)
    first.put(
        "job-1",
        {"tokenId": "token-1", "expireAt": "2099-01-01T00:00:00Z"},
    )
    second.put(
        "job-2",
        {"tokenId": "token-2", "expireAt": "2099-01-01T00:00:00Z"},
    )
    first.remove("job-1")

    assert first.get("job-1") is None
    assert second.get("job-2") == {
        "tokenId": "token-2",
        "expireAt": "2099-01-01T00:00:00Z",
    }


def test_a4p_cron_token_stale_cleanup_does_not_delete_concurrent_update(
    tmp_path,
) -> None:
    store = SQLiteCronIntentTokenStore(
        tmp_path / "a4p" / CRON_INTENT_TOKENS_DB_FILENAME
    )
    old = {"tokenId": "old", "expireAt": "2000-01-01T00:00:00Z"}
    new = {"tokenId": "new", "expireAt": "2099-01-01T00:00:00Z"}
    store.put("job-1", old)
    store.put("job-1", new)

    store._remove_if_unchanged(
        "job-1",
        json.dumps(old, ensure_ascii=False, separators=(",", ":")),
    )

    assert store.get("job-1") == new


def test_a4p_normalize_actions_uses_internal_subset_semantics() -> None:
    actions = [
        {
            "name": "bash",
            "params": {
                "command": "mkdir -p /Users/yukuan/.jiuwenswarm/agent/workspace/科幻小说",
                "allowExtraParams": True,
            },
        }
    ]
    normalized = _normalize_actions(actions)
    assert normalized == [
        {
            "name": "bash",
            "params": {"command": "mkdir -p /Users/yukuan/.jiuwenswarm/agent/workspace/科幻小说"},
            "allowExtraParams": True,
        }
    ]

    # Obsolete input at either level is ignored; the internal SDK value is true.
    actions_mixed = [
        {
            "name": "bash",
            "params": {"command": "ls", "allowExtraParams": "ignored"},
            "allowExtraParams": False,
        }
    ]
    normalized_mixed = _normalize_actions(actions_mixed)
    assert normalized_mixed == [
        {
            "name": "bash",
            "params": {"command": "ls"},
            "allowExtraParams": True,
        }
    ]
    assert "allowExtraParams" not in normalized_mixed[0]["params"]


@pytest.mark.parametrize(
    "action",
    [
        {"name": "bash", "params": {"command": "pwd"}},
        {"name": "mcp_exec_command", "params": {"command": "pwd"}},
        {"name": "create_terminal", "params": {"cmd": "python"}},
        {"name": "write_file", "params": {"file_path": "/workspace/a.txt"}},
        {"name": "edit_file", "params": {"file_path": "/workspace/a.txt"}},
        {"name": "acp_chat", "params": {"agent": "reviewer"}},
    ],
)
def test_a4p_action_scope_policy_accepts_registered_tools(action) -> None:
    assert _normalize_actions([action]) == [
        {
            **action,
            "allowExtraParams": True,
        }
    ]


@pytest.mark.parametrize(
    ("actions", "code", "action_index", "action_name", "required_fields"),
    [
        ([], "A4P_ACTION_SCOPE_INVALID", None, None, []),
        ([{"name": "bash", "params": {}}], "A4P_ACTION_SCOPE_INVALID", 0, "bash", ["command"]),
        (
            [{"name": "bash", "params": {"description": "list files"}}],
            "A4P_ACTION_SCOPE_INVALID",
            0,
            "bash",
            ["command"],
        ),
        ([{"name": "bash", "params": "*"}], "A4P_ACTION_SCOPE_INVALID", 0, "bash", ["command"]),
        (
            [{"name": "write_file", "params": {"path": "/workspace/a.txt"}}],
            "A4P_ACTION_SCOPE_INVALID",
            0,
            "write_file",
            ["file_path"],
        ),
        (
            [{"name": "write_file", "params": {"file_path": "/workspace/stories/"}}],
            "A4P_ACTION_SCOPE_INVALID",
            0,
            "write_file",
            ["file_path"],
        ),
        (
            [{"name": "write_file", "params": {"file_path": "/workspace/*/story.md"}}],
            "A4P_ACTION_SCOPE_INVALID",
            0,
            "write_file",
            ["file_path"],
        ),
        (
            [{"name": "write_file", "params": {"file_path": "*.md"}}],
            "A4P_ACTION_SCOPE_INVALID",
            0,
            "write_file",
            ["file_path"],
        ),
        (
            [{"name": "unknown_tool", "params": {"target": "x"}}],
            "A4P_ACTION_SCOPE_UNSUPPORTED",
            0,
            "unknown_tool",
            [],
        ),
        (
            [{"name": "write", "params": {"path": "/workspace/a.txt"}}],
            "A4P_ACTION_SCOPE_UNSUPPORTED",
            0,
            "write",
            [],
        ),
        (
            [{"name": "search_replace", "params": {"file_path": "/workspace/a.txt"}}],
            "A4P_ACTION_SCOPE_UNSUPPORTED",
            0,
            "search_replace",
            [],
        ),
    ],
)
@pytest.mark.asyncio
async def test_a4p_tool_rejects_invalid_scope_before_runtime(
    monkeypatch: pytest.MonkeyPatch,
    actions,
    code,
    action_index,
    action_name,
    required_fields,
) -> None:
    class _Runtime:
        async def request_intent_authorization(self, **kwargs):
            raise AssertionError("invalid scope must not reach the A4P runtime")

    monkeypatch.setattr(a4p_tools_module, "get_config", lambda: {"a4p": {"enabled": True}})
    monkeypatch.setattr(a4p_tools_module, "get_a4p_runtime", lambda: _Runtime())
    _activate_execution_context()
    result = await request_a4p_intent_authorization(
        actions=actions,
        _session_id="session-1",
    )

    assert result == {
        "ok": False,
        "error": result["error"],
        "code": code,
        "actionIndex": action_index,
        "actionName": action_name,
        "requiredScopeFields": required_fields,
    }


@pytest.mark.asyncio
async def test_a4p_authorization_push_uses_session_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = A4PRuntime({"a4p": {"enabled": True}})
    route = _activate_execution_context()
    sent: list[dict] = []

    class _Server:
        async def send_push(self, msg: dict) -> None:
            sent.append(msg)

    class _AgentWebSocketServer:
        @staticmethod
        def get_instance() -> _Server:
            return _Server()

    import jiuwenswarm.server.agent_ws_server as agent_ws_server

    monkeypatch.setattr(agent_ws_server, "AgentWebSocketServer", _AgentWebSocketServer)
    assert route is not None
    await runtime.authorizer._send_push(
        WebAuthorizationRequest(
            request_id="req-1",
            mandate={
                "type": "a4p/v1/intent-mandate",
                "mandateId": "req-1",
                "intent": {
                    "actions": [
                        {"name": "bash", "params": {"command": "pwd"}},
                    ]
                },
            },
            signing_options={"signatureMethod": "test"},
            ui_context={"kind": "intent", "sessionId": "session-1", "channelId": "web"},
            route=route,
        ),
        event_type="a4p.authorization_request",
    )

    assert sent[0]["channel_id"] == "web"
    assert sent[0]["session_id"] == "session-1"
    assert sent[0]["payload"]["event_type"] == "a4p.authorization_request"
    assert sent[0]["payload"]["requestId"] == "req-1"
    assert sent[0]["payload"]["mandate"]["type"] == "a4p/v1/intent-mandate"
    assert sent[0]["payload"]["mandate"]["mandateId"] == "req-1"
    assert sent[0]["payload"]["signingOptions"] == {"signatureMethod": "test"}
