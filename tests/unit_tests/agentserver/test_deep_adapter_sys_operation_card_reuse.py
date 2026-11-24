# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for sys_operation card sync on isolation-key reuse (JW-74).

A CLI connector that connects first registers the sandbox sys_operation under
the default workspace's isolation key. The agent adapter's
``_resolve_sys_operation`` then reuses that operation, and
``apply_sandbox_runtime_patch`` must mutate the *registered* card's
``extra_params`` — the object the reused operation's providers read — not the
locally built card that was never registered (the "dead card").

These tests drive ``_resolve_sys_operation`` against a fake resource manager
that mirrors the real registration semantics (id → card cache, isolation-key
owner map, first-registrant-wins on ``add_sys_operation``). Cards are real
``SysOperationCard`` instances so the isolation-key derivation goes through
the production ``SysOperation`` path. No sandbox is started and no real
config is read; the sandbox endpoint/runtime lookups are patched so the card
construction is deterministic.
"""

from __future__ import annotations

from typing import Any

import pytest
from openjiuwen.core.sys_operation import OperationMode, SysOperationCard
from openjiuwen.core.sys_operation.config import (
    ContainerScope,
    PreDeployLauncherConfig,
    SandboxGatewayConfig,
    SandboxIsolationConfig,
)

from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


class _FakeSysOperation:
    """Minimal SysOperation double: id plus the isolation key template."""

    def __init__(self, sys_operation_id: str, isolation_key: str | None) -> None:
        self.id = sys_operation_id
        self.isolation_key_template = isolation_key


class _FakeResourceMgr:
    """Registry double mirroring the real add/get/owner-map semantics.

    ``add_sys_operation`` returns a Result-like object and reports an error on
    a duplicate id (the real one errors through ``build_error``; the
    isolation-key collision the adapter handles via the owner-map pre-check
    is exercised by pre-registering a card first). ``_id_to_card`` holds the
    registered cards exactly like ``ResourceMgr._inner_add_resource`` does.
    """

    def __init__(self) -> None:
        self._sys_operations: dict[str, Any] = {}
        self._id_to_card: dict[str, Any] = {}
        self._owner_map: dict[str, str] = {}
        self._resource_registry = _RegistryView(self)

    def add_sys_operation(self, card: Any) -> Any:
        if card.id in self._sys_operations:
            return _Result(err=True, msg="already exists")
        sys_operation = _FakeSysOperation(card.id, _card_isolation_key(card))
        self._sys_operations[card.id] = sys_operation
        self._id_to_card[card.id] = card
        key = sys_operation.isolation_key_template
        if key:
            self._owner_map.setdefault(key, card.id)
        return _Result(err=False, msg="")

    def get_sys_operation(self, sys_operation_id: str) -> Any:
        return self._sys_operations.get(sys_operation_id)

    def remove_sys_operation(self, sys_operation_id: str) -> None:
        sys_operation = self._sys_operations.pop(sys_operation_id, None)
        self._id_to_card.pop(sys_operation_id, None)
        if sys_operation is not None and sys_operation.isolation_key_template:
            self._owner_map.pop(sys_operation.isolation_key_template, None)


class _RegistryView:
    """Stand-in for ``resource_mgr._resource_registry`` (owner-map access)."""

    def __init__(self, mgr: _FakeResourceMgr) -> None:
        self._mgr = mgr

    def sys_operation(self) -> Any:
        return self

    @property
    def _sandbox_key_owner_map(self) -> dict[str, str]:
        return self._mgr._owner_map


class _Result:
    """Result-like double with the ``is_err``/``msg`` surface."""

    def __init__(self, *, err: bool, msg: str) -> None:
        self._err = err
        self._msg = msg

    def is_err(self) -> bool:
        return self._err

    def msg(self) -> str:
        return self._msg


def _card_isolation_key(card: Any) -> str | None:
    """Isolation key template for a card, via the production SysOperation."""
    from openjiuwen.core.sys_operation import SysOperation

    try:
        return SysOperation(card).isolation_key_template
    except Exception:  # noqa: BLE001 - local cards double as non-sandbox cards
        return None


def _sandbox_card(card_id: str, custom_id: str) -> SysOperationCard:
    """Build a sandbox-mode card whose extra_params carry a files policy."""
    return SysOperationCard(
        id=card_id,
        mode=OperationMode.SANDBOX,
        gateway_config=SandboxGatewayConfig(
            isolation=SandboxIsolationConfig(
                container_scope=ContainerScope.CUSTOM,
                custom_id=custom_id,
            ),
            launcher_config=PreDeployLauncherConfig(
                base_url="http://127.0.0.1:18901",
                sandbox_type="jiuwenbox",
                extra_params={
                    "policy": {"filesystem_policy": {"files": []}},
                    "policy_mode": "append",
                    "excluded_commands": [],
                },
            ),
        ),
    )


def _make_adapter(card: Any) -> JiuWenSwarmDeepAdapter:
    """Bare adapter wired so ``_resolve_sys_operation`` builds *card*."""
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_code_agent = False
    adapter._retained_sys_operation_ids = []
    adapter._sys_operation_card = None
    adapter._instance_overrides = {}
    adapter._resolve_project_dir_for_sandbox = lambda: "/home/agentos/workspace"  # type: ignore[method-assign]
    adapter._create_sandbox_sys_operation = lambda *a, **k: card  # type: ignore[method-assign]
    return adapter


@pytest.fixture(name="resource_mgr")
def _resource_mgr(monkeypatch: pytest.MonkeyPatch) -> _FakeResourceMgr:
    fake = _FakeResourceMgr()
    monkeypatch.setattr(interface_deep.Runner, "resource_mgr", fake)
    yield fake


def _sandbox_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``_resolve_sys_operation`` take the sandbox branch."""
    monkeypatch.setattr(
        interface_deep,
        "get_sandbox_endpoint",
        lambda: {"url": "http://127.0.0.1:18901", "type": "jiuwenbox"},
    )
    monkeypatch.setattr(
        interface_deep,
        "get_sandbox_runtime",
        lambda: {"enabled": True, "files": {"allow": [], "deny": []}},
    )


def test_reuse_points_card_at_registered_card(
    resource_mgr: _FakeResourceMgr, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI-style first registration wins; the adapter's card must follow it.

    Without the sync, ``self._sys_operation_card`` stays on the locally built
    card that was never registered, so ``apply_sandbox_runtime_patch``
    mutates a dead card while the reused operation's providers read the
    registered card's extra_params.
    """
    _sandbox_enabled(monkeypatch)
    cli_card = _sandbox_card("mcp_cli_install", "project_abc123")
    resource_mgr.add_sys_operation(cli_card)

    local_card = _sandbox_card("agent_local", "project_abc123")
    adapter = _make_adapter(local_card)

    sys_operation = adapter._resolve_sys_operation()

    assert sys_operation is not None
    assert sys_operation.id == "mcp_cli_install"
    assert adapter._sys_operation_card is cli_card


def test_reuse_after_add_failure_points_card_at_registered_card(
    resource_mgr: _FakeResourceMgr, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The add-failure reuse branch must sync the card the same way.

    Models the real race: the owner-map pre-check misses (the other registrar
    is mid-registration), ``add_sys_operation`` then fails on the isolation
    key, and the post-failure lookup finds the CLI operation. That second
    reuse path must sync the card exactly like the pre-check path.
    """
    _sandbox_enabled(monkeypatch)
    cli_card = _sandbox_card("mcp_cli_install", "project_abc123")
    resource_mgr.add_sys_operation(cli_card)

    local_card = _sandbox_card("agent_local", "project_abc123")
    adapter = _make_adapter(local_card)

    real_lookup = JiuWenSwarmDeepAdapter.__dict__[
        "_get_registered_sys_operation_by_isolation_key"
    ].__get__(None, JiuWenSwarmDeepAdapter)
    calls = {"count": 0}

    def staged_lookup(key: str | None) -> Any:
        calls["count"] += 1
        if calls["count"] == 1:
            # Pre-check misses: the CLI registration is not visible yet.
            return None
        # Post-add-failure lookup sees the CLI operation.
        return real_lookup(key)

    monkeypatch.setattr(
        interface_deep.JiuWenSwarmDeepAdapter,
        "_get_registered_sys_operation_by_isolation_key",
        staticmethod(staged_lookup),
    )

    real_add = resource_mgr.add_sys_operation

    def failing_add(card: Any) -> Any:
        if card.id == local_card.id:
            # Mirror the real manager's Result error for a lost key race.
            return _Result(err=True, msg="isolation key already registered")
        return real_add(card)

    monkeypatch.setattr(resource_mgr, "add_sys_operation", failing_add)

    sys_operation = adapter._resolve_sys_operation()

    assert calls["count"] == 2
    assert sys_operation is not None
    assert sys_operation.id == "mcp_cli_install"
    assert adapter._sys_operation_card is cli_card


def test_no_reuse_registers_local_card(
    resource_mgr: _FakeResourceMgr, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh registration keeps pointing at the card the adapter built."""
    _sandbox_enabled(monkeypatch)
    local_card = _sandbox_card("agent_local", "project_abc123")

    adapter = _make_adapter(local_card)
    sys_operation = adapter._resolve_sys_operation()

    assert sys_operation is not None
    assert sys_operation.id == "agent_local"
    assert adapter._sys_operation_card is local_card


def test_runtime_patch_mutates_registered_card(
    resource_mgr: _FakeResourceMgr, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``apply_sandbox_runtime_patch`` must reach the provider's card.

    This is the user-visible contract: after a CLI connector connects first
    and the session runs in the default workspace, ``/sandbox files deny``
    has to change the extra_params of the card the reused operation's
    providers read. The policy build and the jiuwenbox recreate call are
    patched so the assertions stay on the extra_params mutation.
    """
    import asyncio

    _sandbox_enabled(monkeypatch)
    cli_card = _sandbox_card("mcp_cli_install", "project_abc123")
    resource_mgr.add_sys_operation(cli_card)

    local_card = _sandbox_card("agent_local", "project_abc123")
    adapter = _make_adapter(local_card)
    adapter._resolve_sys_operation()

    new_policy = {
        "filesystem_policy": {
            "files": [{"path": "/home/agentos/workspace/secret", "mode": "deny"}],
        },
    }
    monkeypatch.setattr(
        interface_deep,
        "build_filesystem_policy",
        lambda files, **kwargs: (new_policy, []),
    )
    monkeypatch.setattr(
        "openjiuwen.extensions.sys_operation.sandbox.providers.jiuwenbox."
        "force_recreate_jiuwenbox_sandbox",
        lambda *a, **k: asyncio.sleep(0, result="new_sandbox_1"),
    )

    runtime = {
        "enabled": True,
        "files": {"allow": [], "deny": ["/home/agentos/workspace/secret"]},
        "excluded_commands": ["rm"],
        "fallback_on_failure": False,
    }
    asyncio.run(adapter.apply_sandbox_runtime_patch(runtime, files_changed=True))

    cli_extra = cli_card.gateway_config.launcher_config.extra_params
    assert cli_extra["policy"] is new_policy
    assert cli_extra["excluded_commands"] == ["rm"]
    assert cli_extra["sandbox_id"] == "new_sandbox_1"
    local_extra = local_card.gateway_config.launcher_config.extra_params
    assert local_extra.get("policy") is not new_policy


def test_missing_registered_card_keeps_local_card(
    resource_mgr: _FakeResourceMgr, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registry without a card cache keeps the old (local-card) behavior.

    The sync is best effort: when the id→card lookup misses, the adapter must
    not crash and must keep whatever card it already resolved.
    """
    _sandbox_enabled(monkeypatch)
    cli_card = _sandbox_card("mcp_cli_install", "project_abc123")
    resource_mgr.add_sys_operation(cli_card)
    # Simulate a registry whose card cache lost the entry (defensive path).
    resource_mgr._id_to_card.clear()

    local_card = _sandbox_card("agent_local", "project_abc123")
    adapter = _make_adapter(local_card)

    sys_operation = adapter._resolve_sys_operation()

    assert sys_operation is not None
    assert adapter._sys_operation_card is local_card
