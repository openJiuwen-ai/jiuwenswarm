# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Skill authorization on develop's permission engine and tenant session files."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.runner.callback.errors import AbortError
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.harness.security.core import PermissionEngine
from openjiuwen.harness.security.host import ToolPermissionHost
from openjiuwen.harness.security.skill_authorization import (
    GrantDecision, SkillGrantStore, SkillTrustLevel,
    get_skill_grant_store, load_skill_manifest,
    set_skill_grant_store,
)
from openjiuwen.harness.rails.skills.skill_authorization_rail import SkillAuthorizationRail

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    convert_interactions_to_ask_user_question,
)
from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization.file_store import FileSkillGrantStore
from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization.permission_bridge import (
    SkillAuthorizationPermissionRail, compose_active_permissions, gate_handled,
)


@pytest.fixture
def enterprise_edition(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")


@pytest.fixture
def baseline():
    return {"enabled": True, "package_builtin_rules": False,
            "skill_authorization": {"enabled": True}, "defaults": {"*": "ask"},
            "tools": {"skill_tool": "ask", "read_file": "ask", "write_file": "ask", "bash": "ask"},
            "file_guard": {"enabled": False}}


@pytest.fixture
def manifest(tmp_path):
    (tmp_path / "SKILL.md").write_text("Probe skill body", encoding="utf-8")
    (tmp_path / "skill_permissions.json").write_text(json.dumps({
        "risk": {"level": "medium"},
        "permissions": {"tools": {"write_file": "allow", "read_file": "allow", "bash": "ask"},
                        "file_guard": {"global": {"/tmp": {"read": "allow", "write": "allow"}}}},
    }), encoding="utf-8")
    return load_skill_manifest(tmp_path, trust=SkillTrustLevel.OTHER)


@pytest.fixture
def stores(tmp_path):
    sessions = tmp_path / "sessions"
    (sessions / "session").mkdir(parents=True)
    original = get_skill_grant_store()
    a, b = [FileSkillGrantStore(sessions_root=lambda: sessions, data_root=lambda: tmp_path) for _ in range(2)]
    set_skill_grant_store(a)
    yield a, b
    set_skill_grant_store(original)


def activate(store, manifest, *, session="session", scope="main"):
    store.create_pending_grant(session, scope, manifest, decision=GrantDecision.LOCAL,
                               approval_tool_call_id="load")
    return store.activate_pending(session, scope, manifest.skill_name, manifest, approval_tool_call_id="load")


@pytest.mark.parametrize("session", ["../session", "a/b", "default"])
def test_reject_unsafe_session_path(stores, session):
    with pytest.raises(ValueError):
        stores[0].session_directory(session)


def test_reject_symlink_session(stores, tmp_path):
    (tmp_path / "sessions" / "link").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        stores[0].session_directory("link")


def test_permission_snapshot_only_composes_in_owned_scope(stores, manifest, baseline):
    activate(stores[0], manifest)
    rail = SkillAuthorizationPermissionRail(config=baseline, host=ToolPermissionHost(get_permissions_snapshot=lambda: baseline))
    rail.set_skill_scope_resolver(lambda ctx: ("session", "main") if ctx.agent == "owner" else None)
    assert compose_active_permissions(baseline)["tools"]["write_file"] == "ask"
    with rail.skill_check_context(SimpleNamespace(agent="owner")):
        assert rail._invoke_permissions_hook(rail._host.get_permissions_snapshot)["tools"]["write_file"] == "allow"
    with rail.skill_check_context(SimpleNamespace(agent="child")):
        assert rail._invoke_permissions_hook(rail._host.get_permissions_snapshot)["tools"]["write_file"] == "ask"


@pytest.mark.parametrize("level", ["ask", "deny"])
def test_run_constraints_survive_skill_overlay(stores, manifest, baseline, level):
    from jiuwenswarm.runtime.run_permissions import RUN_PERMISSIONS

    activate(stores[0], manifest)
    rail = SkillAuthorizationPermissionRail(config=baseline, host=ToolPermissionHost(get_permissions_snapshot=lambda: baseline))
    rail.set_skill_scope_resolver(lambda ctx: ("session", "main"))
    token = RUN_PERMISSIONS.set({"write_file": level})
    try:
        with rail.skill_check_context(SimpleNamespace(agent="owner")):
            assert rail._invoke_permissions_hook(rail._host.get_permissions_snapshot)["tools"]["write_file"] == level
    finally:
        RUN_PERMISSIONS.reset(token)


def test_cancellation_retires_only_owned_session(stores, manifest, tmp_path, enterprise_edition):
    from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization.adapter import revoke_skill_authorization

    activate(stores[0], manifest)
    (tmp_path / "sessions" / "other-session").mkdir()
    activate(stores[0], manifest, session="other-session")
    revoke_skill_authorization("session", reason="cancel", sessions_root=tmp_path / "sessions")
    assert stores[1].get_active("session", "main") is None
    assert stores[1].get_active("other-session", "main") is not None


def test_disabled_reload_retires_grants_before_gate_is_removed(stores, manifest, tmp_path, enterprise_edition):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    activate(stores[0], manifest)
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._parent_session_id = "session"
    adapter._skill_authorization_rail = SimpleNamespace(sessions_root=tmp_path / "sessions")
    assert adapter._build_skill_authorization_rail({"permissions": {"enabled": True}}) is None
    stores[1].sync_feature_state("session", enabled=True)
    assert stores[1].get_active("session", "main") is None


@pytest.mark.parametrize("edition", ["personal", ""])
def test_personal_never_installs_enterprise_skill_authorization(monkeypatch, edition):
    from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization.adapter import (
        revoke_skill_authorization, clear_deleted_skill_authorization,
    )
    from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization.subagents import install_subagent_authorization
    from jiuwenswarm.server.runtime.agent_adapter.sysop_builder import _skill_state_paths, _protect_yuanrong_skill_state

    monkeypatch.setenv("JIUWENSWARM_EDITION", edition)
    monkeypatch.setenv("SKILL_AUTHORIZATION_ENABLED", "true")
    # An unexpected enterprise feature flag must not even look up a personal adapter/store.
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    assert adapter._build_skill_authorization_rail({"permissions": {
        "enabled": True, "skill_authorization": {"enabled": True},
    }}) is None
    install_subagent_authorization(object(), lambda: pytest.fail("personal gate lookup"))
    assert _skill_state_paths(None) == []
    mounts = [object()]
    assert _protect_yuanrong_skill_state(mounts, None) is mounts
    revoke_skill_authorization("invalid/session", reason="cancel")
    clear_deleted_skill_authorization("invalid/session", sessions_root=None)


def test_gate_marker_is_bound_to_exact_tool_call():
    ctx = SimpleNamespace(inputs=SimpleNamespace(tool_name="skill_tool", tool_call=SimpleNamespace(id="second")),
                          extra={"_skill_authorization_gate_handled": "first"})
    assert not gate_handled(ctx)
    ctx.extra["_skill_authorization_gate_handled"] = "second"
    assert gate_handled(ctx)


@pytest.mark.asyncio
async def test_medium_skill_asks_before_loading_and_projects_protocol(baseline, manifest, tmp_path):
    store = SkillGrantStore()
    rail = SkillAuthorizationRail(engine=PermissionEngine(baseline), config_provider=lambda: baseline,
                                  skill_resolver=lambda name: (tmp_path, str(tmp_path), None), grant_store=store)
    state = {}
    session = SimpleNamespace(get_session_id=lambda: "session", get_state=lambda key: state.get(key),
                              update_state=lambda values: state.update(values))
    call = ToolCall(id="load", type="function", name="skill_tool", arguments=json.dumps({"skill_name": manifest.skill_name}))
    ctx = SimpleNamespace(inputs=SimpleNamespace(tool_name="skill_tool", tool_call=call),
                          session=session, extra={})
    with pytest.raises(AbortError) as stopped:
        await rail.before_tool_call(ctx)
    request = stopped.value.cause.request
    card = request.payload_schema["x-skill-approval-card"]
    assert card["kind"] == "skill_approval"
    assert card["diff"]["widened"]
    assert store.get_active("session", "main") is None
    payload = convert_interactions_to_ask_user_question([
        {"id": "load", "value": {"tool_name": "skill_tool", "message": request.message,
                                  "ui_options": request.ui_options, "payload_schema": request.payload_schema,
                                  "interrupt_kind": request.interrupt_kind, "interrupt_id": "intr_skill"}},
    ])
    assert payload["skill_approval_card"] == card
    assert payload["source"] == "permission_interrupt"
    assert payload["interrupt_id"] == "intr_skill"
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm

    answer = JiuWenSwarm._build_interactive_input_from_answers(
        "load", [{"action": "approve_once"}], payload["source"], interrupt_id=payload["interrupt_id"],
    )
    assert answer.interrupt_answers == {"intr_skill": {"action": "approve_once"}}
    assert not answer.user_inputs


@pytest.mark.asyncio
async def test_prewarmed_session_registers_gate_and_complete_tool(stores, baseline, enterprise_edition):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    class Abilities:
        def __init__(self):
            self.cards = {}

        def get(self, name):
            return self.cards.get(name)

        def add_ability(self, card, tool):
            self.cards[card.name] = card

    instance = SimpleNamespace(ability_manager=Abilities(), _registered_rails=[], react_agent=object(),
                               create_subagent=lambda *args, **kwargs: None)

    async def register(rail):
        rail.init(instance)
        instance._registered_rails.append(rail)

    instance.register_rail = AsyncMock(side_effect=register)
    instance.is_registered_rail = instance._registered_rails.__contains__
    permission = SkillAuthorizationPermissionRail(config=baseline, host=ToolPermissionHost(get_permissions_snapshot=lambda: baseline))
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance, adapter._permission_rail, adapter._parent_session_id = instance, permission, "session"
    adapter._config_base_cache, adapter._skill_rail = {"permissions": baseline}, None
    adapter._skill_scan_dirs = lambda: []
    adapter._skill_manager = SimpleNamespace(list_skill_installations=lambda: [])
    await adapter._ensure_skill_authorization_registered()
    await adapter._ensure_skill_authorization_registered()
    assert instance.register_rail.await_count == 2
    assert instance.ability_manager.get("skill_complete") is not None
    assert "exposure" in instance.ability_manager.get("skill_complete").model_fields_set
    assert adapter._skill_authorization_rail.uses_permission_engine(permission.permission_engine)


@pytest.mark.asyncio
async def test_enterprise_prewarm_installs_live_permission_chain(monkeypatch, baseline):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
    monkeypatch.setattr("jiuwenswarm.enterprise.is_enterprise", lambda: True)
    ensure_skill = AsyncMock()
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = True
    adapter._config_base_cache = {}
    adapter._permission_state = SimpleNamespace(permission_update_in_progress=False)
    adapter._requested_model_name = lambda request: "test-model"
    adapter._has_valid_model_config = lambda name: False
    adapter._load_enterprise_config = AsyncMock(return_value=SimpleNamespace(
        policy=SimpleNamespace(models={"default": {}}), config={"permissions": baseline}))
    adapter._update_permission_rail = lambda config: None
    adapter._ensure_permission_rail_live_registered = AsyncMock()
    adapter._ensure_skill_authorization_registered = ensure_skill
    adapter._create_model = lambda config: None
    await adapter._ensure_enterprise_models(object())
    adapter._ensure_permission_rail_live_registered.assert_awaited_once()
    ensure_skill.assert_awaited_once_with()
