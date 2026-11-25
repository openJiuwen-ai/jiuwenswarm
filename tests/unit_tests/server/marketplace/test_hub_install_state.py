# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Hub install ledger source_id persistence and backfill (I-05 / CMS-V-05/09)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jiuwenswarm.extensions.swarm_connector_hub.constants import OPENJIUWEN_HUB_SOURCE_ID
from jiuwenswarm.server.runtime.marketplace.hub_install_state import (
    HubInstallRecord,
    HubInstallStateStore,
    normalize_install_source_id,
)


def test_normalize_install_source_id_defaults_to_openjiuwen_hub() -> None:
    assert normalize_install_source_id(None) == OPENJIUWEN_HUB_SOURCE_ID
    assert normalize_install_source_id("") == OPENJIUWEN_HUB_SOURCE_ID
    assert normalize_install_source_id("  ") == OPENJIUWEN_HUB_SOURCE_ID
    assert normalize_install_source_id("corp-hub") == "corp-hub"


def test_from_dict_backfills_missing_source_id() -> None:
    record = HubInstallRecord.from_dict(
        {
            "asset_id": "asset-1",
            "kind": "mcp",
            "version": "1.0.0",
            "checksum_sha256": "abc",
            "installed_at": "2026-11-11T00:00:00Z",
            "package_id": "demo-mcp",
        }
    )
    assert record is not None
    assert record.source_id == OPENJIUWEN_HUB_SOURCE_ID


def test_from_dict_keeps_explicit_source_id() -> None:
    record = HubInstallRecord.from_dict(
        {
            "asset_id": "asset-1",
            "kind": "mcp",
            "version": "1.0.0",
            "checksum_sha256": "abc",
            "installed_at": "2026-11-11T00:00:00Z",
            "package_id": "demo-mcp",
            "source_id": "corp-hub",
        }
    )
    assert record is not None
    assert record.source_id == "corp-hub"


def test_store_roundtrip_persists_source_id(tmp_path: Path) -> None:
    store = HubInstallStateStore(tmp_path)
    store.upsert(
        HubInstallRecord(
            asset_id="asset-1",
            kind="mcp",
            version="1.0.0",
            checksum_sha256="abc",
            installed_at="2026-11-11T00:00:00Z",
            package_id="demo-mcp",
            source_id="corp-hub",
        )
    )
    payload = json.loads((tmp_path / "hub_state.json").read_text(encoding="utf-8"))
    assert payload["packages"][0]["source_id"] == "corp-hub"
    loaded = store.get("asset-1", source_id="corp-hub")
    assert loaded is not None
    assert loaded.source_id == "corp-hub"
    # Unique asset_id across sources still resolves without an explicit source_id.
    assert store.get("asset-1") == loaded


def test_ledger_keeps_cross_source_same_asset_id(tmp_path: Path) -> None:
    store = HubInstallStateStore(tmp_path)
    store.upsert(
        HubInstallRecord(
            asset_id="shared-asset",
            kind="mcp",
            version="1.0.0",
            checksum_sha256="aaa",
            installed_at="2026-11-11T00:00:00Z",
            package_id="pkg-a",
            source_id="openjiuwen-hub",
        )
    )
    store.upsert(
        HubInstallRecord(
            asset_id="shared-asset",
            kind="mcp",
            version="2.0.0",
            checksum_sha256="bbb",
            installed_at="2026-11-11T00:00:00Z",
            package_id="pkg-b",
            source_id="corp-hub",
        )
    )
    assert len(store.all()) == 2
    assert store.get("shared-asset", source_id="openjiuwen-hub").package_id == "pkg-a"
    assert store.get("shared-asset", source_id="corp-hub").package_id == "pkg-b"
    # Bare asset_id prefers the default Hub slot when present (personal-compat).
    assert store.get("shared-asset").package_id == "pkg-a"
    store.remove("shared-asset", source_id="corp-hub")
    assert store.get("shared-asset", source_id="corp-hub") is None
    assert store.get("shared-asset", source_id="openjiuwen-hub").package_id == "pkg-a"


def test_store_reads_legacy_ledger_without_source_id(tmp_path: Path) -> None:
    (tmp_path / "hub_state.json").write_text(
        json.dumps(
            {
                "packages": [
                    {
                        "asset_id": "legacy-asset",
                        "kind": "mcp",
                        "version": "1.2.0",
                        "checksum_sha256": "abc",
                        "installed_at": "2026-01-01T00:00:00Z",
                        "package_id": "legacy-mcp",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    store = HubInstallStateStore(tmp_path)
    record = store.get("legacy-asset")
    assert record is not None
    assert record.source_id == OPENJIUWEN_HUB_SOURCE_ID
    assert store.get_by_package_id("legacy-mcp") == record


def test_assert_package_id_available_rejects_cross_source(tmp_path: Path) -> None:
    store = HubInstallStateStore(tmp_path)
    store.upsert(
        HubInstallRecord(
            asset_id="asset-a",
            kind="mcp",
            version="1.0.0",
            checksum_sha256="abc",
            installed_at="2026-11-11T00:00:00Z",
            package_id="shared-mcp",
            source_id="openjiuwen-hub",
        )
    )
    with pytest.raises(ValueError, match="already installed from source"):
        store.assert_package_id_available(
            "shared-mcp",
            asset_id="asset-b",
            source_id="corp-hub",
            kind="mcp",
        )


def test_assert_package_id_available_rejects_other_asset_same_source(
    tmp_path: Path,
) -> None:
    store = HubInstallStateStore(tmp_path)
    store.upsert(
        HubInstallRecord(
            asset_id="asset-a",
            kind="mcp",
            version="1.0.0",
            checksum_sha256="abc",
            installed_at="2026-11-11T00:00:00Z",
            package_id="shared-mcp",
            source_id="openjiuwen-hub",
        )
    )
    with pytest.raises(ValueError, match="owned by another Hub asset"):
        store.assert_package_id_available(
            "shared-mcp",
            asset_id="asset-b",
            source_id="openjiuwen-hub",
            kind="mcp",
        )


def test_assert_package_id_available_allows_same_asset_reinstall_check(
    tmp_path: Path,
) -> None:
    store = HubInstallStateStore(tmp_path)
    store.upsert(
        HubInstallRecord(
            asset_id="asset-a",
            kind="mcp",
            version="1.0.0",
            checksum_sha256="abc",
            installed_at="2026-11-11T00:00:00Z",
            package_id="shared-mcp",
            source_id="corp-hub",
        )
    )
    store.assert_package_id_available(
        "shared-mcp",
        asset_id="asset-a",
        source_id="corp-hub",
        kind="mcp",
    )
