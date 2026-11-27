from __future__ import annotations

import os
from dataclasses import dataclass

_QUEUE_TIMEOUT_SECONDS = 60.0
_IDLE_CHECK_INTERVAL_SECONDS = 30.0
_DEFAULT_IDLE_TIMEOUT_SECONDS = 600.0
_DEFAULT_QUEUE_MAX_SIZE = 100
_DEFAULT_LINK_HEARTBEAT_TIMEOUT_SECONDS = 15.0
_DEFAULT_LINK_HEARTBEAT_CHECK_INTERVAL_SECONDS = 5.0


def sandbox_routing_enabled() -> bool:
    return _env_bool("SANDBOX_ENABLE", default=False)


@dataclass(frozen=True)
class SandboxRoutingSettings:
    max_sandboxes: int
    queue_max_size: int
    queue_timeout_seconds: float = _QUEUE_TIMEOUT_SECONDS
    idle_timeout_seconds: float = _DEFAULT_IDLE_TIMEOUT_SECONDS
    idle_check_interval_seconds: float = _IDLE_CHECK_INTERVAL_SECONDS
    link_heartbeat_enabled: bool = True
    link_heartbeat_timeout_seconds: float = _DEFAULT_LINK_HEARTBEAT_TIMEOUT_SECONDS
    link_heartbeat_check_interval_seconds: float = _DEFAULT_LINK_HEARTBEAT_CHECK_INTERVAL_SECONDS

    @classmethod
    def from_env(cls) -> SandboxRoutingSettings:
        max_sandboxes = _env_optional_positive_int("SANDBOX_MAX_NUM")
        queue_max_size = _env_int("SANDBOX_MAX_QUEUE_SIZE", default=_DEFAULT_QUEUE_MAX_SIZE)
        idle_timeout_seconds = _env_float(
            "SANDBOX_IDLE_TIMEOUT_SECONDS",
            default=_DEFAULT_IDLE_TIMEOUT_SECONDS,
        )
        link_heartbeat_timeout_seconds = _env_float(
            "SANDBOX_LINK_HEARTBEAT_TIMEOUT_SECONDS",
            default=_DEFAULT_LINK_HEARTBEAT_TIMEOUT_SECONDS,
        )
        link_heartbeat_check_interval_seconds = _env_float(
            "SANDBOX_LINK_HEARTBEAT_CHECK_INTERVAL_SECONDS",
            default=_DEFAULT_LINK_HEARTBEAT_CHECK_INTERVAL_SECONDS,
        )
        return cls(
            max_sandboxes=max_sandboxes,
            queue_max_size=max(1, queue_max_size),
            queue_timeout_seconds=_QUEUE_TIMEOUT_SECONDS,
            idle_timeout_seconds=max(0.0, idle_timeout_seconds),
            idle_check_interval_seconds=_IDLE_CHECK_INTERVAL_SECONDS,
            link_heartbeat_enabled=_env_bool("SANDBOX_LINK_HEARTBEAT_ENABLED", default=True),
            link_heartbeat_timeout_seconds=max(1.0, link_heartbeat_timeout_seconds),
            link_heartbeat_check_interval_seconds=max(
                1.0,
                link_heartbeat_check_interval_seconds,
            ),
        )


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, *, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return int(str(raw).strip())


def _env_optional_positive_int(name: str) -> int:
    """Return 0 when unset (unlimited). Raise if explicitly set to a non-positive value."""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return 0
    value = int(str(raw).strip())
    if value <= 0:
        raise ValueError(f"{name} must be greater than 0 when set, got {value}")
    return value


def _env_float(name: str, *, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return float(str(raw).strip())
