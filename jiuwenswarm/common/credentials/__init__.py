# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Credential reference resolution (config SecretRef, exec: source).

Kept free of config/utils imports: dependency is strictly one-directional
``common.config -> common.credentials``. Registering nothing else on
package import keeps the module cheap and cycle-free.
"""

from jiuwenswarm.common.credentials.registry import is_credential_field
from jiuwenswarm.common.credentials.resolver import (
    EXEC_PREFIX,
    is_reference,
    prewarm_from_config,
    resolve_credential_ref,
)

__all__ = [
    "EXEC_PREFIX",
    "is_credential_field",
    "is_reference",
    "prewarm_from_config",
    "resolve_credential_ref",
]
