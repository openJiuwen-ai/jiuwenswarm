# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Configuration resolution for the local trajectory read store."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.utils import get_user_workspace_dir

DEFAULT_QUEUE_SIZE = 4096
DEFAULT_BATCH_SIZE = 64
DEFAULT_FLUSH_INTERVAL_MS = 200
DEFAULT_RETENTION_DAYS = 7
DEFAULT_POLL_INTERVAL_MS = 2000
DEFAULT_DETAIL_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_SESSION_DATABASE_DIRECTORY = "sessions"


@dataclass(frozen=True, slots=True)
class TrajectoryStoreSettings:
    """Resolved settings shared by the AgentServer writer and Gateway reader."""

    enabled: bool
    database_path: Path
    retention_days: int
    queue_size: int
    batch_size: int
    flush_interval_ms: int
    poll_interval_ms: int
    detail_max_bytes: int = DEFAULT_DETAIL_MAX_BYTES


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _positive_int(value: Any, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def _resolve_database_path(value: Any, workspace: Path) -> Path:
    raw_path = str(value or "").strip()
    if not raw_path:
        return workspace / ".trace" / DEFAULT_SESSION_DATABASE_DIRECTORY
    configured = Path(raw_path).expanduser()
    if configured.is_absolute():
        return configured
    return workspace / configured


def session_database_path(database_root: Path, session_id: str) -> Path:
    """Return the traversal-safe SQLite path owned by one session.

    Args:
        database_root: Root directory containing all session databases.
        session_id: Stable session identifier used only as hash input.

    Returns:
        A platform-independent path containing no user-controlled component.

    Raises:
        ValueError: If the session identifier is empty or padded.
    """
    normalized_session_id = str(session_id or "").strip()
    if not normalized_session_id or normalized_session_id != session_id:
        raise ValueError("session_id must be a non-empty normalized string")
    digest = hashlib.sha256(normalized_session_id.encode("utf-8")).hexdigest()
    return Path(database_root) / digest[:2] / f"{digest}.sqlite3"


def load_trajectory_store_settings(
    config: Mapping[str, Any] | None = None,
    *,
    workspace: Path | None = None,
) -> TrajectoryStoreSettings:
    """Resolve the ``trajectory_ui`` block without mutating user configuration.

    Args:
        config: Optional complete JiuwenSwarm configuration mapping.
        workspace: Optional data root override, primarily for isolated tests.

    Returns:
        Validated settings with conservative defaults from the data contract.
    """
    source = config if config is not None else get_config()
    raw_section = source.get("trajectory_ui", {}) if isinstance(source, Mapping) else {}
    section = raw_section if isinstance(raw_section, Mapping) else {}
    resolved_workspace = workspace if workspace is not None else get_user_workspace_dir()
    return TrajectoryStoreSettings(
        # The packaged config opts in explicitly. A caller supplying an older
        # config without this section keeps the additive data plane disabled.
        enabled=_as_bool(section.get("enabled"), False),
        database_path=_resolve_database_path(section.get("db_path"), resolved_workspace),
        retention_days=_positive_int(
            section.get("retention_days"),
            DEFAULT_RETENTION_DAYS,
        ),
        queue_size=_positive_int(section.get("queue_size"), DEFAULT_QUEUE_SIZE),
        batch_size=_positive_int(section.get("batch_size"), DEFAULT_BATCH_SIZE),
        flush_interval_ms=_positive_int(
            section.get("flush_interval_ms"),
            DEFAULT_FLUSH_INTERVAL_MS,
        ),
        poll_interval_ms=_positive_int(
            section.get("poll_interval_ms"),
            DEFAULT_POLL_INTERVAL_MS,
        ),
        detail_max_bytes=_positive_int(
            section.get("detail_max_bytes"),
            DEFAULT_DETAIL_MAX_BYTES,
            minimum=64 * 1024,
        ),
    )


__all__ = [
    "DEFAULT_DETAIL_MAX_BYTES",
    "DEFAULT_SESSION_DATABASE_DIRECTORY",
    "TrajectoryStoreSettings",
    "load_trajectory_store_settings",
    "session_database_path",
]
