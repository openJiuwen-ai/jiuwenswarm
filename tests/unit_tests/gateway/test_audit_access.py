# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import pytest

from openjiuwen_runtime.foundation.audit import (
    AuditManager,
    MemoryEmitter,
    default_format,
    get_audit_manager,
    reset_audit_manager,
)

from jiuwenswarm.common.audit_gate import is_audit_config_enabled
from jiuwenswarm.gateway.config.audit.access import (
    apply_audit_log_config_payload,
    apply_audit_log_config_row,
    reload_audit_log_config_from_db,
)
from jiuwenswarm.gateway.config.enterprise import (
    EnterpriseRecordRepository,
    clear_enterprise_record_repositories,
    set_enterprise_record_repository,
)
from jiuwenswarm.gateway.storage.backends.memory_persistent import InMemoryPersistentBackend


def _sample_body(**overrides) -> dict:
    fmt = default_format()
    body = {
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
        "ntp": {
            "servers": ["ntp.example"],
            "sync_interval": "300s",
            "max_offset_ms": 500,
            "failover": True,
        },
        "data_center": "N",
        "system_code": "JIUWEN",
        "node": "gw-1",
    }
    body.update(overrides)
    return body


@pytest.fixture(autouse=True)
def _reset_audit_manager():
    mem = MemoryEmitter()
    reset_audit_manager(AuditManager(emitter=mem))
    yield
    reset_audit_manager()


def test_apply_payload_sets_gateway_service() -> None:
    result = apply_audit_log_config_payload(_sample_body())
    assert result["ok"] is True
    assert result["service"] == "gateway"
    assert result["source"] == "db"
    assert is_audit_config_enabled() is True
    cfg = get_audit_manager().config
    assert cfg.service == "gateway"
    assert cfg.identity.system_code == "JIUWEN"
    assert cfg.otel.endpoint == "http://collector:4317"


def test_apply_payload_agentserver_service() -> None:
    result = apply_audit_log_config_payload(
        _sample_body(),
        service="agentserver",
    )
    assert result["service"] == "agentserver"
    assert get_audit_manager().config.service == "agentserver"


def test_apply_payload_rejects_invalid_otel() -> None:
    with pytest.raises(ValueError, match="otel.endpoint"):
        apply_audit_log_config_payload(
            _sample_body(otel={"enabled": True, "endpoint": "", "protocol": "grpc"})
        )


def test_apply_row_uses_body() -> None:
    apply_audit_log_config_row({"body": _sample_body(system_code="FROM-ROW")})
    assert get_audit_manager().config.identity.system_code == "FROM-ROW"


def test_apply_none_disables_emit_with_gateway_service() -> None:
    apply_audit_log_config_payload(_sample_body(system_code="TMP"))
    assert is_audit_config_enabled() is True
    result = apply_audit_log_config_payload(None)
    assert result["source"] == "disabled"
    assert is_audit_config_enabled() is False
    cfg = get_audit_manager().config
    assert cfg.service == "gateway"
    assert cfg.identity.system_code == "-"


@pytest.mark.asyncio
async def test_reload_from_db_prefers_repository(monkeypatch) -> None:
    store = InMemoryPersistentBackend()
    repo = EnterpriseRecordRepository(store, "audit_log_config", instance_id="")
    set_enterprise_record_repository("audit_log_config", repo)
    try:
        await repo.create(
            {
                "schema_version": "1.0.0",
                "otel_enabled": True,
                "otel_endpoint": "http://collector:4317",
                "otel_protocol": "grpc",
                "data_center": "N",
                "system_code": "COLD",
                "node": "n1",
                "ntp_servers": [],
                "ntp_sync_interval": "300s",
                "ntp_max_offset_ms": 500,
                "ntp_failover": True,
                "body": _sample_body(system_code="COLD"),
                "source": "manager",
                "revision": 1,
            }
        )
        monkeypatch.setattr("jiuwenswarm.edition.is_enterprise", lambda: True)
        result = await reload_audit_log_config_from_db()
        assert result["ok"] is True
        assert result["source"] == "db"
        assert is_audit_config_enabled() is True
        assert get_audit_manager().config.identity.system_code == "COLD"
        assert get_audit_manager().config.service == "gateway"
    finally:
        clear_enterprise_record_repositories()


@pytest.mark.asyncio
async def test_reload_from_empty_db_disables_emit(monkeypatch) -> None:
    store = InMemoryPersistentBackend()
    repo = EnterpriseRecordRepository(store, "audit_log_config", instance_id="")
    set_enterprise_record_repository("audit_log_config", repo)
    try:
        apply_audit_log_config_payload(_sample_body())
        assert is_audit_config_enabled() is True
        monkeypatch.setattr("jiuwenswarm.edition.is_enterprise", lambda: True)
        result = await reload_audit_log_config_from_db()
        assert result["ok"] is True
        assert result["source"] == "disabled"
        assert is_audit_config_enabled() is False
    finally:
        clear_enterprise_record_repositories()
