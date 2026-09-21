# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from typing import Any

import pytest

from openjiuwen_runtime.foundation.audit import (
    AuditManager,
    MemoryEmitter,
    default_format,
    get_audit_manager,
    reset_audit_manager,
)

from jiuwenswarm.gateway.config.enterprise import (
    EnterpriseRecordRepository,
    clear_enterprise_record_repositories,
    set_enterprise_record_repository,
)
from jiuwenswarm.gateway.config_poll.appliers import (
    TABLE_APPLIERS,
    TableApplyContext,
    apply_audit_log_config_table,
)
from jiuwenswarm.gateway.config_poll.db import POLL_TABLES
from jiuwenswarm.gateway.storage.backends.memory_persistent import InMemoryPersistentBackend
from tests.unit_tests.gateway.test_manager_config_receiver_a2a import (
    import_manager_config_receiver_module,
)


def _sample_body(**overrides) -> dict[str, Any]:
    fmt = default_format()
    body: dict[str, Any] = {
        "format": {
            "schema_version": fmt.schema_version,
            "header_fields": list(fmt.header_fields),
            "content_fields": list(fmt.content_fields),
            "required_fields": list(fmt.required_fields),
            "placeholder": fmt.placeholder,
            "timestamp_format": fmt.timestamp_format,
        },
        "otel": {
            "enabled": True,
            "endpoint": "http://collector:4317",
            "protocol": "grpc",
            "headers": {},
        },
        "data_center": "N",
        "system_code": "JIUWEN",
        "node": "gw-1",
    }
    body.update(overrides)
    return body


@pytest.fixture(autouse=True)
def _reset_audit_manager():
    reset_audit_manager(AuditManager(emitter=MemoryEmitter()))
    yield
    reset_audit_manager()
    clear_enterprise_record_repositories()


def test_poll_tables_include_audit_log_config() -> None:
    assert "audit_log_config" in POLL_TABLES
    assert frozenset(TABLE_APPLIERS) == frozenset(POLL_TABLES)


@pytest.mark.asyncio
async def test_apply_audit_log_config_table_hot_loads() -> None:
    await apply_audit_log_config_table(
        TableApplyContext(rows=[{"body": _sample_body(system_code="POLL")}])
    )
    assert get_audit_manager().config.identity.system_code == "POLL"
    assert get_audit_manager().config.service == "gateway"


@pytest.mark.asyncio
async def test_ee_audit_log_service_upsert_and_delete() -> None:
    service_mod = import_manager_config_receiver_module(
        "core.application_config.audit_log_config"
    )
    store = InMemoryPersistentBackend()
    repo = EnterpriseRecordRepository(store, "audit_log_config", instance_id="")
    set_enterprise_record_repository("audit_log_config", repo)

    svc = service_mod.AuditLogConfigService()
    row = await svc.upsert(
        body=_sample_body(system_code="EE"),
        schema_version="1.0.0",
        otel_enabled=True,
        otel_endpoint="http://collector:4317",
        otel_protocol="grpc",
        data_center="N",
        system_code="EE",
        node="gw-1",
        ntp_servers=["ntp.example"],
        source="manager",
        revision=1,
    )
    assert row is not None
    assert row["system_code"] == "EE"
    assert isinstance(row["body"], dict)
    assert get_audit_manager().config.identity.system_code == "EE"
    assert await repo.get() is not None

    await svc.delete()
    assert await repo.get() is None
    assert get_audit_manager().config.service == "gateway"
