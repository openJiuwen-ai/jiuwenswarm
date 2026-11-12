# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""模板写端点 notify_runtime 豁免行为：扩展配置写后不戳 runtime、只失效本进程缓存；其余模板照常戳。"""

# ruff: noqa: E402 -- install optional openjiuwen-runtime DB stubs before imports

from __future__ import annotations

import importlib
import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi.testclient import TestClient


def _install_runtime_db_stubs() -> None:
    """Supply table-definition types when the lightweight test env lacks runtime DB."""
    try:
        __import__("openjiuwen_runtime.foundation.db.table_def")
        return
    except ModuleNotFoundError:
        pass

    class _Definition:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.args = args
            for key, value in kwargs.items():
                setattr(self, key, value)

    handler = ModuleType("openjiuwen_runtime.foundation.db.handler")
    handler.DBHandler = object  # type: ignore[attr-defined]
    table_def = ModuleType("openjiuwen_runtime.foundation.db.table_def")
    table_def.ColumnDefinition = _Definition  # type: ignore[attr-defined]
    table_def.IndexDefinition = _Definition  # type: ignore[attr-defined]
    table_def.TableDefinition = _Definition  # type: ignore[attr-defined]
    sys.modules[handler.__name__] = handler
    sys.modules[table_def.__name__] = table_def
    db_package = sys.modules["openjiuwen_runtime.foundation.db"]
    for module_name, class_name in (
        ("mysql_handler", "MySQLHandler"),
        ("postgresql_handler", "PostgreSQLHandler"),
        ("sqlite_handler", "SQLiteHandler"),
    ):
        module = ModuleType(f"openjiuwen_runtime.foundation.db.{module_name}")
        setattr(module, class_name, type(class_name, (), {}))
        sys.modules[module.__name__] = module
        setattr(db_package, module_name, module)
    runtime_log = ModuleType("openjiuwen_runtime.foundation.log")
    runtime_log.get_logger = logging.getLogger  # type: ignore[attr-defined]
    sys.modules[runtime_log.__name__] = runtime_log


def _install_audit_stub() -> None:
    """老 pin 的 openjiuwen_runtime.foundation.audit 缺 get_audit_manager（存量环境问题，
    与检视记录中 2 failed 同源）；缺失时给最小桩，仅供模板写链路导入通过。"""
    try:
        from openjiuwen_runtime.foundation.audit import get_audit_manager  # noqa: F401
        return
    except ImportError:
        pass

    class _StubAuditManager:
        def apply_config(self, config):
            return None

    audit = ModuleType("openjiuwen_runtime.foundation.audit")

    class _AuditLogConfig:
        @staticmethod
        def from_dict(data, validate=True):
            del validate
            return data

    class _SchemaValidationError(Exception):
        pass

    audit.AuditLogConfig = _AuditLogConfig  # type: ignore[attr-defined]
    audit.SchemaValidationError = _SchemaValidationError  # type: ignore[attr-defined]
    audit.get_audit_manager = lambda: _StubAuditManager()  # type: ignore[attr-defined]
    sys.modules["openjiuwen_runtime.foundation.audit"] = audit


_install_runtime_db_stubs()
_install_audit_stub()

from jiuwenswarm.gateway.config.enterprise import (  # noqa: E402
    clear_enterprise_record_repositories,
    set_enterprise_record_repositories,
)
from jiuwenswarm.gateway.storage.backends.memory_persistent import (  # noqa: E402
    InMemoryPersistentBackend,
)
from jiuwenswarm.gateway.storage_assembly.setup import (  # noqa: E402
    create_enterprise_record_repositories,
)


def _import_manager_config_receiver_module(module_suffix: str) -> Any:
    """Load the bundled extension without relying on the removed runtime importer."""
    parent_name = "jiuwenswarm.loaded_extension"
    package_name = f"{parent_name}.manager_config_receiver"
    extension_root = (
        Path(__file__).resolve().parents[3]
        / "packages"
        / "jiuwenclaw-ee"
        / "gateway"
        / "extensions"
        / "manager_config_receiver"
    )
    if parent_name not in sys.modules:
        parent = ModuleType(parent_name)
        parent.__path__ = []  # type: ignore[attr-defined]
        sys.modules[parent_name] = parent
    if package_name not in sys.modules:
        package = ModuleType(package_name)
        package.__path__ = [str(extension_root)]  # type: ignore[attr-defined]
        package.__package__ = package_name
        sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.{module_suffix}")


@pytest.fixture
def notify_counts(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, int]]:
    """Spy on the module-level notify hooks used by template_routers handlers."""
    routers = _import_manager_config_receiver_module("routers.template_routers")
    counts = {"trigger": 0, "invalidate": 0}

    def _trigger() -> None:
        counts["trigger"] += 1

    def _invalidate() -> None:
        counts["invalidate"] += 1

    monkeypatch.setattr(routers, "trigger_runtime_config_update", _trigger)
    monkeypatch.setattr(routers, "invalidate_local_enterprise_config_caches", _invalidate)
    yield counts


@pytest.fixture
def receiver_client(notify_counts: dict[str, int]) -> Iterator[tuple[TestClient, dict[str, int]]]:
    store = InMemoryPersistentBackend()
    repos = create_enterprise_record_repositories(store)
    set_enterprise_record_repositories(repos)
    app_module = _import_manager_config_receiver_module("http.app")
    try:
        with TestClient(app_module.create_app(), base_url="http://gateway.test") as client:
            yield client, notify_counts
    finally:
        clear_enterprise_record_repositories()


_EXTENSION_BODY = {
    "template_id": "ext-notify-gate",
    "template_name": "notify gate test",
    "component": "agent_server",
    "hook_type": "pre_request",
    "hook_config": {"handler": "noop", "params": {}},
}


def test_extension_config_writes_never_trigger_runtime(receiver_client) -> None:
    client, counts = receiver_client

    resp = client.post("/api/v1/extension-config-templates", json=_EXTENSION_BODY)
    assert resp.status_code == 200, resp.text
    resp = client.patch(
        "/api/v1/extension-config-templates/ext-notify-gate",
        json={"template_name": "renamed"},
    )
    assert resp.status_code == 200, resp.text
    resp = client.request("DELETE", "/api/v1/extension-config-templates/ext-notify-gate", json={})
    assert resp.status_code == 200, resp.text

    assert counts["trigger"] == 0
    assert counts["invalidate"] == 3


def test_permissions_template_writes_still_trigger_runtime(receiver_client) -> None:
    client, counts = receiver_client

    resp = client.post(
        "/api/v1/permissions-templates",
        json={
            "template_id": "perm-notify-gate",
            "template_name": "notify gate test",
            "body": {"allow": [], "ask": []},
        },
    )
    assert resp.status_code == 200, resp.text
    assert counts["trigger"] == 1
    assert counts["invalidate"] == 0

    # 删除同样"每写必戳"（旧行为保留）。
    resp = client.request("DELETE", "/api/v1/permissions-templates/perm-notify-gate", json={})
    assert resp.status_code == 200, resp.text
    assert counts["trigger"] == 2
    assert counts["invalidate"] == 0
