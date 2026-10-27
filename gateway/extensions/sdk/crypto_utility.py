# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""
转发别名（过渡形态）：已迁 ``gateway_protocol.sdk.crypto_utility``。

CryptoProvider 协议随协议包内联（原 common/security/base_crypto.py 中的同名
Protocol 副本亦为同一对象，见 base_crypto.py 转发说明）。
"""

from __future__ import annotations

from gateway_protocol.sdk.crypto_utility import (  # noqa: F401
    CryptoProvider,
    CryptoUtility,
)

__all__ = ["CryptoProvider", "CryptoUtility"]
