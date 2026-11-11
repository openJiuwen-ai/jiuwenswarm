# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Regressions for dev-stable PR 7950, 8078 and 8265 on the current SDK factory."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.harness import create_deep_agent
from openjiuwen.core.runner.callback.errors import AbortError
from openjiuwen.harness.security.core import PermissionEngine
from openjiuwen.harness.security.skill_authorization import (
    GrantDecision, SubagentApprovalKind, SkillTrustLevel, get_skill_grant_store,
    set_skill_grant_store,
)
from openjiuwen.harness.security.skill_authorization.subagent_approval_registry import SubagentApprovalRegistry

from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization.file_store import (
    FileSkillGrantStore, bind_session_file_context, clear_session_file_context,
)
from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization.permission_bridge import (
    SkillAuthorizationPermissionRail, gate_handled,
)
from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization.subagents import (
    install_subagent_authorization, resolve_subagent_approval,
)
from jiuwenswarm.agents.harness.common.rails.skill_authorization_rail import SkillAuthorizationRail
from jiuwenswarm.agents.harness.common.rails.skill_active_state import SkillActiveStateRail
from jiuwenswarm.agents.harness.common.rails.subagent_skill_authorization_rail import SubagentSkillAuthorizationRail
from jiuwenswarm.agents.harness.common.rails.subagent_permission_rail import SubagentPermissionRail


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    root = tmp_path / "sessions"
    (root / "parent").mkdir(parents=True)
    skill = tmp_path / "probe"
    skill.mkdir()
    (skill / "SKILL.md").write_text("Probe body", encoding="utf-8")
    (skill / "skill_permissions.json").write_text(json.dumps({
        "risk": {"level": "medium"},
        "permissions": {"tools": {"write_file": "allow", "read_file": "allow"}},
    }), encoding="utf-8")
    policy = {"enabled": True, "package_builtin_rules": False, "defaults": {"*": "ask"},
              "skill_authorization": {"enabled": True}, "file_guard": {"enabled": False}}
    original = get_skill_grant_store()
    store = FileSkillGrantStore(sessions_root=lambda: root, data_root=lambda: tmp_path)
    set_skill_grant_store(store)
    bind_session_file_context("parent", root)
    registry = SubagentApprovalRegistry.get_instance()
    registry.clear_all()
    yield SimpleNamespace(root=root, skill=skill, policy=policy, store=store, registry=registry)
    registry.clear_all()
    clear_session_file_context()
    set_skill_grant_store(original)


def context(name, args=None, call_id="call"):
    call = ToolCall(id=call_id, name=name, type="function", arguments=json.dumps(args or {}))
    return SimpleNamespace(
        inputs=SimpleNamespace(tool_name=name, tool_call=call, tool_args=args or {}, tool_result=None, tool_msg=None),
        session=SimpleNamespace(get_session_id=lambda: "parent_sub_child"), extra={}, agent=object(),
    )


@pytest.mark.parametrize("edition,expected", [("enterprise", True), ("personal", False), ("", False)])
def test_delegated_control_classification_is_enterprise_only(monkeypatch, edition, expected):
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        is_interrupt_resume_payload,
    )

    monkeypatch.setenv("JIUWENSWARM_EDITION", edition)
    params = {"request_id": "subagent_skill_load_probe", "source": "subagent_skill_load",
              "answers": [{"action": "approve_once"}]}
    assert is_interrupt_resume_payload(params) is expected
    assert not is_interrupt_resume_payload({**params, "request_id": "unrelated"})
    assert not is_interrupt_resume_payload({**params, "source": []})


def child_gate(s, **kwargs):
    gate = SubagentSkillAuthorizationRail(
        session_id="parent", agent_scope_id="child", engine=PermissionEngine(s.policy),
        grant_store=s.store, config_provider=lambda: s.policy,
        skill_resolver=lambda name: (s.skill, str(s.skill), None),
        trust_resolver=lambda path: SkillTrustLevel.OTHER, **kwargs,
    )
    gate.set_sessions_root(s.root)
    return gate


@pytest.mark.asyncio
async def test_public_factory_excludes_main_gate_and_binds_child_to_parent_files(setup, tmp_path):
    s = setup
    permission = SkillAuthorizationPermissionRail(config=s.policy)
    gate = SkillAuthorizationRail(engine=permission._engine, config_provider=lambda: s.policy,
                                  grant_store=s.store)
    gate.bind_session("parent", s.root)
    parent = create_deep_agent(
        model=Model(model_client_config=ModelClientConfig(client_provider="OpenAI", api_key="test",
                                                          api_base="http://127.0.0.1:1"),
                    model_config=ModelRequestConfig(model="test")),
        rails=[gate, permission], add_general_purpose_agent=True,
        workspace=str(tmp_path / "workspace"), auto_create_workspace=False,
    )
    install_subagent_authorization(parent, lambda: gate)
    install_subagent_authorization(parent, lambda: gate)
    child = parent.create_subagent("general-purpose", "parent_sub_child")
    rails = child.configured_rails()
    assert gate not in rails
    assert permission in rails  # Baseline still checks tools outside a Skill window.
    gates = [rail for rail in rails if isinstance(rail, SubagentSkillAuthorizationRail)]
    assert len(gates) == 1
    ctx = context("probe")
    clear_session_file_context()  # Simulate callbacks running outside the request task.
    await gates[0].before_invoke(ctx)
    await gates[0].before_tool_call(ctx)
    assert gates[0].resolve_scope(ctx) == ("parent", "parent_sub_child")
    assert (s.root / "parent/.skill_authorization/state.json").is_file()
    assert not (s.root / "parent_sub_child").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approve_once", "continue_without_overlay"])
async def test_child_skill_approval_does_not_grant_parent_or_sibling(setup, action):
    s = setup
    emitted = asyncio.Queue()
    gate = child_gate(s, approval_sender=emitted.put)
    ctx = context("skill_tool", {"skill_name": "probe"})
    waiting = asyncio.create_task(gate.before_tool_call(ctx))
    request = await asyncio.wait_for(emitted.get(), 2)
    assert "approve_session" not in request.payload["card"]["actions"]
    assert not resolve_subagent_approval(session_id="other", request_id=request.approval_id,
                                        params={"answers": [{"action": "approve_once"}]})
    assert resolve_subagent_approval(session_id="parent", request_id=request.approval_id,
                                    params={"source": "subagent_skill_load", "answers": [{"action": action}]})
    await waiting
    assert gate_handled(ctx)
    ctx.inputs.tool_result = SimpleNamespace(success=True)
    ctx.inputs.tool_msg = SimpleNamespace(content="Probe body", metadata={"skill_name": "probe"})
    await gate.after_tool_call(ctx)
    assert (s.store.get_active("parent", "child") is not None) == (action == "approve_once")
    assert s.store.get_active("parent", "main") is None
    assert s.store.get_active("parent", "sibling") is None
    assert s.store.get_active_skill_execution("parent", "child") == "probe"
    assert not resolve_subagent_approval(session_id="parent", request_id=request.approval_id, params={})


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["approve", "reject", "timeout", "cancel", "disable", "delete"])
async def test_child_undeclared_tool_requires_current_one_shot_approval(setup, outcome):
    s = setup
    s.store.enter_skill_execution("parent", "child", "probe")
    emitted = asyncio.Queue()
    expired = AsyncMock()
    rail = SubagentPermissionRail(
        session_id="parent", agent_scope_id="child", grant_store=s.store,
        config_provider=lambda: s.policy, approval_sender=emitted.put,
        approval_expiry_sender=expired, approval_timeout=0.05 if outcome == "timeout" else 5,
    )
    rail.set_sessions_root(s.root)
    ctx = context("bash", {"command": "printf probe"})
    waiting = asyncio.create_task(rail.before_tool_call(ctx))
    request = await asyncio.wait_for(emitted.get(), 2)
    if outcome == "cancel":
        s.registry.cancel_scope("parent", "child")
    elif outcome != "timeout":
        if outcome == "disable":
            s.policy["skill_authorization"]["enabled"] = False
            s.store.sync_feature_state("parent", enabled=False)
        if outcome == "delete":
            s.store.clear_session("parent")
        assert resolve_subagent_approval(session_id="parent", request_id=request.approval_id, params={
            "source": "subagent_tool_permission",
            "answers": [{"selected_options": ["拒绝" if outcome == "reject" else "本次允许"]}],
        })
    if outcome == "delete":
        with pytest.raises(AbortError, match="version unavailable"):
            await waiting
        assert s.registry.pending_count == 0
        return
    await waiting
    assert bool(ctx.extra.get("_skip_tool")) == (outcome != "approve")
    assert gate_handled(ctx)
    assert s.registry.pending_count == 0
    assert not resolve_subagent_approval(session_id="parent", request_id=request.approval_id, params={})


@pytest.mark.asyncio
async def test_child_approved_overlay_skips_repeated_native_approval(setup):
    s = setup
    gate = child_gate(s)
    manifest = gate._resolve_manifest_for_call("probe")
    s.store.create_pending_grant("parent", "child", manifest, decision=GrantDecision.LOCAL,
                                 approval_tool_call_id="load")
    s.store.activate_pending("parent", "child", "probe", manifest, approval_tool_call_id="load")
    s.store.enter_skill_execution("parent", "child", "probe")
    rail = SubagentPermissionRail(session_id="parent", agent_scope_id="child", grant_store=s.store,
                                  config_provider=lambda: s.policy, approval_sender=AsyncMock())
    rail.set_sessions_root(s.root)
    ctx = context("write_file", {"file_path": "/tmp/probe", "content": "probe"})
    await rail.before_tool_call(ctx)
    assert not ctx.extra.get("_skip_tool")
    assert gate_handled(ctx)
    rail._approval_sender.assert_not_awaited()
    inherited = SkillAuthorizationPermissionRail(config=s.policy)
    inherited.resolve_interrupt = AsyncMock(side_effect=AssertionError("duplicate native gate"))
    await inherited.before_tool_call(ctx)


@pytest.mark.asyncio
async def test_expiry_skips_hitl_and_retires_grant_without_cross_scope_cleanup(setup):
    s = setup
    manifest = child_gate(s)._resolve_manifest_for_call("probe")
    for scope in ("main", "child"):
        s.store.create_pending_grant("parent", scope, manifest, decision=GrantDecision.LOCAL,
                                     approval_tool_call_id="load")
        s.store.activate_pending("parent", scope, "probe", manifest, approval_tool_call_id="load")
    s.store.update_active_skill_state("parent", lambda old: {"active_skill": "probe", "invokes_since_skill_tool": 1})
    rail = SkillActiveStateRail(session_id="parent", sessions_root=s.root, stale_invoke_limit=1)
    ctx = SimpleNamespace(inputs=SimpleNamespace(run_context=SimpleNamespace(
        extra={"__jiuwenswarm_chat_send_source__": "permission_interrupt"})), extra={})
    await rail.before_invoke(ctx)
    assert s.store.get_active("parent", "main") is not None
    ctx.inputs.run_context.extra.clear()
    await rail.before_invoke(ctx)
    assert s.store.get_active("parent", "main") is None
    assert s.store.get_active("parent", "child") is not None
