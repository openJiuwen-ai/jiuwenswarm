# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""OHOS compat stub: pymilvus.client.utils."""
from __future__ import annotations

_GRPC_OK_CODES = frozenset({0})


def is_successful(rpc_code) -> bool:  # type: ignore[no-untyped-def]
    """Mirror pymilvus semantics: grpc status code 0 == success."""
    try:
        return int(rpc_code) in _GRPC_OK_CODES
    except Exception:
        return False
