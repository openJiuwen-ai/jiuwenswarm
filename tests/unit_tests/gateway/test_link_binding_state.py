# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from openjiuwen_runtime.foundation.db.sqlite_handler import SQLiteHandler

from jiuwenswarm.common.security.link_mtls import (
    MTLSDeploymentIdentity,
    LinkMTLSConfig,
    LinkMTLSError,
    LinkMTLSMode,
)
from jiuwenswarm.gateway.config.enterprise.link_binding_state import (
    LOCAL_SERVICE_ROLE,
    PROTOCOL_VERSION,
    sync_local_link_binding_state,
)
from jiuwenswarm.gateway.config.enterprise.tables.link_binding_state_models import (
    LINK_BINDING_STATE_TABLE,
    LINK_BINDING_STATE_TABLE_DEF,
)


class _FakeDb:
    def __init__(self, row: object | None = None) -> None:
        self.row = row
        self.created: tuple[str, dict[str, object]] | None = None
        self.updated: tuple[str, dict[str, object], dict[str, object]] | None = None

    async def get(self, table: str, filters: dict[str, object]) -> object | None:
        assert table == LINK_BINDING_STATE_TABLE
        assert filters == {"service_role": LOCAL_SERVICE_ROLE}
        return self.row

    async def create(self, table: str, data: dict[str, object]) -> object:
        self.created = (table, data)
        return SimpleNamespace(**data)

    async def update(
        self,
        table: str,
        filters: dict[str, object],
        data: dict[str, object],
    ) -> object:
        self.updated = (table, filters, data)
        return SimpleNamespace(**data)


def _config(tmp_path: Path) -> LinkMTLSConfig:
    from tests.fixtures.link_mtls import provision

    provision(
        tmp_path / "bundle",
        mtls_deployment_id="claw-1",
        endpoints={"gateway": "127.0.0.1:8775"},
    )
    ca = tmp_path / "bundle/gateway/ca.crt"
    cert = tmp_path / "bundle/gateway/tls.crt"
    key = tmp_path / "bundle/gateway/tls.key"
    return LinkMTLSConfig(
        mode=LinkMTLSMode.ENFORCE,
        ca_file=str(ca),
        cert_file=str(cert),
        key_file=str(key),
        identity=MTLSDeploymentIdentity("claw-1", "binding-1", 7),
    )


@pytest.mark.asyncio
async def test_sync_local_link_binding_state_creates_public_record(
    tmp_path: Path,
) -> None:
    db = _FakeDb()
    config = _config(tmp_path)

    await sync_local_link_binding_state(db, config)

    assert db.created is not None
    table, row = db.created
    assert table == LINK_BINDING_STATE_TABLE
    assert row["service_role"] == "gateway"
    assert row["mtls_deployment_id"] == "claw-1"
    assert row["mtls_binding_id"] == "binding-1"
    assert row["mtls_binding_epoch"] == 7
    assert row["protocol_version"] == PROTOCOL_VERSION
    assert row["local_cert_pem"] == Path(config.cert_file).read_text()
    assert row["peer_trust_bundle_pem"] == Path(config.ca_file).read_text()
    assert row["private_key_ref"] == config.key_file
    assert "PRIVATE KEY" not in str(row)


@pytest.mark.asyncio
async def test_sync_local_link_binding_state_updates_existing_record(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    db = _FakeDb(
        row=SimpleNamespace(
            status="active",
            mtls_deployment_id=config.identity.mtls_deployment_id,
            mtls_binding_id=config.identity.mtls_binding_id,
            mtls_binding_epoch=config.identity.mtls_binding_epoch,
            local_cert_fingerprint=config.cert_fingerprint(),
        )
    )

    await sync_local_link_binding_state(db, config)

    assert db.created is None
    assert db.updated is not None
    assert db.updated[1] == {
        "service_role": LOCAL_SERVICE_ROLE,
        "mtls_binding_epoch": 7,
        "status": "active",
    }
    assert db.updated[2]["mtls_binding_epoch"] == 7


@pytest.mark.asyncio
async def test_sync_local_link_binding_state_off_does_not_access_db() -> None:
    class _FailDb:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"unexpected DB access: {name}")

    result = await sync_local_link_binding_state(
        _FailDb(),
        LinkMTLSConfig(mode=LinkMTLSMode.OFF),
    )
    assert result is None


@pytest.mark.asyncio
async def test_link_binding_state_round_trip_with_real_sqlite(tmp_path: Path) -> None:
    handler = SQLiteHandler(str(tmp_path / "gateway.db"))
    await handler.init_database()
    await handler.connect()
    try:
        await handler.init_table(LINK_BINDING_STATE_TABLE_DEF)
        config = _config(tmp_path)
        await sync_local_link_binding_state(handler, config)
        row = await handler.get(
            LINK_BINDING_STATE_TABLE,
            {"service_role": LOCAL_SERVICE_ROLE},
        )
        assert row is not None
        assert row.mtls_deployment_id == "claw-1"
        assert row.mtls_binding_id == "binding-1"
        assert row.mtls_binding_epoch == 7
        assert row.local_cert_pem.startswith("-----BEGIN CERTIFICATE-----")
        assert row.private_key_ref.endswith("tls.key")
        from dataclasses import replace

        updated = replace(
            config, identity=MTLSDeploymentIdentity("claw-1", "binding-2", 8)
        )
        await sync_local_link_binding_state(handler, updated)
        with pytest.raises(LinkMTLSError, match="older than persisted"):
            await sync_local_link_binding_state(handler, config)
        row = await handler.get(
            LINK_BINDING_STATE_TABLE, {"service_role": LOCAL_SERVICE_ROLE}
        )
        assert row.mtls_binding_epoch == 8
        assert row.mtls_binding_id == "binding-2"
    finally:
        await handler.disconnect()
