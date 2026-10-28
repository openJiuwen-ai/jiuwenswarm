"""Manual permission hot update keeps the host snapshot on the installed policy."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


def _adapter_with_rail() -> tuple[JiuWenSwarmDeepAdapter, list[tuple[str, object]]]:
    calls: list[tuple[str, object]] = []
    rail = SimpleNamespace(
        replace_installed_global_permissions=lambda permissions: calls.append(
            ("replace", permissions)
        ),
        update_config=lambda config: calls.append(("update", config)),
    )
    adapter = JiuWenSwarmDeepAdapter()
    adapter._is_cron_execution = False
    adapter._parent_session_id = "session-1"
    adapter._permission_rail = rail
    return adapter, calls


def test_update_permission_rail_publishes_global_policy_before_engine_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, calls = _adapter_with_rail()

    def compose(**kwargs):
        calls.append(("compose", dict(kwargs["global_permissions"])))
        return {"enabled": True, "composed": True, "tools": kwargs["global_permissions"]["tools"]}

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permission_compose.compose_host_effective_permissions",
        compose,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers.load_user_permissions",
        lambda: {},
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers.load_session_permissions",
        lambda _session_id: {},
    )

    adapter._update_permission_rail({
        "permissions": {"enabled": True, "mode": "manual", "tools": {"write_file": "ask"}},
    })

    assert [name for name, _payload in calls] == ["replace", "compose", "update"]
    assert calls[0][1]["tools"]["write_file"] == "ask"
    assert calls[2][1]["tools"]["write_file"] == "ask"


def test_update_permission_rail_drops_existing_rail_when_disabled() -> None:
    adapter, calls = _adapter_with_rail()

    adapter._update_permission_rail({
        "permissions": {"enabled": False, "mode": "manual", "tools": {"write_file": "ask"}},
    })

    assert adapter._permission_rail is None
    assert calls == []


@pytest.mark.asyncio
async def test_disabled_reload_unregisters_live_permission_rail() -> None:
    adapter = JiuWenSwarmDeepAdapter()
    rail = object()
    adapter._permission_rail = rail
    adapter._is_cron_execution = False
    unregistered: list[object] = []

    async def unregister_rail(item: object) -> None:
        unregistered.append(item)

    adapter._instance = SimpleNamespace(unregister_rail=unregister_rail)

    await adapter._unregister_permission_rail_if_disabled({
        "permissions": {"enabled": False},
    })

    assert unregistered == [rail]
    assert adapter._permission_rail is None


@pytest.mark.asyncio
async def test_enabled_reload_keeps_live_permission_rail() -> None:
    adapter = JiuWenSwarmDeepAdapter()
    rail = object()
    adapter._permission_rail = rail
    adapter._instance = SimpleNamespace(
        unregister_rail=lambda _item: (_ for _ in ()).throw(AssertionError("unregister"))
    )

    await adapter._unregister_permission_rail_if_disabled({
        "permissions": {"enabled": True, "tools": {"write_file": "ask"}},
    })

    assert adapter._permission_rail is rail
