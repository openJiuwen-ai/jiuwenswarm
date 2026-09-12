# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persist the Gateway's effective HTTP/SSE certificate binding state."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openjiuwen_runtime.foundation.security.link_material_store import (
    assert_binding_state_compatible,
    assert_managed_state,
)

from jiuwenswarm.common.security.link_mtls import (
    LinkMTLSConfig,
    LinkMTLSError,
    LinkMTLSMode,
)
from jiuwenswarm.gateway.config.enterprise.tables.link_binding_state_models import (
    LINK_BINDING_STATE_TABLE,
)

LOCAL_SERVICE_ROLE = "gateway"
PROTOCOL_VERSION = "0.0.1"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def sync_local_link_binding_state(db: Any, config: LinkMTLSConfig) -> Any | None:
    """Idempotently persist the active public identity; ``off`` writes nothing."""
    if config.mode is LinkMTLSMode.OFF:
        return None
    if not config.identity or not config.cert_file or not config.ca_file:
        # Observe mode may intentionally run an incomplete material preflight.
        return None

    now = _utc_now()
    payload = {
        "service_role": "gateway",
        "mtls_deployment_id": config.identity.mtls_deployment_id,
        "mtls_binding_id": config.identity.mtls_binding_id,
        "protocol_version": PROTOCOL_VERSION,
        "mtls_binding_epoch": config.identity.mtls_binding_epoch,
        "local_cert_pem": Path(config.cert_file).read_text(encoding="utf-8"),
        "local_cert_fingerprint": config.cert_fingerprint(),
        "private_key_ref": config.key_file or "",
        "peer_trust_bundle_pem": Path(config.ca_file).read_text(encoding="utf-8"),
        "status": "active",
        "updated_at": now,
    }
    row = await db.get(LINK_BINDING_STATE_TABLE, {"service_role": LOCAL_SERVICE_ROLE})
    managed = await assert_managed_state(
        db,
        LOCAL_SERVICE_ROLE,
        config.identity,
        payload["local_cert_fingerprint"],
        required=bool(
            config.profile and config.profile.current().get("persistence") == "database"
        ),
    )
    if managed:
        return (
            row  # Deployment owns the authorized state; service startup is read-only.
        )
    assert_binding_state_compatible(
        row, config.identity, payload["local_cert_fingerprint"]
    )
    if row is None:
        return await db.create(
            LINK_BINDING_STATE_TABLE,
            {"created_at": now, **payload},
        )
    previous_epoch = int(getattr(row, "mtls_binding_epoch", 0) or 0)
    filters = {"service_role": LOCAL_SERVICE_ROLE}
    if hasattr(row, "mtls_binding_epoch"):
        filters["mtls_binding_epoch"] = previous_epoch
        filters["status"] = getattr(row, "status", "active")
    updated = await db.update(LINK_BINDING_STATE_TABLE, filters, payload)
    # DBHandler.update re-reads with the OLD filters, so an epoch change may
    # return None even after a successful conditional update.
    if hasattr(row, "mtls_binding_epoch"):
        updated = await db.get(
            LINK_BINDING_STATE_TABLE, {"service_role": LOCAL_SERVICE_ROLE}
        )
        checked_fields = (
            "mtls_binding_id",
            "mtls_binding_epoch",
            "status",
            "local_cert_fingerprint",
        )
        if updated is None or any(
            getattr(updated, key, None) != payload.get(key) for key in checked_fields
        ):
            raise LinkMTLSError(
                "link binding state changed concurrently; restart with the current profile"
            )
    return updated
