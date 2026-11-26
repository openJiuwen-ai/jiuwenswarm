# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Keep the Gateway copy of the shared install state in step with AgentServer."""

from __future__ import annotations

import inspect
from dataclasses import fields

import pytest

from agent_gateway.extensions_v2.management import containers as gateway_containers
from agent_gateway.extensions_v2.management.install_state import (
    ExtensionBundleMember as GatewayBundleMember,
)
from agent_gateway.extensions_v2.management.install_state import (
    ExtensionBundleRecord as GatewayBundleRecord,
)
from agent_gateway.extensions_v2.management.install_state import (
    ExtensionInstallRecord as GatewayInstallRecord,
)
from agent_gateway.extensions_v2.management.install_state import (
    ExtensionInstallState as GatewayInstallState,
)
from jiuwenswarm.extensions_v2.management import containers as server_containers
from jiuwenswarm.server.runtime.marketplace.extension_install_state import (
    ExtensionBundleMember,
    ExtensionBundleRecord,
    ExtensionInstallRecord,
    ExtensionInstallState,
)

_PUBLIC_METHODS = (
    "get",
    "packages",
    "by_asset",
    "upsert",
    "upsert_many",
    "activate",
    "mark",
    "remove",
    "bundles",
    "get_bundle",
    "active_bundle",
    "activate_bundle",
    "deactivate_bundle",
    "bundle_members",
    "get_bundle_member",
    "upsert_bundle_member",
    "mark_bundle_member",
    "remove_bundle_member",
    "remove_bundle_members",
    "active_instance",
    "reconcile_claims",
    "switch_active_instance",
    "disable_instance",
    "bundle_of",
    "upsert_bundle",
    "remove_bundle",
)


@pytest.mark.parametrize(
    ("local", "shared"),
    [
        (ExtensionInstallRecord, GatewayInstallRecord),
        (ExtensionBundleMember, GatewayBundleMember),
        (ExtensionBundleRecord, GatewayBundleRecord),
        (ExtensionInstallState, GatewayInstallState),
    ],
)
def test_shared_install_state_types_match(local, shared) -> None:
    if inspect.isclass(local) and hasattr(local, "__dataclass_fields__"):
        assert [field.name for field in fields(local)] == [field.name for field in fields(shared)]
    else:
        for name in _PUBLIC_METHODS:
            assert inspect.signature(getattr(local, name)) == inspect.signature(getattr(shared, name))


def test_active_bundle_round_trips_in_both_stores(tmp_path) -> None:
    for index, (bundle_type, store_type) in enumerate(
        (
            (ExtensionBundleRecord, ExtensionInstallState),
            (GatewayBundleRecord, GatewayInstallState),
        )
    ):
        store = store_type.for_install_dir(tmp_path / str(index))
        store.upsert_bundle(bundle_type("demo.bundle", "1.0.0", "user", active=True))
        store.upsert_bundle(bundle_type("demo.bundle", "1.0.1", "user", active=True))

        store.activate_bundle("demo.bundle", "1.0.1")

        assert store.active_bundle("demo.bundle").version == "1.0.1"
        assert store.get_bundle("demo.bundle", "1.0.0").active is False
        assert store.get_bundle("demo.bundle", "1.0.1").active is True


def test_bundle_status_vocabulary_matches_across_hosts(tmp_path) -> None:
    member = GatewayBundleMember("demo.gw", "1.0.0", "gateway")
    record = GatewayBundleRecord("demo.bundle", "1.0.0", "user", members=(member,))
    local_member = ExtensionBundleMember("demo.gw", "1.0.0", "gateway")
    local_record = ExtensionBundleRecord("demo.bundle", "1.0.0", "user", members=(local_member,))

    for status, active, enabled in (
        ("active", True, True),
        ("pending", False, True),
        ("failed", False, False),
        ("active", False, False),
    ):
        gateway_store = GatewayInstallState.for_install_dir(tmp_path / status / f"gw-{active}-{enabled}")
        gateway_store.upsert(
            GatewayInstallRecord(
                package_id="demo.gw",
                version="1.0.0",
                origin="user",
                active=active,
                enabled=enabled,
                domain="gateway",
                status=status,
            )
        )
        local_store = ExtensionInstallState.for_install_dir(tmp_path / status / f"as-{active}-{enabled}")
        local_store.upsert(
            ExtensionInstallRecord(
                package_id="demo.gw",
                version="1.0.0",
                origin="user",
                active=active,
                enabled=enabled,
                domain="gateway",
                status=status,
            )
        )

        assert gateway_containers.frontend_status(gateway_store, record) == server_containers.bundle_overall_status(
            local_record, local_store
        )
