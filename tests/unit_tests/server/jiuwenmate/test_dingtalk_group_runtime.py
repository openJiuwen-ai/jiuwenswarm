"""Host-owned DingTalk history identity and runtime boundary checks."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.jiuwenmate.dingtalk_cli_local_provider import LocalDingTalkCliRuntime
from jiuwenswarm.server.jiuwenmate.dingtalk_cli_runtime import DingTalkCliRuntimeService, DingTalkCliScope
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError


def _local_runtime(tmp_path: Path, *, configured: bool = True) -> LocalDingTalkCliRuntime:
    apps = [{"client_id": "test-app", "client_secret": "test-secret", "is_default": True}] if configured else []
    return LocalDingTalkCliRuntime(
        root=tmp_path, workspace_root=tmp_path / "workspaces", dws_path=tmp_path / "dws",
        config_loader=lambda: {"channels": {"dingtalk": {"apps": apps}}},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('code', ['org_access_denied', 'authorization_expired', 'authorization_failed', 'private-output'])
async def test_user_authorization_only_exposes_known_terminal_error_codes(code):
    async def status(scope):
        return {'status': 'error', 'cli_version': '1', 'error_code': code,
                '_profile': 'private-profile', 'stderr': 'private-output'}

    service = DingTalkCliRuntimeService(runtime=SimpleNamespace(is_available=lambda scope: True,
                                                               dingtalk_auth_status=status))
    result = await service.dingtalk_auth_status(DingTalkCliScope('local', 'employee-a'))
    assert result == ({'status': 'error', 'cli_version': '1'}
                      | ({'error_code': code} if code != 'private-output' else {}))


@pytest.mark.asyncio
async def test_provider_history_identity_reads_only_each_employees_authorized_profile(tmp_path, monkeypatch):
    runtime = _local_runtime(tmp_path)
    calls = []

    async def status(scope):
        calls.append(scope)
        return {"status": "connected", "_profile": f"corp:{scope.agent_id}"}

    monkeypatch.setattr(runtime, "dingtalk_auth_status", status)
    first = DingTalkCliScope("local", "employee-a")
    second = DingTalkCliScope("local", "employee-b")

    first_key = await runtime.group_history_identity(first)
    second_key = await runtime.group_history_identity(second)

    assert first_key == hashlib.sha256(b"corp:employee-a").hexdigest()
    assert second_key == hashlib.sha256(b"corp:employee-b").hexdigest()
    assert first_key != second_key
    assert calls == [first, second]


@pytest.mark.asyncio
async def test_provider_history_identity_without_binding_never_checks_authorization(tmp_path, monkeypatch):
    runtime = _local_runtime(tmp_path, configured=False)

    async def status(scope):
        pytest.fail("An unconfigured employee must not use any user profile")

    monkeypatch.setattr(runtime, "dingtalk_auth_status", status)

    assert await runtime.group_history_identity(DingTalkCliScope("local", "employee-a")) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", [
    {"status": "not_connected"}, {"status": "authorizing"}, {"status": "unavailable"},
    {"status": "connected"}, {"status": "connected", "_profile": ""},
    {"status": "connected", "_profile": " "}, {"status": "connected", "_profile": []},
    {"status": "connected", "_profile": "x" * 513},
])
async def test_provider_history_identity_without_usable_employee_authorization_is_none(tmp_path, monkeypatch, authorization):
    runtime = _local_runtime(tmp_path)

    async def status(scope):
        return authorization

    monkeypatch.setattr(runtime, "dingtalk_auth_status", status)

    assert await runtime.group_history_identity(DingTalkCliScope("local", "employee-a")) is None


@pytest.mark.asyncio
async def test_provider_history_identity_does_not_treat_authorization_errors_as_logged_out(tmp_path, monkeypatch):
    runtime = _local_runtime(tmp_path)

    async def status(scope):
        return {"status": "error"}

    monkeypatch.setattr(runtime, "dingtalk_auth_status", status)
    with pytest.raises(PersistentAgentError) as failure:
        await runtime.group_history_identity(DingTalkCliScope("local", "employee-a"))

    assert failure.value.code == "DINGTALK_CLI_GROUP_HISTORY_UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [None, SimpleNamespace(is_available=lambda scope: False),
                                          SimpleNamespace(is_available=lambda scope: True)])
async def test_service_history_identity_allows_absent_or_unconfigured_optional_provider(provider):
    service = DingTalkCliRuntimeService(runtime=provider)

    assert await service.group_history_identity(DingTalkCliScope("local", "employee-a")) is None


@pytest.mark.asyncio
async def test_service_history_identity_forwards_scope_and_returns_only_hash():
    calls = []

    async def identity(scope):
        calls.append(scope)
        return "a" * 64 if scope.agent_id == "employee-a" else None

    service = DingTalkCliRuntimeService(runtime=SimpleNamespace(
        is_available=lambda scope: True, group_history_identity=identity,
    ))
    first = DingTalkCliScope("local", "employee-a")
    second = DingTalkCliScope("local", "employee-b")

    assert await service.group_history_identity(first) == "a" * 64
    assert await service.group_history_identity(second) is None
    assert calls == [first, second]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["corp:private-profile", "a" * 63, "g" * 64, 123, {}])
async def test_service_history_identity_rejects_non_hash_results(key):
    async def identity(scope):
        return key

    service = DingTalkCliRuntimeService(runtime=SimpleNamespace(
        is_available=lambda scope: True, group_history_identity=identity,
    ))
    with pytest.raises(PersistentAgentError) as failure:
        await service.group_history_identity(DingTalkCliScope("local", "employee-a"))

    assert failure.value.code == "DINGTALK_CLI_GROUP_HISTORY_INVALID"
    assert "private-profile" not in str(failure.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["group_history_identity", "read_group_history"])
async def test_service_group_history_wraps_network_failures_without_credentials(operation):
    async def fail(scope, **params):
        raise OSError("secret-token and private-profile")

    provider = SimpleNamespace(is_available=lambda scope: True)
    setattr(provider, operation, fail)
    service = DingTalkCliRuntimeService(runtime=provider)
    scope = DingTalkCliScope("local", "employee-a")
    with pytest.raises(PersistentAgentError) as failure:
        if operation == "group_history_identity":
            await service.group_history_identity(scope)
        else:
            await service.read_group_history(scope, conversation_id="cid-known", since="2026-10-30T14:00:00+00:00")

    assert failure.value.code == "DINGTALK_CLI_GROUP_HISTORY_UNAVAILABLE"
    assert "secret-token" not in str(failure.value)
    assert "private-profile" not in str(failure.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_id", ["employee-a", "employee-b"])
async def test_service_group_history_forwards_only_trusted_target_boundary_and_employee_scope(agent_id):
    calls = []
    response = {
        "readerKey": "a" * 64, "messages": [], "complete": True, "hasMore": False, "nextCursor": None,
        "botCodes": ["current-bot", "other-bot"],
    }

    async def history(scope, *, conversation_id, since):
        calls.append((scope, conversation_id, since))
        return {**response, "_profile": "private-profile", "access_token": "private-token"}

    service = DingTalkCliRuntimeService(runtime=SimpleNamespace(is_available=lambda scope: True, read_group_history=history))
    scope = DingTalkCliScope("local", agent_id)

    result = await service.read_group_history(scope, conversation_id="cid-known", since="2026-10-30T14:00:00+00:00")

    assert result == response
    assert calls == [(scope, "cid-known", "2026-10-30T14:00:00+00:00")]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [None, SimpleNamespace(is_available=lambda scope: False),
                                          SimpleNamespace(is_available=lambda scope: True)])
async def test_service_group_history_requires_available_provider_method(provider):
    with pytest.raises(PersistentAgentError) as failure:
        await DingTalkCliRuntimeService(runtime=provider).read_group_history(
            DingTalkCliScope("local", "employee-a"), conversation_id="cid-known", since="2026-10-30T14:00:00+00:00",
        )

    assert failure.value.code == "DINGTALK_CLI_GROUP_HISTORY_UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("readerKey", "profile-name"), ("readerKey", "a" * 63), ("readerKey", "g" * 64),
    ("messages", {}), ("complete", 1), ("hasMore", "false"),
    ("botCodes", None), ("botCodes", "current-bot"), ("botCodes", [123]),
    ("botCodes", [""]), ("botCodes", ["invalid code"]),
    ('botSenders', []), ('botSenders', {'unlisted': 'bot-id'}),
    ('botSenders', {'current-bot': 'invalid id'}), ('botSenders', {'current-bot': []}),
])
async def test_service_group_history_rejects_invalid_response_fields(field, value):
    response = {"readerKey": "a" * 64, "messages": [], "complete": True, "hasMore": False, "botCodes": ["current-bot"]}
    response[field] = value

    async def history(scope, *, conversation_id, since):
        return response

    service = DingTalkCliRuntimeService(runtime=SimpleNamespace(is_available=lambda scope: True, read_group_history=history))
    with pytest.raises(PersistentAgentError) as failure:
        await service.read_group_history(
            DingTalkCliScope("local", "employee-a"), conversation_id="cid-known", since="2026-10-30T14:00:00+00:00",
        )

    assert failure.value.code == "DINGTALK_CLI_GROUP_HISTORY_INVALID"
