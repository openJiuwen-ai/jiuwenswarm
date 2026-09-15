# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Version marker for the one-shot Process CLI machine schema."""

from __future__ import annotations


CURRENT_SCHEMA_VERSION = "0.1"
SUPPORTED_SCHEMA_VERSIONS = (CURRENT_SCHEMA_VERSION,)


def is_schema_version_supported(version: object) -> bool:
    """Return whether *version* names an exactly implemented machine schema."""

    return isinstance(version, str) and version in SUPPORTED_SCHEMA_VERSIONS


def require_supported_schema_version(version: object) -> str:
    """Validate one schema version without negotiating a resident connection."""

    if not isinstance(version, str):
        raise TypeError("schema_version must be a string")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError(f"unsupported schema_version: {version}")
    return version


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "is_schema_version_supported",
    "require_supported_schema_version",
]
