# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Configuration resolution for the local trajectory read store."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.utils import get_user_workspace_dir

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_SIZE = 4096
DEFAULT_BATCH_SIZE = 64
DEFAULT_FLUSH_INTERVAL_MS = 200
DEFAULT_RETENTION_DAYS = 7
DEFAULT_POLL_INTERVAL_MS = 2000
DEFAULT_DETAIL_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_SESSION_DATABASE_DIRECTORY = "sessions"

DEFAULT_DIAGNOSIS_KEEP_DAYS = 7
DEFAULT_DIAGNOSIS_UPLOAD_MB = 50
DEFAULT_DIAGNOSIS_HISTORY_RECORDS = 20


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


@dataclass(frozen=True, slots=True)
class DiagnosisSettings:
    """Resolved settings for the trace auto-diagnosis feature (设计 §4.6)."""

    enabled: bool
    include_logs: bool
    history_summary_records: int
    keep_evidence_days: int
    allow_log_upload: bool
    max_upload_log_mb: int


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


def load_diagnosis_settings(
    config: Mapping[str, Any] | None = None,
) -> DiagnosisSettings:
    """Resolve the ``diagnosis`` block.

    离线模式（offline=true）纯日志诊断不依赖 trajectory store；在线模式由
    诊断端点按 trace 查询结果自行 404。故此处不强制 trajectory_ui.enabled，
    仅当 trajectory_ui 未开时记录提示（在线诊断会无 trace 可分析）。
    """
    source = config if config is not None else get_config()
    raw_section = source.get("diagnosis", {}) if isinstance(source, Mapping) else {}
    section = raw_section if isinstance(raw_section, Mapping) else {}

    enabled = _as_bool(section.get("enabled"), False)
    if enabled:
        trajectory = source.get("trajectory_ui", {}) if isinstance(source, Mapping) else {}
        trajectory_enabled = _as_bool(
            trajectory.get("enabled") if isinstance(trajectory, Mapping) else None,
            False,
        )
        if not trajectory_enabled:
            # 离线纯日志诊断不依赖 trajectory store，不阻断启动；
            # 在线诊断端点查 trace 时会自行 404，前端引导用户走离线模式。
            logger.info(
                "diagnosis.enabled=true but trajectory_ui.enabled=false; "
                "在线诊断无 trace 可分析，仅离线模式（纯日志）可用"
            )

    return DiagnosisSettings(
        enabled=enabled,
        include_logs=_as_bool(section.get("include_logs"), True),
        history_summary_records=_positive_int(
            section.get("history_summary_records"),
            DEFAULT_DIAGNOSIS_HISTORY_RECORDS,
        ),
        keep_evidence_days=_positive_int(
            section.get("keep_evidence_days"),
            DEFAULT_DIAGNOSIS_KEEP_DAYS,
        ),
        allow_log_upload=_as_bool(section.get("allow_log_upload"), True),
        max_upload_log_mb=_positive_int(
            section.get("max_upload_log_mb"),
            DEFAULT_DIAGNOSIS_UPLOAD_MB,
        ),
    )


__all__ = [
    "DEFAULT_DETAIL_MAX_BYTES",
    "DEFAULT_SESSION_DATABASE_DIRECTORY",
    "DiagnosisSettings",
    "TrajectoryStoreSettings",
    "load_diagnosis_settings",
    "load_trajectory_store_settings",
    "session_database_path",
]
