# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Configuration for the secret-shield rail.

Every gate defaults to ``observe`` so mounting the rail never changes
behaviour until an operator opts into ``enforce``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

SECTION_NAME = "secret_shield"
VALID_MODES = frozenset({"off", "observe", "enforce"})
DEFAULT_MODE = "observe"


def _mode(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in VALID_MODES else DEFAULT_MODE


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


@dataclass(frozen=True, slots=True)
class SecretShieldConfig:
    """Rail settings read from the top-level ``secret_shield`` section."""

    enabled: bool = True
    tool_result_mode: str = DEFAULT_MODE
    exec_gate_mode: str = DEFAULT_MODE
    output_mode: str = DEFAULT_MODE
    vault_max_entries: int = 64


def load_secret_shield_config(raw: Mapping[str, object] | None) -> SecretShieldConfig:
    """Build a config from the ``secret_shield`` section, tolerating bad values."""
    if not isinstance(raw, Mapping):
        return SecretShieldConfig()
    return SecretShieldConfig(
        enabled=raw.get("enabled", True) is not False,
        tool_result_mode=_mode(raw.get("tool_result_mode")),
        exec_gate_mode=_mode(raw.get("exec_gate_mode")),
        output_mode=_mode(raw.get("output_mode")),
        vault_max_entries=_positive_int(raw.get("vault_max_entries"), 64),
    )


__all__ = [
    "DEFAULT_MODE",
    "SECTION_NAME",
    "SecretShieldConfig",
    "VALID_MODES",
    "load_secret_shield_config",
]
