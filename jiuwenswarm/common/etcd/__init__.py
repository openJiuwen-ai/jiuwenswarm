# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Minimal etcd v3 client, shared by gateway components.

Pure infrastructure: stdlib + ``httpx`` only, no business dependencies.
"""

from __future__ import annotations

from jiuwenswarm.common.etcd.client import (
    EtcdCasError,
    EtcdError,
    EtcdJsonClient,
    EtcdKv,
    EtcdRangeResult,
    prefix_range_end,
)

__all__ = [
    "EtcdCasError",
    "EtcdError",
    "EtcdJsonClient",
    "EtcdKv",
    "EtcdRangeResult",
    "prefix_range_end",
]
