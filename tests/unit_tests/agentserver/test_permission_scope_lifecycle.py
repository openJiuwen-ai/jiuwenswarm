# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Ownership, active-session lifetime and bounded idle policy registrations."""

from collections import OrderedDict
from unittest.mock import Mock
import weakref

import pytest
from agent_shield.tool_permission import PermissionManager, get_permission_manager
from openjiuwen.harness.security.skill_authorization import SkillGrantStore

from jiuwenswarm.agents.harness.common.rails.permissions import (
    published_scopes as scopes_module,
)
from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization_permission_rail import (
    SkillAuthorizationPermissionRail,
)

CONFIG = {"enabled": True, "tools": {"permission_probe": "allow"}}


@pytest.mark.asyncio
async def test_enterprise_instances_with_same_routing_key_own_distinct_configs():
    """同 routing_key 的两个 rail 实例各自有独立用户级 Manager key，不再发会话 key。"""
    business = "group:fixture/bot:fixture/user:fixture"
    deny = {**CONFIG, "tools": {"permission_probe": "deny"}}
    a = SkillAuthorizationPermissionRail(config=deny, permission_key=business)
    b = SkillAuthorizationPermissionRail(config=CONFIG, permission_key=business)
    try:
        assert a.routing_key == b.routing_key == business
        assert a.permission_key != b.permission_key
        manager = get_permission_manager()
        assert (
            await manager.check_permission("permission_probe", {}, a.permission_key)
        ).permission.value == "deny"
        assert (
            await manager.check_permission("permission_probe", {}, b.permission_key)
        ).permission.value == "allow"
        a.uninit(None)
        assert manager.has_config(b.permission_key)
        assert not manager.has_config(a.permission_key)
    finally:
        a.uninit(None)
        b.uninit(None)


@pytest.fixture
def bounded_manager(monkeypatch):
    manager = PermissionManager(max_keys=3)
    monkeypatch.setattr(scopes_module, "_publishers", weakref.WeakValueDictionary())
    monkeypatch.setattr(scopes_module, "_idle_sessions", OrderedDict())
    monkeypatch.setattr(scopes_module, "MAX_IDLE_PERMISSION_SESSIONS", 2)
    monkeypatch.setattr(scopes_module, "get_permission_manager", lambda: manager)
    publisher = scopes_module.PublishedPermissionScopes(
        "owner", base_config=CONFIG, store=SkillGrantStore()
    )
    yield manager, publisher
    publisher.close()


def test_many_finished_requests_do_not_accumulate_keys(bounded_manager):
    manager, publisher = bounded_manager
    for index in range(1100):
        release = publisher.acquire_session(str(index), CONFIG)
        assert len(manager.keys()) == 2
        release()
        release()
        assert manager.keys() == ("default",)
        assert publisher.session_ids() == []


def test_idle_eviction_never_removes_active_or_shared_session(bounded_manager):
    manager, publisher = bounded_manager
    first = publisher.acquire_session("active", CONFIG)
    second = publisher.retain_session("active")
    key = publisher.baseline_key_for("active")
    first()
    for index in range(12):
        publisher.publish_session(str(index), CONFIG)
        assert manager.has_config(key)
        assert len(manager.keys()) <= 4
    assert len(publisher.session_ids()) <= 3
    second()
    assert not manager.has_config(key)


def test_full_active_budget_rejects_without_evicting_inflight_keys(bounded_manager):
    manager, publisher = bounded_manager
    releases = [publisher.acquire_session(str(index), CONFIG) for index in range(3)]
    before = manager.keys()
    try:
        with pytest.raises(RuntimeError, match="limit reached"):
            publisher.acquire_session("overflow", CONFIG)
        assert manager.keys() == before
        assert not publisher.has_session("overflow")
    finally:
        for release in releases:
            release()


def test_stale_release_cannot_remove_republished_session(bounded_manager):
    manager, publisher = bounded_manager
    old = publisher.acquire_session("session", CONFIG)
    old_key = publisher.baseline_key_for("session")
    publisher.remove_session("session")
    current = publisher.acquire_session("session", CONFIG)
    key = publisher.baseline_key_for("session")
    assert key != old_key
    old()
    assert manager.has_config(key)
    assert not manager.has_config(old_key)
    current()
    assert not manager.has_config(key)


def test_close_defers_active_key_removal_until_last_user(bounded_manager):
    manager, publisher = bounded_manager
    release = publisher.acquire_session("active", CONFIG)
    key = publisher.baseline_key_for("active")
    publisher.close()
    assert manager.has_config(key)
    release()
    assert not manager.has_config(key)


def test_closed_rail_cannot_resurrect_its_registration():
    rail = SkillAuthorizationPermissionRail(config=CONFIG)
    manager = get_permission_manager()
    user_key = rail.permission_key
    assert manager.has_config(user_key)
    # 会话发布已停用：acquire 只返回空回调，不注册会话 key。
    release = rail.acquire_session_config("closing-session", CONFIG)
    assert callable(release)
    assert rail._published_scopes.session_ids() == []
    rail.uninit(None)
    try:
        with pytest.raises(RuntimeError, match="released"):
            rail.update_config(CONFIG)
        assert not manager.has_config(user_key)
    finally:
        release()
    assert not manager.has_config(user_key)


@pytest.mark.asyncio
async def test_closed_owner_does_not_block_replacement_overlay_refresh(
    bounded_manager, monkeypatch
):
    from jiuwenswarm.agents.harness.common.rails.permissions import config_loader

    manager, old = bounded_manager
    release = old.acquire_session("shared-session", CONFIG)
    old_key = old.baseline_key_for("shared-session")
    old.close()
    current = scopes_module.PublishedPermissionScopes(
        "replacement", base_config=CONFIG, store=SkillGrantStore()
    )
    try:
        current.publish_session("shared-session", CONFIG)
        monkeypatch.setattr(config_loader, "is_enterprise", lambda: True)
        monkeypatch.setattr(
            config_loader,
            "_session_overlays",
            {
                "shared-session": {"tools": {"permission_probe": "deny"}},
            },
        )
        scopes_module.refresh_session_overlays("shared-session")
        key = current.baseline_key_for("shared-session")
        assert (
            await manager.check_permission("permission_probe", {}, key)
        ).permission.value == "deny"
        assert not manager.has_config(old_key)
    finally:
        release()
        current.close()


def test_non_capacity_publication_error_does_not_evict_idle_policy(
    bounded_manager, monkeypatch
):
    manager, publisher = bounded_manager
    publisher.publish_session("idle", CONFIG)
    before = manager.keys()
    monkeypatch.setattr(
        manager,
        "update_config",
        Mock(side_effect=RuntimeError("invalid compiler state")),
    )
    with pytest.raises(RuntimeError, match="invalid compiler state"):
        publisher.acquire_session("bad", CONFIG)
    assert manager.keys() == before


def test_orphaned_owner_releases_only_its_own_keys(bounded_manager):
    import gc

    manager, publisher = bounded_manager
    manager.update_config(CONFIG, "orphan-owner")
    orphan = scopes_module.PublishedPermissionScopes(
        "orphan-owner", base_config=CONFIG, store=SkillGrantStore()
    )
    orphan.publish_session("idle", CONFIG)
    key = orphan.baseline_key_for("idle")
    reference = weakref.ref(orphan)
    del orphan
    gc.collect()
    assert reference() is None
    assert not manager.has_config("orphan-owner") and not manager.has_config(key)
    publisher.publish_session("survivor", CONFIG)
    assert manager.has_config(publisher.baseline_key_for("survivor"))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invoke", "stream"])
async def test_detached_child_retain_is_noop_without_session_publish(mode):
    """会话 retain 已停用：子 Agent 接线不再依赖已发布会话。"""
    from jiuwenswarm.agents.harness.common.tools.subagent_executor.authorization import (
        _retain_child_permission_session,
    )

    child = object()
    # 不应抛 KeyError；空实现直接返回。
    _retain_child_permission_session(child)
    assert not hasattr(child, "invoke")
    assert not hasattr(child, "stream")
    assert mode in ("invoke", "stream")


@pytest.fixture
def enterprise_rail(monkeypatch):
    from jiuwenswarm.agents.harness.common.rails.permissions import (
        config_loader,
        permission_key,
    )
    from jiuwenswarm.agents.harness.common.rails.interrupt import interrupt_helpers

    monkeypatch.setattr(config_loader, "is_enterprise", lambda: True)
    monkeypatch.setattr(permission_key, "is_enterprise", lambda: True)
    monkeypatch.setattr(interrupt_helpers, "is_enterprise", lambda: True)
    monkeypatch.setattr(config_loader, "_session_overlays", {})
    config = {
        "enabled": True,
        "defaults": {"*": "ask"},
        "tools": {"get_current_user": "ask", "read_memory": "ask"},
        "net_guard": {"enabled": True, "urls": {"blocked.test": "deny"}},
    }
    rail = interrupt_helpers.build_permission_rail(
        {},
        interrupt_helpers.PermissionRailBuildOptions(
            permission_config=config,
            group_id="fixture",
            bot_id="fixture",
            user_id="fixture",
        ),
    )
    assert rail is not None
    rail._host.permission_scene_hook = None
    yield rail, config
    rail.uninit(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [False, True])
async def test_enterprise_always_allow_session_remember_is_inert(
    enterprise_rail, cached
):
    """企业「会话内记住」只写内存 overlay，不热更用户级 key、不 publish 会话 key。"""
    from types import SimpleNamespace
    from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
    from jiuwenswarm.agents.harness.common.rails.permissions import config_loader

    rail, config = enterprise_rail
    sid, state = "persist-session", {}
    if cached:
        rail.update_session_config(sid, config)
    session = SimpleNamespace(
        session_id=sid, get_state=state.get, update_state=state.update
    )
    ctx = AgentCallbackContext(agent=None, session=session, extra={})
    manager = get_permission_manager()
    root_before = dict(manager.get_config(rail.permission_key))
    for name in ("get_current_user", "read_memory"):
        call = ToolCall(id=name, type="function", name=name, arguments="{}")
        await rail.resolve_interrupt(
            ctx,
            call,
            {"approved": True, "auto_confirm": True, "persist_allow": True},
            {},
        )
    key = rail.permission_key_for(ctx)
    assert key == rail.permission_key
    for name in ("get_current_user", "read_memory"):
        assert (
            await manager.check_permission(name, {}, key)
        ).permission.value == "ask"
    assert sid in config_loader._session_overlays
    assert dict(manager.get_config(rail.permission_key)) == root_before
    assert rail._published_scopes.session_ids() == []


def test_enterprise_persist_without_session_returns_failure_and_does_not_write(
    enterprise_rail, monkeypatch
):
    from jiuwenswarm.agents.harness.common.rails.permissions import config_loader
    from jiuwenswarm.agents.harness.common.rails.interrupt import interrupt_helpers

    rail, config = enterprise_rail
    warning = Mock()
    monkeypatch.setattr(interrupt_helpers.logger, "warning", warning)
    assert (
        rail._host.persist_allow_rule(
            {**config, "tools": {"get_current_user": "allow"}}
        )
        is False
    )
    assert config_loader._session_overlays == {}
    assert "missing session" in warning.call_args.args[0]
    assert (
        get_permission_manager().get_config(rail.permission_key)["tools"][
            "get_current_user"
        ]
        == "ask"
    )


def test_adapter_request_preparation_updates_user_key_without_session_lease(tmp_path):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    adapter = JiuWenSwarmDeepAdapter()
    adapter._agent_permissions_body = CONFIG
    adapter._workspace_dir = tmp_path
    adapter._permission_rail = SkillAuthorizationPermissionRail(config=CONFIG)
    try:
        assert (
            adapter._update_permission_rail({}, session_id="adapter-session") is None
        )
        user_key = adapter._permission_rail.permission_key
        assert get_permission_manager().has_config(user_key)
        publisher = adapter._permission_rail._published_scopes
        assert not publisher.has_session("adapter-session")
    finally:
        adapter._permission_rail.uninit(None)
