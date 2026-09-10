"""Small activation contracts not duplicated by real SDK group lifecycle tests."""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest
from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import build_permission_rail
from jiuwenswarm.agents.harness.common.rails.permissions.auto_permission_rail import AutoPermissionInterruptRail
from jiuwenswarm.server.runtime.agent_adapter import interface, interface_deep
from jiuwenswarm.server.runtime.agent_adapter.browser_runtime_security import BrowserRuntimeSecurityProfile
from jiuwenswarm.server.runtime.agent_manager import AgentManager
from tests.unit_tests.agentserver.permissions.test_permission_lifecycle_integration import (
    lifecycle as lifecycle,
)


@pytest.mark.parametrize("persisted", [True, False, RuntimeError("write failed")])
def test_exact_persist_notifies_only_after_success(monkeypatch, tmp_path, persisted):
    persist = Mock(side_effect=persisted if isinstance(persisted, Exception) else None,
                   return_value=persisted)
    notify = Mock()
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permissions_persist."
        "persist_exact_permission_allow_rule", persist,
    )
    config = {"permissions": {"enabled": True, "mode": "auto"}}
    rail = build_permission_rail(
        config, enable_auto_permission=True, permissions_changed_notifier=notify,
        session_id="persist-session", workspace_root=tmp_path,
    )
    assert isinstance(rail, AutoPermissionInterruptRail)
    callback = rail.base_rail._exact_persist_callback
    if isinstance(persisted, Exception):
        with pytest.raises(RuntimeError, match="write failed"):
            callback("bash", {"command": "git status"}, ())
    else:
        assert callback("bash", {"command": "git status"}, ()) is persisted
    persist.assert_called_once_with(
        "bash", {"command": "git status"}, (),
        session_id="persist-session", workspace_root=tmp_path,
    )
    assert notify.call_count == (1 if persisted is True else 0)


def test_facade_propagates_permission_notifier_during_lazy_adapter_creation(monkeypatch):
    class RecordingAdapter:
        def __init__(self):
            self.notifiers = []

        def set_permissions_changed_notifier(self, notifier):
            self.notifiers.append(notifier)

    adapter, notify = RecordingAdapter(), Mock()
    monkeypatch.setattr(interface, "resolve_sdk_choice", lambda: "deep")
    monkeypatch.setattr(interface, "create_adapter", lambda *_args, **_kwargs: adapter)
    facade = interface.JiuWenSwarm()
    facade.set_permissions_changed_notifier(notify)
    assert facade._ensure_adapter() is adapter
    assert facade._ensure_adapter() is adapter
    assert adapter.notifiers == [notify]


@pytest.mark.asyncio
@pytest.mark.parametrize("include_legacy", [False, True])
@pytest.mark.parametrize("change", ["permissions", "model", "already_dirty"])
@pytest.mark.parametrize("installed,desired", [(True, "auto"), (True, "manual"), (False, "auto")])
async def test_permission_notification_preserves_child_reload_versions(
    monkeypatch, include_legacy, change, installed, desired,
):
    router = interface_deep.JiuWenSwarmDeepAdapter()
    config = {"permissions": {"enabled": True, "mode": desired}, "models": {"default": "old"}}
    smart = interface_deep.JiuWenSwarmDeepAdapter()
    smart.mark_as_session_scoped("smart")
    smart._session_instance_mode = "agent"
    smart._enable_auto_permission = installed
    ordinary = interface_deep.JiuWenSwarmDeepAdapter()
    ordinary.mark_as_session_scoped("code")
    ordinary._is_code_agent = True
    for adapter in (router, smart, ordinary):
        adapter._config_base_cache = deepcopy(config)
    router._session_adapters = {"smart": smart, "code": ordinary}
    router._session_adapter_config_version = 2
    router._session_adapter_versions = {"smart": 1 if change == "already_dirty" else 2, "code": 2}
    if change == "model":
        config["models"]["default"] = "new"
    # Only external dotenv/memory IO is stubbed; facade, router and version publication are real.
    monkeypatch.setattr(router, "_apply_reload_config_snapshot", AsyncMock(return_value=config))
    full_snapshot = router._apply_reload_config_snapshot
    facade = interface.JiuWenSwarm()
    facade._adapter = router
    await facade.reload_permissions_config(config, include_legacy=include_legacy)
    assert router._session_adapter_config_version == (3 if include_legacy else 2)
    assert router._session_adapter_versions["code"] == 2
    expected = 1 if change == "already_dirty" else 2
    if include_legacy and change == "permissions":
        expected = 3
    assert router._session_adapter_versions["smart"] == expected
    assert smart._enable_auto_permission is installed
    assert smart._permission_state.permission_epoch is None
    assert full_snapshot.await_count == int(include_legacy)
    if include_legacy:
        assert router._pending_session_reload_config_base == config


@pytest.mark.asyncio
@pytest.mark.parametrize("smart", [False, True])
@pytest.mark.parametrize("include_legacy", [False, True])
async def test_permission_notification_uses_existing_facade_owner(monkeypatch, smart, include_legacy):
    facade = interface.JiuWenSwarm()
    adapter = Mock()
    adapter.has_smart_permission_lifecycle.return_value = smart
    adapter.notify_permissions_changed = AsyncMock()
    facade._adapter = adapter
    legacy = AsyncMock()
    monkeypatch.setattr(facade, "reload_agent_config", legacy)
    config = {"permissions": {"enabled": True, "mode": "manual"}}
    await facade.reload_permissions_config(config, include_legacy=include_legacy)
    if smart:
        adapter.notify_permissions_changed.assert_awaited_once_with(config, include_legacy=include_legacy)
    else:
        adapter.notify_permissions_changed.assert_not_awaited()
    if not smart and include_legacy:
        legacy.assert_awaited_once_with(config_base=config, env_overrides={})
    else:
        legacy.assert_not_awaited()


@pytest.mark.asyncio
async def test_smart_grant_notification_does_not_reload_ordinary_facades(monkeypatch):
    manager = AgentManager()
    config = {"permissions": {"enabled": True, "mode": "manual"}}
    monkeypatch.setattr("jiuwenswarm.server.runtime.agent_manager.get_config", lambda: config)
    smart, ordinary = interface.JiuWenSwarm(), interface.JiuWenSwarm()
    smart._adapter = Mock()
    smart._adapter.has_smart_permission_lifecycle.return_value = True
    smart._adapter.notify_permissions_changed = AsyncMock()
    ordinary._adapter = None
    monkeypatch.setattr(ordinary, "_ensure_adapter", Mock(side_effect=AssertionError("created ordinary owner")))
    manager.agents = {"web": {"smart": smart, "manual": ordinary}}
    await manager.schedule_permissions_reload()
    await manager.wait_for_permissions_ready()
    smart._adapter.notify_permissions_changed.assert_awaited_once_with(config, include_legacy=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,sub_mode,is_code,session_id", [
    ("code", "normal", True, "excluded"), ("team", None, False, "excluded"),
    ("code", "team", False, "excluded"), ("auto_harness", "auto_harness", False, "excluded"),
    ("agent", None, False, "cron_19abc_job1"),
])
async def test_excluded_owner_reload_keeps_develop_dispatch(monkeypatch, mode, sub_mode, is_code, session_id):
    adapter = interface_deep.JiuWenSwarmDeepAdapter()
    adapter.mark_as_session_scoped(session_id)
    adapter._session_instance_mode, adapter._session_instance_sub_mode = mode, sub_mode
    adapter._is_code_agent = is_code
    ordinary_reload = AsyncMock()
    monkeypatch.setattr(adapter, "_reload_agent_config", ordinary_reload)
    replace = AsyncMock(side_effect=AssertionError("excluded owner entered Smart replacement"))
    monkeypatch.setattr(adapter, "_replace_permission_group", replace)
    desired = {"permissions": {"enabled": True, "mode": "auto"}}
    await adapter.reload_agent_config(desired, reload_scopes={"permissions"})
    ordinary_reload.assert_awaited_once_with(desired, None, None, {"permissions"})
    replace.assert_not_awaited()
    assert not adapter._enable_auto_permission


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, 1])
async def test_exit_smart_requires_exact_enabled_true(lifecycle, enabled):
    h = lifecycle
    h.change()
    await h.reload()
    h.change()
    h.desired["permissions"]["enabled"] = enabled
    await h.reload()
    assert not h.adapter._enable_auto_permission
    assert h.adapter._permission_state.permission_epoch is None
    assert h.adapter._stream_event_rail._root_permission_queue is None
    assert not h.adapter._ask_user_rail._strict_continuation_contract
    assert not any(isinstance(rail, AutoPermissionInterruptRail) for rail in h.permissions())
    if enabled is False:
        assert h.permissions() == []


@pytest.mark.asyncio
async def test_replacement_preserves_authority_and_refreshes_browser_profile(lifecycle, monkeypatch):
    h = lifecycle
    h.adapter._platform_trusted_root = h.adapter._workspace_dir / "platform"
    h.change()
    await h.reload()
    old = h.adapter._permission_rail
    profile = BrowserRuntimeSecurityProfile(network_guard_enforced=True, guard_provider="owned-guard")
    h.adapter._browser_runtime_security_profile = profile
    build = Mock(wraps=interface_deep.build_permission_rail)
    monkeypatch.setattr(interface_deep, "build_permission_rail", build)
    h.change()
    await h.reload()
    assert h.adapter._permission_rail is not old
    kwargs = build.call_args.kwargs
    assert kwargs["workspace_root"] == h.adapter._permission_workspace_root
    assert kwargs["platform_trusted_root"] == h.adapter._platform_trusted_root
    assert kwargs["browser_runtime_security_profile"] is profile
    assert kwargs["sys_operation"] is h.adapter._sys_operation
    assert h.adapter._permission_rail.workspace_root == h.adapter._permission_workspace_root
    assert h.permissions() == [h.adapter._permission_rail]


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["enter", "exit", "disable"])
async def test_busy_transition_preserves_old_mode_until_settlement(lifecycle, transition):
    """Exercise Host admission only; exact SDK answer continuation is separate."""
    h = lifecycle
    if transition != "enter":
        h.change()
        await h.reload()
    old_epoch, old_group = h.adapter._permission_state.permission_epoch, h.permissions()
    old_smart = h.adapter._enable_auto_permission
    h.change("manual" if transition == "exit" else "auto")
    if transition == "disable":
        h.desired["permissions"]["enabled"] = False
    h.adapter._mark_session_active("lifecycle")
    try:
        with pytest.raises(RuntimeError, match="permission_session_busy"):
            await h.reload()
        if old_smart:
            await h.request(answer=True)
            assert h.observed[-1][0] == old_epoch
        # A fabricated raw InteractiveInput is not a pending manual answer.
        # Real manual-to-Smart continuation, including partial SDK batches,
        # is exercised in test_permission_answer_cutover.py.
        assert h.permissions() == old_group
        assert h.adapter._enable_auto_permission is old_smart
    finally:
        h.adapter._unmark_session_active("lifecycle")
    await h.reload()
    assert h.adapter._enable_auto_permission is (transition == "enter")
    if transition == "exit":
        assert type(h.adapter._permission_rail) is PermissionInterruptRail
    elif transition == "disable":
        assert h.permissions() == []
