"""Small activation contracts not duplicated by real SDK group lifecycle tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import build_permission_rail
from jiuwenswarm.agents.harness.common.rails.permissions.auto_permission_rail import AutoPermissionInterruptRail
from jiuwenswarm.server.runtime.agent_adapter import interface, interface_deep
from jiuwenswarm.server.runtime.agent_adapter.browser_runtime_security import BrowserRuntimeSecurityProfile
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
