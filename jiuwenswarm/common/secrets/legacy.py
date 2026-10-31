"""Legacy sensitive-key rules (wraps local_env_config)."""

from __future__ import annotations

from jiuwenswarm.common.local_env_config import is_sensitive_env_name


def is_legacy_sensitive_key(name: str) -> bool:
    """Whether *name* triggers custom-crypto auto encrypt/decrypt (legacy rules)."""
    return is_sensitive_env_name(name)
