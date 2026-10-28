"""Session authorization filtering must use execution policy, not tool baselines."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock
from weakref import WeakValueDictionary

import pytest

from jiuwenswarm.agents.harness.common.a4p import preflight
from jiuwenswarm.agents.harness.common.a4p.execution_context import (
    AUTHORIZATION_EXECUTION_CONTEXTS,
    AuthorizationExecutionContext,
    AuthorizerRoute,
)
from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import build_permission_rail
from jiuwenswarm.agents.harness.common.tools import a4p_tools


@pytest.fixture
def authorization_session(monkeypatch, tmp_path):
    monkeypatch.setattr(preflight, "_SESSION_RAILS", WeakValueDictionary())
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers.load_user_permissions",
        lambda: {},
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers.load_session_permissions",
        lambda _sid: {},
    )
    monkeypatch.setattr(a4p_tools, "get_config", lambda: {"a4p": {"enabled": True}})
    AUTHORIZATION_EXECUTION_CONTEXTS.clear()
    AUTHORIZATION_EXECUTION_CONTEXTS.activate(
        AuthorizationExecutionContext(
            request_id="request-preflight",
            session_id="preflight-session",
            channel_id="web",
            agent_id="agent-preflight",
            metadata={},
            authorizer_route=AuthorizerRoute(
                session_id="preflight-session", app_id="default",
                agent_ref_mode="agent", agent_ref_id="agent-preflight",
            ),
        ),
    )
    runtime = AsyncMock()
    runtime.find_valid_intent_token.return_value = None

    async def authorize(**kwargs):
        actions = [
            {"name": action["name"], "params": action["params"]}
            for action in kwargs["actions"]
        ]
        return {
            "approved": True,
            "authorizedActions": actions,
            "intentToken": {"tokenId": "new-token", "intent": {"actions": actions}},
        }

    runtime.request_intent_authorization.side_effect = authorize
    monkeypatch.setattr(a4p_tools, "get_a4p_runtime", lambda: runtime)
    yield runtime
    AUTHORIZATION_EXECUTION_CONTEXTS.clear()


def _rail(config):
    return build_permission_rail(
        {"permissions": {
            "enabled": True,
            "schema": "tiered_policy",
            "package_builtin_rules": False,
            "defaults": {"*": "allow"},
            "file_guard": {"enabled": False},
            **config,
        }},
        session_id="preflight-session",
    )


def test_public_permission_snapshot_preserves_invocation_overlay(authorization_session):
    from jiuwenswarm.runtime.run_permissions import RUN_PERMISSIONS

    rail = _rail({"tools": {"bash": "ask"}})
    assert rail is not None
    assert rail.get_permission_snapshot(session_id="preflight-session")["tools"]["bash"] == "ask"
    token = RUN_PERMISSIONS.set({"bash": "deny"})
    try:
        assert rail.get_permission_snapshot(session_id="preflight-session")["tools"]["bash"] == "deny"
    finally:
        RUN_PERMISSIONS.reset(token)
    assert rail.get_permission_snapshot(session_id="preflight-session")["tools"]["bash"] == "ask"


def _action(name="bash", **params):
    return {"name": name, "params": params}


async def _request(actions, cron_job_id=None):
    return await a4p_tools.request_a4p_intent_authorization(
        actions, cron_job_id=cron_job_id, _session_id="preflight-session",
    )


@pytest.mark.asyncio
async def test_mixed_commands_only_request_policy_ask(authorization_session):
    rail = _rail({
        "tools": {"bash": "allow"},
        "rules": [{"id": "remove-ask", "tools": ["bash"], "pattern": "rm *", "action": "ask"}],
    })
    assert rail is not None
    allowed = _action(command="pwd")
    needs_approval = _action(command="rm /tmp/a4p-test.txt")

    result = await _request([allowed, needs_approval])

    assert result["permittedActions"] == [allowed, needs_approval]
    assert result["unapprovedActions"] == []
    assert set(result).isdisjoint({
        "allowedActions", "coveredActions", "authorizedActions", "unselectedActions",
        "unauthorizedActions", "uncheckedActions",
    })
    assert result["authorizationRequired"] is True
    assert authorization_session.request_intent_authorization.await_args.kwargs["actions"] == [
        {**needs_approval, "allowExtraParams": True},
    ]


@pytest.mark.asyncio
async def test_path_guard_overrides_allow_baseline(authorization_session, tmp_path):
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    rail = _rail({
        "tools": {"write_file": "allow"},
        "file_guard": {
            "enabled": True,
            "defaults": {"read": "ask", "write": "ask", "exec": "ask"},
            "paths": [{"path": str(allowed_root), "read": "allow", "write": "allow"}],
        },
    })
    assert rail is not None
    allowed = _action("write_file", file_path=str(allowed_root / "a.txt"))
    needs_approval = _action("write_file", file_path=str(tmp_path / "outside.txt"))

    result = await _request([allowed, needs_approval])

    assert result["permittedActions"] == [allowed, needs_approval]
    assert result["unapprovedActions"] == []


@pytest.mark.asyncio
async def test_all_allowed_returns_success_without_card_or_token(authorization_session):
    rail = _rail({})
    assert rail is not None
    action = _action(command="pwd")

    result = await _request([action])

    assert result["ok"] is True
    assert result["authorizationRequired"] is False
    assert result["approved"] is False
    assert result["token"] is None
    assert result["permittedActions"] == [action]
    assert result["unapprovedActions"] == []
    authorization_session.request_intent_authorization.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_token_is_verified_before_filtering(authorization_session):
    rail = _rail({"tools": {"bash": "ask"}})
    assert rail is not None
    authorization_session.find_valid_intent_token.return_value = {"tokenId": "existing"}
    action = _action(command="pwd")

    result = await _request([action])

    assert result["ok"] is True
    assert result["authorizationRequired"] is False
    assert result["permittedActions"] == [action]
    assert result["unapprovedActions"] == []
    authorization_session.find_valid_intent_token.assert_awaited_once()
    authorization_session.request_intent_authorization.assert_not_awaited()


@pytest.mark.asyncio
async def test_filtered_actions_remain_distinct_from_partial_approval(authorization_session):
    rail = _rail({"tools": {"acp_chat": "ask"}})
    assert rail is not None
    allowed = _action(command="pwd")
    selected = _action("acp_chat", agent="selected-agent")
    unselected = _action("acp_chat", agent="unselected-agent")
    authorization_session.request_intent_authorization.side_effect = None
    authorization_session.request_intent_authorization.return_value = {
        "approved": True,
        "partial": True,
        "authorizedActions": [selected],
        "unselectedActions": [unselected],
        "intentToken": {"tokenId": "partial-token", "intent": {"actions": [selected]}},
    }

    result = await _request([allowed, selected, unselected])

    assert result["ok"] is True
    assert result["partial"] is True
    assert result["permittedActions"] == [allowed, selected]
    assert result["unapprovedActions"] == [unselected]


@pytest.mark.asyncio
async def test_user_rejection_does_not_authorize_remaining_actions(authorization_session):
    rail = _rail({"tools": {"acp_chat": "ask"}})
    assert rail is not None
    allowed = _action(command="pwd")
    needs_approval = _action("acp_chat", agent="external-agent")
    authorization_session.request_intent_authorization.side_effect = None
    authorization_session.request_intent_authorization.return_value = {
        "approved": False, "rejectReason": "User rejected",
    }

    result = await _request([allowed, needs_approval])

    assert result["ok"] is False
    assert result["permittedActions"] == [allowed]
    assert result["unapprovedActions"] == [needs_approval]
    assert result["token"] is None
    assert AUTHORIZATION_EXECUTION_CONTEXTS.get_attempt("preflight-session").status == "rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize("decision,expected", [("approve", "allow"), ("reject", "deny")])
async def test_manual_scene_decision_matches_execution(authorization_session, decision, expected):
    rail = _rail({"tools": {"bash": "deny" if decision == "approve" else "allow"}})
    assert rail is not None
    rail._host.permission_scene_hook = AsyncMock(return_value=(decision,))

    assert await preflight.evaluate_action("preflight-session", _action(command="pwd")) == expected


@pytest.mark.asyncio
async def test_policy_deny_blocks_whole_request_even_with_token(authorization_session):
    rail = _rail({"tools": {"acp_chat": "deny", "bash": "ask"}})
    assert rail is not None
    authorization_session.find_valid_intent_token.return_value = {"tokenId": "existing"}
    denied = _action("acp_chat", agent="external-agent")

    result = await _request([denied, _action(command="pwd")])

    assert result["ok"] is False
    assert result["code"] == "A4P_ACTION_POLICY_DENIED"
    assert result["deniedActions"] == [denied]
    assert result["permittedActions"] == [_action(command="pwd")]
    assert result["unapprovedActions"] == []
    authorization_session.request_intent_authorization.assert_not_awaited()


@pytest.mark.asyncio
async def test_policy_denial_preserves_unrequested_actions(authorization_session):
    rail = _rail({"tools": {"acp_chat": "deny", "bash": "ask"}})
    assert rail is not None
    denied = _action("acp_chat", agent="external-agent")
    unapproved = _action(command="pwd")

    result = await _request([denied, unapproved])

    assert result["permittedActions"] == []
    assert result["unapprovedActions"] == [unapproved]
    assert result["deniedActions"] == [denied]
    authorization_session.request_intent_authorization.assert_not_awaited()


@pytest.mark.asyncio
async def test_authorization_error_preserves_unapproved_actions(authorization_session):
    rail = _rail({"tools": {"acp_chat": "ask"}})
    assert rail is not None
    allowed = _action(command="pwd")
    unapproved = _action("acp_chat", agent="external-agent")
    authorization_session.request_intent_authorization.side_effect = RuntimeError("timeout")

    result = await _request([allowed, unapproved])

    assert result["ok"] is False
    assert result["code"] == "A4P_AUTHORIZATION_FAILED"
    assert result["permittedActions"] == [allowed]
    assert result["unapprovedActions"] == [unapproved]
    assert result["deniedActions"] == []


@pytest.mark.asyncio
async def test_pattern_is_never_prechecked_as_literal(authorization_session, monkeypatch):
    rail = _rail({})
    assert rail is not None
    check = AsyncMock(side_effect=AssertionError("Patterns must not reach the permission engine"))
    monkeypatch.setattr(rail._engine, "check_permission", check)
    action = _action("write_file", file_path="/reports/*.md")

    result = await _request([action])

    assert result["permittedActions"] == [action]
    assert result["unapprovedActions"] == []
    check.assert_not_awaited()
    authorization_session.find_valid_intent_token.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_rail", "engine", "snapshot", "verification"])
async def test_preflight_failure_keeps_action_in_request(authorization_session, monkeypatch, failure):
    rail = _rail({"tools": {"bash": "ask"}})
    assert rail is not None
    if failure == "missing_rail":
        preflight.unregister_preflight_rail("preflight-session")
    elif failure == "engine":
        monkeypatch.setattr(rail._engine, "check_permission", AsyncMock(side_effect=RuntimeError("failed")))
    elif failure == "snapshot":
        monkeypatch.setattr(rail, "get_permission_snapshot", Mock(side_effect=ValueError("snapshot unavailable")))
    else:
        authorization_session.find_valid_intent_token.side_effect = RuntimeError("verification failed")
    action = _action(command="pwd")

    result = await _request([action])

    assert result["authorizationRequired"] is True
    assert result["permittedActions"] == [action]


@pytest.mark.asyncio
async def test_cron_keeps_complete_scope_despite_session_allow(authorization_session, monkeypatch):
    rail = _rail({})
    assert rail is not None
    monkeypatch.setattr(
        a4p_tools, "_resolve_cron_authorization_target",
        AsyncMock(return_value={"cronJobId": "job-1", "enabled": False}),
    )
    action = _action(command="pwd")

    result = await _request([action], cron_job_id="job-1")

    assert result["permittedActions"] == [action]
    assert result["unapprovedActions"] == []
    authorization_session.find_valid_intent_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_execution_policy_change_is_not_bypassed_by_preflight(authorization_session):
    rail = _rail({})
    assert rail is not None
    action = _action(command="pwd")
    assert (await _request([action]))["authorizationRequired"] is False

    rail.replace_installed_global_permissions({
        "enabled": True, "schema": "tiered_policy", "package_builtin_rules": False,
        "tools": {"bash": "deny"}, "file_guard": {"enabled": False},
    })

    assert await preflight.evaluate_action("preflight-session", action) == "deny"
    result = await rail._engine.check_permission("bash", action["params"])
    assert result.permission.value == "deny"


def test_replacing_rail_with_disabled_permissions_drops_old_preflight(authorization_session):
    rail = _rail({})
    assert rail is not None
    assert build_permission_rail(
        {"permissions": {"enabled": False}}, session_id="preflight-session",
    ) is None
    assert "preflight-session" not in preflight._SESSION_RAILS
