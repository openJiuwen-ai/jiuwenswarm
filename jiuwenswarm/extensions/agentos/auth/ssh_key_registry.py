# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""兼容别名：已归 ``jiuwenswarm.gateway.extensions.auth(ssh_key_registry)``。"""

from jiuwenswarm.gateway.extensions.auth.ssh_key_registry import (  # noqa: F401
    KeyRegistryEntry,
    KeyRegistry,
)

__all__ = [
    "KeyRegistryEntry",
    "KeyRegistry",
]
