# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway local HTTP/SSE certificate binding state, unrelated to Identity Center.

This application projection exposes only public certificate material and
private-key references.  The deployment-owned extension of the same table may
store the recoverable all-role bundle in ``certificate_materials``; application
ORM access deliberately omits that sensitive column.
"""

from __future__ import annotations

from openjiuwen_runtime.foundation.db.table_def import (
    ColumnDefinition,
    IndexDefinition,
    TableDefinition,
)

LINK_BINDING_STATE_TABLE = "link_binding_state"

LINK_BINDING_STATE_TABLE_DEF = TableDefinition(
    table_name=LINK_BINDING_STATE_TABLE,
    columns=[
        ColumnDefinition(
            "service_role", "string", length=32, primary_key=True, nullable=False
        ),
        ColumnDefinition("mtls_deployment_id", "string", length=64, nullable=False),
        ColumnDefinition("mtls_binding_id", "string", length=64, nullable=False),
        ColumnDefinition("protocol_version", "string", length=32, nullable=False),
        ColumnDefinition("mtls_binding_epoch", "integer", nullable=False),
        ColumnDefinition("local_cert_pem", "text", nullable=False),
        ColumnDefinition(
            "local_cert_fingerprint", "string", length=128, nullable=False
        ),
        ColumnDefinition("private_key_ref", "string", length=512, nullable=False),
        ColumnDefinition("peer_trust_bundle_pem", "text", nullable=False),
        ColumnDefinition(
            "status", "string", length=32, nullable=False, default="active"
        ),
        ColumnDefinition("created_at", "datetime", nullable=False),
        ColumnDefinition("updated_at", "datetime", nullable=False),
    ],
    indexes=[
        IndexDefinition(
            ["mtls_deployment_id"],
            unique=True,
            name="uq_link_binding_state_mtls_deployment_id",
        ),
        IndexDefinition(
            ["mtls_binding_id"], unique=True, name="uq_link_binding_state_binding_id"
        ),
        IndexDefinition(["status"], unique=False, name="ix_link_binding_state_status"),
    ],
)
