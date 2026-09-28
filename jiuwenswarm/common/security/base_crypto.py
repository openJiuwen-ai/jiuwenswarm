# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""加解密 Provider 契约与默认 Provider 存取。

CryptoProvider Protocol 以 ``gateway_protocol.sdk.crypto_utility`` 为
source of truth（转发别名同一对象）；set/get_crypto_provider 为本仓
运行期状态存取，保留实现。
"""

from __future__ import annotations

from typing import Optional

from gateway_protocol.sdk.crypto_utility import CryptoProvider  # noqa: F401

__all__ = ["CryptoProvider"]

_default_provider: Optional[CryptoProvider] = None


def set_crypto_provider(provider: CryptoProvider) -> None:
    global _default_provider
    _default_provider = provider


def get_crypto_provider() -> Optional[CryptoProvider]:
    return _default_provider
