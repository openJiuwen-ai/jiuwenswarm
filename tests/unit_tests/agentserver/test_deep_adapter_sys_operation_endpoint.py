# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Sandbox sys_operation reuse must follow the current jiuwenbox endpoint."""

from __future__ import annotations

import pytest
from openjiuwen.core.sys_operation import OperationMode, SandboxGatewayConfig, SysOperation, SysOperationCard
from openjiuwen.core.sys_operation.config import (
    ContainerScope,
    PreDeployLauncherConfig,
    SandboxIsolationConfig,
)

from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def _card(base_url: str) -> SysOperationCard:
    return SysOperationCard(
        mode=OperationMode.SANDBOX,
        gateway_config=SandboxGatewayConfig(
            isolation=SandboxIsolationConfig(container_scope=ContainerScope.CUSTOM, custom_id="project_x"),
            launcher_config=PreDeployLauncherConfig(base_url=base_url, sandbox_type="jiuwenbox"),
        ),
    )


class _FakeResourceMgr:
    def __init__(self) -> None:
        self.ops: dict[str, SysOperation] = {}
        self.owner: dict[str, str] = {}
        self.removed: list[str] = []

    def add_sys_operation(self, card: SysOperationCard):
        op = SysOperation(card)
        self.ops[op.id] = op
        self.owner[op.isolation_key_template] = op.id

        class _Ok:
            @staticmethod
            def is_err() -> bool:
                return False

        return _Ok()

    def get_sys_operation(self, sys_operation_id: str):
        return self.ops.get(sys_operation_id)

    def remove_sys_operation(self, sys_operation_id: str) -> None:
        op = self.ops.pop(sys_operation_id, None)
        if op is not None:
            self.owner.pop(op.isolation_key_template, None)
        self.removed.append(sys_operation_id)


@pytest.fixture(name="resource_mgr")
def _resource_mgr(monkeypatch: pytest.MonkeyPatch) -> _FakeResourceMgr:
    fake = _FakeResourceMgr()
    monkeypatch.setattr(interface_deep.Runner, "resource_mgr", fake)
    monkeypatch.setattr(
        JiuWenSwarmDeepAdapter,
        "_get_registered_sys_operation_by_isolation_key",
        staticmethod(lambda key: fake.ops.get(fake.owner.get(key, ""))),
    )
    monkeypatch.setattr(interface_deep, "get_sandbox_runtime", lambda: {"enabled": True})
    return fake


def _resolve(monkeypatch: pytest.MonkeyPatch, base_url: str) -> SysOperation | None:
    monkeypatch.setattr(interface_deep, "get_sandbox_endpoint", lambda: {"url": base_url, "type": "jiuwenbox"})
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._create_sandbox_sys_operation = lambda url, _type, **_kw: _card(url)
    adapter._resolve_project_dir_for_sandbox = lambda: None
    return adapter._resolve_sys_operation()


def test_same_endpoint_reuses_registered_sys_operation(
    monkeypatch: pytest.MonkeyPatch, resource_mgr: _FakeResourceMgr
) -> None:
    first = _resolve(monkeypatch, "http://127.0.0.1:44005")
    second = _resolve(monkeypatch, "http://127.0.0.1:44005")

    assert first is not None and second is first
    assert resource_mgr.removed == []


def test_changed_endpoint_replaces_stale_sys_operation(
    monkeypatch: pytest.MonkeyPatch, resource_mgr: _FakeResourceMgr
) -> None:
    stale = _resolve(monkeypatch, "http://127.0.0.1:44005")
    fresh = _resolve(monkeypatch, "http://127.0.0.1:44006")

    assert stale is not None and fresh is not None
    assert fresh.id != stale.id
    assert resource_mgr.removed == [stale.id]
    assert JiuWenSwarmDeepAdapter._sandbox_base_url_of(fresh._run_config.config) == "http://127.0.0.1:44006"
