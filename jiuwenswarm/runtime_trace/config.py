# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Lightweight reader for ``runtime_observability``.

This module deliberately avoids ``jiuwenswarm.common.config`` and
``jiuwenswarm.common.utils``: both import chains pull in OpenJiuwen and
OpenTelemetry, which a disabled runtime trace must not add to the process.
Only the standard library and PyYAML are used.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Internal constants (not part of the YAML contract, see §4.2 of the plan).
_NORMAL_QUEUE_SIZE = 1024
_CRITICAL_QUEUE_SIZE = 128
_DRAIN_CAP_SECONDS = 3.0
_READY_TIMEOUT_SECONDS = 300.0
_RETENTION_SWEEP_SECONDS = 600.0
_RETENTION_BATCH_TRACES = 200
_MAINTENANCE_LOCK_TIMEOUT = 0.1
_BUILD_LOCK_TIMEOUT = 5.0
_MAX_OPEN_HANDLES = 4096
_BUSY_TIMEOUT_MS = 5000
_SCHEMA_VERSION = 1
_EXPORT_TIMEOUT_MS = 2000
_EXPORT_MAX_QUEUE = 512
_EXPORT_BATCH_SIZE = 512

_DEFAULT_SLOW_TASK_MS = 1000
_DEFAULT_RETENTION_DAYS = 7
_MAX_SERVICE_NAME_LENGTH = 64

_EXPORTERS = frozenset({"none", "otlp_grpc", "otlp_http", "console"})

_warned = False


@dataclass(frozen=True, slots=True)
class RuntimeTraceSettings:
    enabled: bool
    slow_task_ms: int
    retention_days: int
    exporter: str = ""
    endpoint: str = ""
    headers: tuple[tuple[str, str], ...] = ()
    service_name: str = ""


def _default_workspace() -> Path:
    data_dir = os.environ.get("JIUWENSWARM_DATA_DIR")
    if data_dir:
        return Path(data_dir)
    home = os.environ.get("JIUWENSWARM_HOME")
    base = Path(home) if home else Path.home()
    return base / ".jiuwenswarm"


def _config_candidates(workspace: Path) -> tuple[Path, ...]:
    return (
        workspace / "config" / "config.yaml",
        Path(__file__).resolve().parent.parent / "resources" / "config.yaml",
    )


def _warn_once(message: str, *args: Any) -> None:
    global _warned
    if _warned:
        return
    _warned = True
    logger.warning(message, *args)


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - configuration must never break startup
        _warn_once("runtime trace: cannot read %s (%s); using defaults", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _clean_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _service_name(value: Any) -> str:
    """Return a usable OTel service name; over-long values fall back to default."""
    name = _clean_str(value)
    return name if len(name) <= _MAX_SERVICE_NAME_LENGTH else ""


def _positive_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int) and value > 0:
        return value
    return default


def _telemetry_enabled(telemetry: dict[str, Any]) -> bool:
    # The template ships ``telemetry.endpoint`` with a localhost default and
    # ``enabled: false``; inheriting on the endpoint alone would export to a
    # collector nobody started.
    return telemetry.get("enabled") is True


def _resolve_exporter(runtime: dict[str, Any], telemetry: dict[str, Any]) -> str:
    configured = _clean_str(runtime.get("exporter")).lower()
    if configured:
        if configured in _EXPORTERS:
            return configured
        _warn_once("runtime trace: unsupported exporter %r; local-only", configured)
        return "none"
    # Inheritance only applies when the deployment layer actually turned
    # telemetry on. ``agentos_trace.py`` writes ``exporter: otlp`` even for an
    # empty endpoint, which would otherwise point at the default local collector.
    if not _telemetry_enabled(telemetry) or not _clean_str(telemetry.get("endpoint")):
        return "none"
    inherited = _clean_str(telemetry.get("exporter")).lower()
    if inherited == "otlp":
        protocol = _clean_str(telemetry.get("protocol")).lower()
        inherited = "otlp_http" if protocol == "http" else "otlp_grpc"
    if inherited in _EXPORTERS:
        return inherited
    return "none"


def _resolve_endpoint(runtime: dict[str, Any], telemetry: dict[str, Any]) -> str:
    configured = _clean_str(runtime.get("endpoint"))
    if configured:
        return configured
    if not _telemetry_enabled(telemetry):
        return ""
    return _clean_str(telemetry.get("endpoint"))


def _resolve_headers(runtime: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """Build the Basic auth header from the keys shared with team/agent observability.

    Runtime trace reports to the same collector as ``team_observability`` /
    ``agent_observability``, so it accepts the same pair of Langfuse keys
    instead of an open ``headers`` map. The encoding matches openjiuwen's
    ``build_auth_headers``; it is reimplemented here because importing that
    module would pull ``openjiuwen`` into the Front/Worker startup path.
    """
    public_key = _clean_str(runtime.get("langfuse_public_key"))
    secret_key = _clean_str(runtime.get("langfuse_secret_key"))
    if not public_key and not secret_key:
        return ()
    if not public_key or not secret_key:
        _warn_once(
            "runtime trace: langfuse_public_key and langfuse_secret_key must "
            "both be set; exporting without auth"
        )
        return ()
    credentials = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    # gRPC metadata keys must be lower case; the HTTP exporter accepts it as is.
    return (("authorization", f"Basic {credentials}"),)


def load_runtime_trace_settings(*, workspace: Path | None = None) -> RuntimeTraceSettings:
    """Read ``runtime_observability`` without touching the Core config stack.

    Missing files, missing blocks, and malformed values all resolve to the
    disabled defaults. No directory is created and no heavy module is imported.
    """
    base = workspace if workspace is not None else _default_workspace()
    data: dict[str, Any] = {}
    for candidate in _config_candidates(base):
        if candidate.is_file():
            data = _load_yaml(candidate)
            break

    telemetry = data.get("telemetry")
    if not isinstance(telemetry, dict):
        telemetry = {}
    runtime = data.get("runtime_observability")
    if not isinstance(runtime, dict):
        runtime = {}

    enabled = runtime.get("enabled") is True
    slow_task_ms = _positive_int(runtime.get("slow_task_ms"), _DEFAULT_SLOW_TASK_MS)
    retention_days = _positive_int(runtime.get("retention_days"), _DEFAULT_RETENTION_DAYS)

    return RuntimeTraceSettings(
        enabled=enabled,
        slow_task_ms=slow_task_ms,
        retention_days=retention_days,
        exporter=_resolve_exporter(runtime, telemetry),
        endpoint=_resolve_endpoint(runtime, telemetry),
        headers=_resolve_headers(runtime),
        service_name=_service_name(runtime.get("service_name")),
    )
