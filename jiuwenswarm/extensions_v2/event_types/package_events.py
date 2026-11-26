# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentServer host callbacks for agent-core's package lifecycle events.

The event names and payloads live in agent-core. This module only records the
container status after a member finishes and leaves member loading to the
agent-core Registry (which already listens to ``package.install``).
"""

from __future__ import annotations

import asyncio

from openjiuwen.core.events import (
    ExtensionEventBus,
    HostPackageInstallRequest,
    PackageInstallOutcome,
    register_host_package_lifecycle,
)


def register_package_lifecycle_events(bus: ExtensionEventBus) -> None:
    """Register the AgentServer outcome callback with core."""

    async def install_member(request: HostPackageInstallRequest):
        # AgentServer loads members through agent-core's Registry and its
        # ``InstallRequest`` path, so a host-level install request is not used.
        del request
        return None

    async def outcome_received(outcome: PackageInstallOutcome) -> None:
        from jiuwenswarm.extensions_v2.config import resolve_install_dir
        from jiuwenswarm.extensions_v2.management import containers
        from jiuwenswarm.server.runtime.marketplace.extension_install_state import ExtensionInstallState

        store = ExtensionInstallState.for_install_dir(resolve_install_dir())
        container_id = outcome.container_id
        container_version = outcome.container_version
        if not container_id:
            record = store.bundle_of(outcome.package_id)
            if record is None:
                return None
            container_id, container_version = record.bundle_id, record.version
        await asyncio.to_thread(containers.refresh_container_status, store, container_id, container_version)
        return None

    register_host_package_lifecycle(
        bus,
        install_handler=install_member,
        outcome_handler=outcome_received,
    )
