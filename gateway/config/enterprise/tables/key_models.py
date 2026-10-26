# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""Gateway 侧配置下发密钥表（**新增独立表，不改动现有表**）。

- ``gateway_enc_keypair``：Gateway 自身 X25519 加密密钥对（单例，私钥本地受保护）。
- ``gateway_sign_keypair``：Gateway 自身 Ed25519 link-auth 签名密钥对（单例，握手出示令牌）。
- ``manager_sign_pubkey``：握手分发的 Manager Ed25519 签名公钥（单例，id="default"）。
"""

from __future__ import annotations

from openjiuwen_runtime.foundation.db.table_def import (
    ColumnDefinition,
    IndexDefinition,
    TableDefinition,
)

# Gateway 加密密钥对（单例，固定主键 id="default"）。
GATEWAY_ENC_KEYPAIR_TABLE_DEF = TableDefinition(
    table_name="gateway_enc_keypair",
    columns=[
        ColumnDefinition("id", "string", length=32, primary_key=True, nullable=False),
        ColumnDefinition("enc_alg", "string", length=32, nullable=False),
        ColumnDefinition("private_key", "string", length=512, nullable=False),
        ColumnDefinition("public_key", "string", length=256, nullable=False),
        ColumnDefinition("fingerprint", "string", length=128, nullable=False),
        ColumnDefinition("data", "json", nullable=True),
        ColumnDefinition("created_at", "datetime", nullable=False),
        ColumnDefinition("updated_at", "datetime", nullable=False),
    ],
    indexes=[],
)

# Gateway link-auth 签名密钥对（Ed25519，单例，固定主键 id="default"）。
GATEWAY_SIGN_KEYPAIR_TABLE_DEF = TableDefinition(
    table_name="gateway_sign_keypair",
    columns=[
        ColumnDefinition("id", "string", length=32, primary_key=True, nullable=False),
        ColumnDefinition("sign_alg", "string", length=32, nullable=False),
        ColumnDefinition("private_key", "string", length=512, nullable=False),
        ColumnDefinition("public_key", "string", length=256, nullable=False),
        ColumnDefinition("fingerprint", "string", length=128, nullable=False),
        ColumnDefinition("data", "json", nullable=True),
        ColumnDefinition("created_at", "datetime", nullable=False),
        ColumnDefinition("updated_at", "datetime", nullable=False),
    ],
    indexes=[],
)

# Manager 签名公钥（握手分发），单例，固定主键 id="default"。
MANAGER_SIGN_PUBKEY_TABLE_DEF = TableDefinition(
    table_name="manager_sign_pubkey",
    columns=[
        ColumnDefinition("id", "string", length=32, primary_key=True, nullable=False),
        ColumnDefinition("manager_id", "string", length=64, nullable=False, default="default"),
        ColumnDefinition("sign_alg", "string", length=32, nullable=False),
        ColumnDefinition("public_key", "string", length=256, nullable=False),
        ColumnDefinition("key_version", "string", length=32, nullable=False),
        ColumnDefinition("fingerprint", "string", length=128, nullable=False),
        ColumnDefinition("status", "string", length=32, nullable=False, default="bound"),
        ColumnDefinition("bound_at", "datetime", nullable=False),
        ColumnDefinition("data", "json", nullable=True),
        ColumnDefinition("created_at", "datetime", nullable=False),
        ColumnDefinition("updated_at", "datetime", nullable=False),
    ],
    indexes=[
        IndexDefinition(["status"], unique=False),
    ],
)
