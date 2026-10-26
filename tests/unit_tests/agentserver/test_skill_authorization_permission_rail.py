# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Leftover bash HITL must not treat the next chat string as a confirm payload."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from openjiuwen.core.runner.callback.errors import AbortError
from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail

from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization_permission_rail import (
    SkillAuthorizationPermissionRail,
    is_unparseable_permission_resume_text,
)

_PLAIN_CHAT = "调用bash工具，不用大模型，直接执行命令'ls /tmpppp'"
_CONFIRM_DICT = {"approved": True, "auto_confirm": True, "persist_allow": False}
_CONFIRM_JSON = (
    '{"approved": true, "auto_confirm": true, "persist_allow": false}'
)


@pytest.mark.parametrize(
    ("user_input", "expected"),
    [
        (_PLAIN_CHAT, True),
        ("hello", True),
        ("", True),
        (_CONFIRM_JSON, False),
        (_CONFIRM_DICT, False),
        (None, False),
        (SimpleNamespace(approved=True), False),
    ],
)
def test_unparseable_permission_resume_text(user_input: Any, expected: bool) -> None:
    assert is_unparseable_permission_resume_text(user_input) is expected


@pytest.mark.asyncio
async def test_plain_chat_resume_falls_back_to_first_check() -> None:
    rail = object.__new__(SkillAuthorizationPermissionRail)
    captured: dict[str, Any] = {}

    async def _parent_resolve(self, ctx, tool_call, user_input, auto_confirm_config=None):
        captured["user_input"] = user_input
        captured["auto_confirm_config"] = auto_confirm_config
        return "first_check"

    with patch.object(PermissionInterruptRail, "resolve_interrupt", _parent_resolve):
        result = await rail.resolve_interrupt(
            ctx=SimpleNamespace(),
            tool_call=SimpleNamespace(name="bash"),
            user_input=_PLAIN_CHAT,
            auto_confirm_config={"bash:ls /tmpppp": True},
        )

    assert result == "first_check"
    assert captured["user_input"] is None
    assert captured["auto_confirm_config"] == {"bash:ls /tmpppp": True}


@pytest.mark.asyncio
async def test_structured_confirm_payload_is_not_rewritten() -> None:
    rail = object.__new__(SkillAuthorizationPermissionRail)
    captured: dict[str, Any] = {}

    async def _parent_resolve(self, ctx, tool_call, user_input, auto_confirm_config=None):
        captured["user_input"] = user_input
        return "approved"

    with patch.object(PermissionInterruptRail, "resolve_interrupt", _parent_resolve):
        result = await rail.resolve_interrupt(
            ctx=SimpleNamespace(),
            tool_call=SimpleNamespace(name="bash"),
            user_input=_CONFIRM_DICT,
        )

    assert result == "approved"
    assert captured["user_input"] is _CONFIRM_DICT


@pytest.mark.asyncio
async def test_json_confirm_string_is_not_rewritten() -> None:
    rail = object.__new__(SkillAuthorizationPermissionRail)
    captured: dict[str, Any] = {}

    async def _parent_resolve(self, ctx, tool_call, user_input, auto_confirm_config=None):
        captured["user_input"] = user_input
        return "approved"

    with patch.object(PermissionInterruptRail, "resolve_interrupt", _parent_resolve):
        result = await rail.resolve_interrupt(
            ctx=SimpleNamespace(),
            tool_call=SimpleNamespace(name="bash"),
            user_input=_CONFIRM_JSON,
        )

    assert result == "approved"
    assert captured["user_input"] == _CONFIRM_JSON


@pytest.mark.asyncio
async def test_interrupt_emits_check_and_reraises() -> None:
    rail = object.__new__(SkillAuthorizationPermissionRail)
    ctx = SimpleNamespace(inputs=SimpleNamespace(tool_name="list_files", tool_call=None))

    async def _parent_before(self, ctx):
        raise AbortError("Tool execution interrupted: list_files")

    with (
        patch.object(PermissionInterruptRail, "before_tool_call", _parent_before),
        patch(
            "jiuwenswarm.agents.harness.common.rails.permissions."
            "skill_authorization_permission_rail.emit_audit_evt"
        ) as emit,
    ):
        with pytest.raises(AbortError):
            await rail.before_tool_call(ctx)

    emit.assert_called_once_with(
        SUBMDL="agent",
        PROC="skill_authorize",
        MSG="list_files",
        EVT="skill_authorize_check",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_input", "expected_choice"),
    [
        ({"approved": True, "auto_confirm": False, "persist_allow": False}, "本次允许"),
        ({"approved": True, "auto_confirm": True, "persist_allow": False}, "会话内记住"),
        ({"approved": True, "auto_confirm": True, "persist_allow": True}, "始终允许"),
        ({"approved": False, "auto_confirm": False, "persist_allow": False}, "不允许"),
    ],
)
async def test_confirm_payload_emits_decision(user_input: dict, expected_choice: str) -> None:
    rail = object.__new__(SkillAuthorizationPermissionRail)

    async def _parent_resolve(self, ctx, tool_call, user_input, auto_confirm_config=None):
        return "decided"

    with (
        patch.object(PermissionInterruptRail, "resolve_interrupt", _parent_resolve),
        patch(
            "jiuwenswarm.agents.harness.common.rails.permissions."
            "skill_authorization_permission_rail.emit_audit_evt"
        ) as emit,
    ):
        result = await rail.resolve_interrupt(
            ctx=SimpleNamespace(),
            tool_call=SimpleNamespace(name="list_files"),
            user_input=user_input,
        )

    assert result == "decided"
    emit.assert_called_once_with(
        SUBMDL="agent",
        PROC="skill_authorize",
        MSG=f"list_files: {expected_choice}",
        EVT="skill_authorize_decision",
        choice=expected_choice,
    )


@pytest.mark.asyncio
async def test_plain_chat_resume_does_not_emit_decision() -> None:
    rail = object.__new__(SkillAuthorizationPermissionRail)

    async def _parent_resolve(self, ctx, tool_call, user_input, auto_confirm_config=None):
        return "first_check"

    with (
        patch.object(PermissionInterruptRail, "resolve_interrupt", _parent_resolve),
        patch(
            "jiuwenswarm.agents.harness.common.rails.permissions."
            "skill_authorization_permission_rail.emit_audit_evt"
        ) as emit,
    ):
        await rail.resolve_interrupt(
            ctx=SimpleNamespace(),
            tool_call=SimpleNamespace(name="bash"),
            user_input=_PLAIN_CHAT,
        )

    emit.assert_not_called()


@pytest.fixture
def active_skill_permissions(tmp_path, monkeypatch):
    from jiuwenswarm.agents.harness.common.rails.skill_authorization_rail import SkillAuthorizationRail
    from openjiuwen.harness.security import skill_authorization

    skill_dir = tmp_path / "permission-probe"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: permission-probe\ndescription: Permission probe\n---\nGet current user.\n"
    )
    (skill_dir / "skill_permissions.json").write_text(json.dumps({
        "risk": {"level": "low"},
        "permissions": {"enabled": True, "tools": {"get_current_user": "allow"}},
    }))
    manifest = skill_authorization.load_skill_manifest(
        skill_dir, trust=skill_authorization.SkillTrustLevel.OTHER,
    )
    assert manifest.overlay["tools"] == {"get_current_user": "allow"}
    store = skill_authorization.SkillGrantStore()
    monkeypatch.setattr(skill_authorization, "get_skill_grant_store", lambda: store)
    monkeypatch.setattr(skill_authorization, "get_skill_authorization_context", lambda: None)
    monkeypatch.setattr(
        "openjiuwen.harness.rails.skills.skill_authorization_rail.get_skill_authorization_context",
        lambda: None,
    )
    store.create_pending_grant(
        "session-1", "main", manifest, decision=skill_authorization.GrantDecision.LOCAL,
    )
    assert store.activate_pending("session-1", "main", manifest.skill_name, manifest)
    baseline = {
        "enabled": True, "schema": "tiered_policy", "defaults": {"*": "ask"},
        "tools": {"get_current_user": "ask", "write_file": "ask"},
        "skill_authorization": {"enabled": True},
    }
    rail = SkillAuthorizationPermissionRail(config=baseline)
    gate = SkillAuthorizationRail(engine=rail._engine, grant_store=store)
    rail.set_skill_scope_resolver(gate.resolve_scope)
    ctx = SimpleNamespace(session=SimpleNamespace(session_id="session-1"))
    return rail, ctx, store, manifest, baseline


@pytest.mark.asyncio
async def test_active_skill_overlay_survives_missing_task_context(active_skill_permissions):
    rail, ctx, _, _, baseline = active_skill_permissions
    original = copy.deepcopy(baseline)
    rail._refresh_permissions_for_tool_call(ctx)
    declared = await rail._engine.check_permission("get_current_user", {})
    undeclared = await rail._engine.check_permission("write_file", {})
    assert declared.permission.value == "allow"
    assert undeclared.permission.value == "ask"
    assert baseline == original
    assert rail._static_config == original


def test_permission_composition_reads_one_snapshot(active_skill_permissions):
    rail, ctx, store, _, _ = active_skill_permissions
    with patch.object(store, "get_active", wraps=store.get_active) as read:
        rail._refresh_permissions_for_tool_call(ctx)
    read.assert_called_once_with("session-1", "main")


@pytest.mark.parametrize(
    "enabled, disable_after, expected_syncs",
    [(True, False, 1), (False, False, 2), (True, True, 2)],
)
@pytest.mark.asyncio
async def test_gate_syncs_policy_without_repeating_enabled_transaction(
    active_skill_permissions, monkeypatch, enabled, disable_after, expected_syncs,
):
    from unittest.mock import Mock

    from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization.file_store import (
        clear_session_file_context,
    )
    from jiuwenswarm.agents.harness.common.rails.skill_authorization_rail import (
        SkillAuthorizationRail,
    )

    _, ctx, store, _, _ = active_skill_permissions
    store.sync_feature_state = Mock()
    gate = SkillAuthorizationRail(grant_store=store)
    gate.set_sessions_root("/trusted/sessions")
    monkeypatch.setattr(gate, "_feature_enabled", lambda: enabled)
    try:
        await gate.before_tool_call(ctx)
        if disable_after:
            enabled = False
        await gate.after_tool_call(ctx)
        assert store.sync_feature_state.call_count == expected_syncs
        store.sync_feature_state.assert_called_with("session-1", enabled=enabled)
    finally:
        clear_session_file_context()


def test_gate_storage_failure_aborts_instead_of_skipping_authorization(
    active_skill_permissions,
):
    from unittest.mock import Mock

    from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization.file_store import (
        clear_session_file_context,
    )
    from jiuwenswarm.agents.harness.common.rails.skill_authorization_rail import (
        SkillAuthorizationRail,
    )

    _, ctx, store, _, _ = active_skill_permissions
    store.sync_feature_state = Mock(side_effect=TimeoutError("storage unavailable"))
    gate = SkillAuthorizationRail(grant_store=store)
    gate.set_sessions_root("/trusted/sessions")
    try:
        with pytest.raises(AbortError, match="storage unavailable"):
            gate._bind_session_files(ctx)
    finally:
        clear_session_file_context()


@pytest.mark.asyncio
async def test_missing_context_cannot_use_another_session_grant(active_skill_permissions):
    rail, ctx, _, _, _ = active_skill_permissions
    rail._refresh_permissions_for_tool_call(ctx)
    ctx.session.session_id = "session-2"
    rail._refresh_permissions_for_tool_call(ctx)
    assert (await rail._engine.check_permission("get_current_user", {})).permission.value == "ask"


@pytest.mark.asyncio
async def test_missing_context_cannot_use_main_grant_for_child(active_skill_permissions):
    from jiuwenswarm.agents.harness.common.rails.skill_authorization_rail import SkillAuthorizationRail

    rail, ctx, store, _, _ = active_skill_permissions
    rail._refresh_permissions_for_tool_call(ctx)
    child_gate = SkillAuthorizationRail(grant_store=store, agent_scope_id="child-1")
    rail.set_skill_scope_resolver(child_gate.resolve_scope)
    rail._refresh_permissions_for_tool_call(ctx)
    assert (await rail._engine.check_permission("get_current_user", {})).permission.value == "ask"


@pytest.mark.asyncio
async def test_missing_context_preserves_deny_and_revocation(active_skill_permissions):
    rail, ctx, store, manifest, baseline = active_skill_permissions
    denied = copy.deepcopy(baseline)
    denied["tools"]["get_current_user"] = "deny"
    rail.update_config(denied)
    rail._refresh_permissions_for_tool_call(ctx)
    assert (await rail._engine.check_permission("get_current_user", {})).permission.value == "deny"

    rail.update_config(baseline)
    rail._refresh_permissions_for_tool_call(ctx)
    assert (await rail._engine.check_permission("get_current_user", {})).permission.value == "allow"
    store.invalidate_scope("session-1", "main", reason="skill_complete")
    store.revoke_grants("session-1", "main", skill_name=manifest.skill_name, reason="skill_complete")
    rail._refresh_permissions_for_tool_call(ctx)
    assert (await rail._engine.check_permission("get_current_user", {})).permission.value == "ask"


@pytest.mark.asyncio
async def test_missing_session_or_unbound_resolver_keeps_baseline(active_skill_permissions):
    rail, ctx, _, _, _ = active_skill_permissions
    ctx.session = None
    rail._refresh_permissions_for_tool_call(ctx)
    assert (await rail._engine.check_permission("get_current_user", {})).permission.value == "ask"
    del rail._skill_scope_resolver
    ctx.session = SimpleNamespace(session_id="session-1")
    rail._refresh_permissions_for_tool_call(ctx)
    assert (await rail._engine.check_permission("get_current_user", {})).permission.value == "ask"


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id, expected", [("session-1", None), ("session-2", ("approve",))])
@pytest.mark.parametrize("overlay_permission", ["ask", "deny"])
async def test_owner_allow_cannot_skip_active_skill_overlay(
    active_skill_permissions, monkeypatch, tmp_path, session_id, expected, overlay_permission,
):
    from openjiuwen.harness.security.host import PermissionSceneHookInput
    from openjiuwen.harness.security import skill_authorization
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import build_permission_rail
    from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
        PermissionContext, TOOL_PERMISSION_CONTEXT,
    )

    original_rail, ctx, store, _, baseline = active_skill_permissions
    config = copy.deepcopy(baseline)
    config["tools"]["get_current_user"] = "allow"
    config["owner_scopes"] = {"web": {"test-owner": {"tools": {"get_current_user": "allow"}}}}
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.config_loader.get_effective_permissions_config",
        lambda **kwargs: config,
    )
    rail = build_permission_rail({})
    rail.set_skill_scope_resolver(original_rail._skill_scope_resolver)
    skill_dir = tmp_path / "permission-probe"
    (skill_dir / "skill_permissions.json").write_text(json.dumps({
        "risk": {"level": "low"},
        "permissions": {"tools": {"get_current_user": overlay_permission}},
    }))
    manifest = skill_authorization.load_skill_manifest(
        skill_dir, trust=skill_authorization.SkillTrustLevel.OTHER,
    )
    store.create_pending_grant(
        "session-1", "main", manifest, decision=skill_authorization.GrantDecision.LOCAL,
    )
    assert store.activate_pending("session-1", "main", manifest.skill_name, manifest)
    ctx.session.session_id = session_id
    inp = PermissionSceneHookInput(
        ctx=ctx, tool_call=None, user_input=None,
        normalized_tool_name="get_current_user", tool_args={}, engine=rail._engine,
    )
    token = TOOL_PERMISSION_CONTEXT.set(PermissionContext(channel_id="web", principal_user_id="test-owner"))
    try:
        assert await rail._host.permission_scene_hook(inp) == expected
        rail._refresh_permissions_for_tool_call(ctx)
        permission = (await rail._engine.check_permission("get_current_user", {})).permission.value
        assert permission == (overlay_permission if session_id == "session-1" else "allow")
    finally:
        TOOL_PERMISSION_CONTEXT.reset(token)


@pytest.mark.asyncio
async def test_disabled_policy_does_not_apply_persisted_grant(active_skill_permissions, monkeypatch):
    rail, ctx, _, _, baseline = active_skill_permissions
    disabled = copy.deepcopy(baseline)
    disabled['skill_authorization'] = {'enabled': False}
    monkeypatch.setattr(rail, '_get_permissions_snapshot', lambda context: disabled)
    # Parent refresh must also consume the disabled baseline snapshot.
    monkeypatch.setattr(type(rail).__mro__[1], '_get_permissions_snapshot', lambda self, context: disabled)
    rail._refresh_permissions_for_tool_call(ctx)
    assert (await rail._engine.check_permission('get_current_user', {})).permission.value == 'ask'
