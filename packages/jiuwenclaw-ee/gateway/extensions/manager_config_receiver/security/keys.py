# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""Gateway 侧密钥落库与生命周期。

- 加密密钥对：单例持久化于 ``gateway_enc_keypair``，私钥永不外发；首启生成。
- 签名密钥对：单例持久化于 ``gateway_sign_keypair``，握手向对端出示 link-auth 令牌；首启生成。
- Manager 签名公钥：握手分发，存 ``manager_sign_pubkey``（单例，id="default"）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from openjiuwen_runtime.foundation.security import link_auth

from jiuwenswarm.gateway.config.enterprise.tables.key_models import (
    GATEWAY_ENC_KEYPAIR_TABLE_DEF,
    GATEWAY_SIGN_KEYPAIR_TABLE_DEF,
    MANAGER_SIGN_PUBKEY_TABLE_DEF,
)
from ..infrastructure.repository_access import require_enterprise_repository
from ..infrastructure.utils import utc_now
from . import crypto_primitives as cp

logger = logging.getLogger(__name__)

_KEYPAIR_TABLE = GATEWAY_ENC_KEYPAIR_TABLE_DEF.table_name
_SIGN_KEYPAIR_TABLE = GATEWAY_SIGN_KEYPAIR_TABLE_DEF.table_name
_SIGN_PUBKEY_TABLE = MANAGER_SIGN_PUBKEY_TABLE_DEF.table_name
_KEYPAIR_ID = "default"
ENC_ALG = "X25519"
SIGN_ALG = "Ed25519"


@dataclass(frozen=True)
class GatewayEncKeypair:
    private_raw: bytes
    public_raw: bytes
    fingerprint: str


@dataclass(frozen=True)
class GatewaySignKeypair:
    """Gateway link-auth 签名密钥对（base64 字符串，可直接喂给 link_auth.build_token）。"""

    private_b64: str
    public_b64: str
    fingerprint: str


@dataclass(frozen=True)
class ManagerSignPublicKey:
    public_raw: bytes
    key_version: str
    fingerprint: str


async def get_or_create_gateway_enc_keypair() -> GatewayEncKeypair:
    """加载 Gateway 加密密钥对；不存在则生成并持久化（单例，幂等）。"""
    repo = require_enterprise_repository(_KEYPAIR_TABLE)
    row = await repo.get({"id": _KEYPAIR_ID})
    if row and row.get("private_key") and row.get("public_key"):
        priv = cp.b64d(row["private_key"])
        pub = cp.b64d(row["public_key"])
        return GatewayEncKeypair(priv, pub, str(row.get("fingerprint") or cp.fingerprint(pub)))

    priv, pub = cp.x25519_generate()
    fp = cp.fingerprint(pub)
    now = utc_now()
    await repo.create(
        {
            "id": _KEYPAIR_ID,
            "enc_alg": ENC_ALG,
            "private_key": cp.b64e(priv),
            "public_key": cp.b64e(pub),
            "fingerprint": fp,
            "data": None,
            "created_at": now,
            "updated_at": now,
        },
    )
    logger.info("[keys] generated gateway enc keypair fp=%s", fp[:16])
    return GatewayEncKeypair(priv, pub, fp)


async def get_or_create_gateway_sign_keypair() -> GatewaySignKeypair:
    """加载 Gateway link-auth 签名密钥对；不存在则生成并持久化（单例，幂等）。

    直接存/读 link_auth 给的 base64 字符串，正好喂给 ``build_token``/``build_token_header``。
    """
    repo = require_enterprise_repository(_SIGN_KEYPAIR_TABLE)
    row = await repo.get({"id": _KEYPAIR_ID})
    if row and row.get("private_key") and row.get("public_key"):
        priv_b64 = str(row["private_key"])
        pub_b64 = str(row["public_key"])
        fp = str(row.get("fingerprint") or link_auth.fingerprint(pub_b64))
        return GatewaySignKeypair(priv_b64, pub_b64, fp)

    priv_b64, pub_b64 = link_auth.generate_keypair()
    fp = link_auth.fingerprint(pub_b64)
    now = utc_now()
    await repo.create(
        {
            "id": _KEYPAIR_ID,
            "sign_alg": SIGN_ALG,
            "private_key": priv_b64,
            "public_key": pub_b64,
            "fingerprint": fp,
            "data": None,
            "created_at": now,
            "updated_at": now,
        },
    )
    logger.info("[keys] generated gateway sign keypair fp=%s", fp[:16])
    return GatewaySignKeypair(priv_b64, pub_b64, fp)


async def load_gateway_enc_privkey_by_fp(fingerprint: str | None) -> bytes | None:
    """按指纹返回本机加密私钥；当前为单例，指纹不匹配则返回 None。"""
    kp = await get_or_create_gateway_enc_keypair()
    if fingerprint and fingerprint != kp.fingerprint:
        return None
    return kp.private_raw


async def store_manager_sign_pubkey(
    public_key_b64: str,
    *,
    key_version: str,
    manager_id: str = "default",
    sign_alg: str = "Ed25519",
    fingerprint: str | None = None,
) -> None:
    """落库/更新 Manager 签名公钥（单例，id="default"；用于 config.push 验签）。"""
    repo = require_enterprise_repository(_SIGN_PUBKEY_TABLE)
    pub = cp.b64d(public_key_b64)
    fp = fingerprint or cp.fingerprint(pub)
    now = utc_now()
    existing = await repo.get({"id": _KEYPAIR_ID})
    data: dict[str, Any] = {
        "manager_id": manager_id,
        "sign_alg": sign_alg,
        "public_key": public_key_b64,
        "key_version": key_version,
        "fingerprint": fp,
        "status": "bound",
        "updated_at": now,
    }
    if existing is not None:
        await repo.update({"id": _KEYPAIR_ID}, data)
    else:
        await repo.create(
            {
                "id": _KEYPAIR_ID,
                "bound_at": now,
                "data": None,
                "created_at": now,
                **data,
            },
        )
    logger.info(
        "[keys] bound manager sign pubkey version=%s fp=%s",
        key_version,
        fp[:16],
    )


async def load_manager_sign_pubkey(
    key_version: str | None = None,
) -> ManagerSignPublicKey | None:
    """读取 Manager 签名公钥（单例）；指定版本时需匹配，否则返回 None。"""
    repo = require_enterprise_repository(_SIGN_PUBKEY_TABLE)
    row = await repo.get({"id": _KEYPAIR_ID})
    if not row or not row.get("public_key") or str(row.get("status")) != "bound":
        return None
    stored_version = str(row.get("key_version") or "")
    if key_version and key_version != stored_version:
        return None
    pub = cp.b64d(row["public_key"])
    return ManagerSignPublicKey(pub, stored_version, str(row.get("fingerprint") or cp.fingerprint(pub)))


async def delete_manager_sign_pubkey() -> None:
    """清除本机 Manager 签名公钥（单例）。"""
    repo = require_enterprise_repository(_SIGN_PUBKEY_TABLE)
    row = await repo.get({"id": _KEYPAIR_ID})
    if row is None:
        return
    await repo.delete({"id": _KEYPAIR_ID})
