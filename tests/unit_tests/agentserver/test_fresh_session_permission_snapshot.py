"""A new session must not re-enable permissions from an older parent snapshot."""

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


class _IdleSession:
    """Idle child used to capture the config handed to lazy reload."""

    def __init__(self) -> None:
        self.reload_calls: list[dict] = []

    async def reload_agent_config(self, *args, **kwargs):
        self.reload_calls.append({"args": args, "kwargs": kwargs})

    def _should_defer_permission_reload(self, _config_base, *, session_id: str, **_kwargs) -> bool:
        _ = session_id
        return False

    def _has_permission_config_delta(self, *_args, **_kwargs) -> bool:
        return False

    def _uses_smart_permission_lifecycle(self, _config_base=None) -> bool:
        return False


def _parent_with_pending(enabled: bool) -> JiuWenSwarmDeepAdapter:
    parent = JiuWenSwarmDeepAdapter()
    parent._mark_session_adapters_stale_for_reload(
        {
            "permissions": {"enabled": enabled, "tools": {"write_file": "ask"}},
            "react": {"agent_name": "kept"},
        },
        None,
    )
    parent._pending_session_reload_config_stamp = (1, 10)
    return parent


@pytest.mark.asyncio
async def test_fresh_session_uses_disk_permissions_when_snapshot_is_older(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent_with_pending(True)
    session = _IdleSession()
    monkeypatch.setattr(parent, "_current_config_file_stamp", lambda: (2, 10))
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.get_config",
        lambda: {"permissions": {"enabled": False, "mode": "manual"}},
    )

    await parent._reload_session_adapter_if_stale(
        "session-new",
        session,
        freshly_built=True,
    )

    passed = session.reload_calls[0]["args"][0]
    assert passed["permissions"]["enabled"] is False
    assert passed["react"]["agent_name"] == "kept"


@pytest.mark.asyncio
async def test_fresh_session_keeps_pending_permissions_when_snapshot_is_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent_with_pending(True)
    session = _IdleSession()
    parent._pending_session_reload_config_stamp = (2, 10)
    monkeypatch.setattr(parent, "_current_config_file_stamp", lambda: (2, 10))
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.get_config",
        lambda: {"permissions": {"enabled": False, "mode": "manual"}},
    )

    await parent._reload_session_adapter_if_stale(
        "session-new",
        session,
        freshly_built=True,
    )

    passed = session.reload_calls[0]["args"][0]
    assert passed["permissions"]["enabled"] is True


@pytest.mark.asyncio
async def test_existing_session_still_applies_pending_permission_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent_with_pending(True)
    session = _IdleSession()
    monkeypatch.setattr(parent, "_current_config_file_stamp", lambda: (2, 10))
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.get_config",
        lambda: {"permissions": {"enabled": False, "mode": "manual"}},
    )

    await parent._reload_session_adapter_if_stale("session-old", session)

    passed = session.reload_calls[0]["args"][0]
    assert passed["permissions"]["enabled"] is True
