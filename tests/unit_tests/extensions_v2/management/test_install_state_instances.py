# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Instance/claim state: exclusivity and startup reconciliation."""

from __future__ import annotations

from pathlib import Path

from jiuwenswarm.server.runtime.marketplace.extension_install_state import (
    ActiveInstance,
    ExtensionBundleRecord,
    ExtensionInstallRecord,
    ExtensionInstallState,
)


def test_switching_claim_demotes_every_other_instance(tmp_path: Path) -> None:
    store = ExtensionInstallState.for_install_dir(tmp_path / "extension_pkgs")
    store.upsert(
        ExtensionInstallRecord(
            package_id="demo.pkg", version="1.0.0", origin="user",
            active=False, enabled=True, domain="agentserver", status="standby",
        )
    )
    store.upsert_bundle(
        ExtensionBundleRecord(
            bundle_id="demo.bundle",
            version="1.0.0",
            origin="user",
            members=(),
            member_records=(),
            status="disabled",
            active=True,
        )
    )

    store.switch_active_instance(
        ActiveInstance(package_id="demo.pkg", version="1.0.0", kind="standalone")
    )

    assert store.get("demo.pkg", "1.0.0").active is True
    assert store.active_instance("demo.pkg").kind == "standalone"
    store.disable_instance("demo.pkg", kind="standalone", version="1.0.0")
    assert store.get("demo.pkg", "1.0.0").enabled is False
    assert store.active_instance("demo.pkg") is None


def test_reconcile_claims_keeps_one_active_instance(tmp_path: Path) -> None:
    store = ExtensionInstallState.for_install_dir(tmp_path / "extension_pkgs")
    store.upsert(
        ExtensionInstallRecord(
            package_id="demo.pkg", version="1.0.0", origin="user",
            active=True, enabled=True, domain="agentserver", status="active",
        )
    )
    store.upsert(
        ExtensionInstallRecord(
            package_id="demo.pkg", version="2.0.0", origin="user",
            active=True, enabled=True, domain="agentserver", status="active",
        )
    )

    store.reconcile_claims()

    active = [record for record in store.packages() if record.active]
    assert len(active) == 1
    assert store.active_instance("demo.pkg") is not None


def test_bundle_status_update_clears_the_previous_error(tmp_path: Path) -> None:
    store = ExtensionInstallState.for_install_dir(tmp_path / "extension_pkgs")
    store.upsert_bundle(
        ExtensionBundleRecord("demo.bundle", "1.0.0", "user", status="failed", error="boom")
    )

    store.upsert_bundle(
        ExtensionBundleRecord("demo.bundle", "1.0.0", "user", status="active", error="")
    )

    record = store.get_bundle("demo.bundle", "1.0.0")
    assert record is not None and record.status == "active" and record.error == ""
