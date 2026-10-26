# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Pure TLS configuration for the isolated DeepResearch child runtime.

The host process never installs these values into :mod:`os.environ`.  Callers
merge the returned JSON-safe ``tls`` mapping into the versioned stdin frame;
the short-lived runner or SDK bridge applies it before importing the SDK.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_TLS_KEYS = ("LLM_SSL_VERIFY", "TOOL_SSL_VERIFY")
_TLS_KEY_SET = frozenset(_TLS_KEYS)
_INVALID_TLS = "deepresearch_tls_invalid"


def _normalize_boolean(name: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    raise ValueError(f"{name} must be a boolean or true/false string")


def normalize_child_tls_config(
    config: Mapping[str, Any] | None,
) -> dict[str, bool]:
    """Validate and normalize the two supported child-runtime TLS options."""
    if config is None:
        values: Mapping[str, Any] = {}
    elif not isinstance(config, Mapping):
        raise ValueError(_INVALID_TLS)
    else:
        values = config
    for key in values:
        if not isinstance(key, str) or key not in _TLS_KEY_SET:
            raise ValueError(_INVALID_TLS)
    return {
        name: _normalize_boolean(name, values.get(name, False))
        for name in _TLS_KEYS
    }


def build_child_tls_config_frame(
    config: Mapping[str, Any] | None,
) -> dict[str, dict[str, bool]]:
    """Return the TLS fragment to merge into a versioned child stdin frame."""
    return {"tls": normalize_child_tls_config(config)}
