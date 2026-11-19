# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Runtime records, lifecycle errors and tracing shared by the AgentOS Router."""
from __future__ import annotations

import contextvars
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, TypedDict

TRACE_ID_HEADER = "X-Trace-Id"
AGENT_GENERATION_ENV = "JIUWEN_GATEWAY_SANDBOX_GENERATION"
_TRACE_ID_MAX_LEN = 128
_southbound_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "agentos_southbound_trace_id", default=""
)

def deterministic_agent_instance_id(namespace: str, name: str) -> str:
    """Return the Environment ID for a namespace and a physical sandbox name."""
    return f"{namespace}-{name}"


def normalize_trace_id(value: str | None = None) -> str:
    """Use the caller value, or mint a UUID (frontend also caps at 128 chars)."""
    text = str(value or "").strip()
    if not text:
        text = str(uuid.uuid4())
    return text[:_TRACE_ID_MAX_LEN]

def bind_southbound_trace_id(value: str | None = None) -> str:
    """Normalize, remember on this task, and return the id sent southbound."""
    resolved = normalize_trace_id(value)
    _southbound_trace_id.set(resolved)
    return resolved

def current_southbound_trace_id() -> str:
    """Last southbound mint in this task; empty if Environment was not called."""
    return str(_southbound_trace_id.get() or "").strip()

def clear_southbound_trace_id() -> None:
    """Drop the task-local id so a later 401 cannot inherit a previous call."""
    _southbound_trace_id.set("")

def extract_trace_id(headers: Mapping[str, Any] | None) -> str:
    """Read ``X-Trace-Id`` from headers (any case). Empty when absent."""
    if not headers:
        return ""
    for key, raw in headers.items():
        if str(key).lower() == "x-trace-id":
            return str(raw or "").strip()
    return ""

def apply_trace_header(
    headers: Mapping[str, str] | None,
    trace_id: str | None = None,
) -> dict[str, str]:
    """Set canonical ``X-Trace-Id``; prefer explicit, then existing header, then mint."""
    merged = {str(key): str(value) for key, value in dict(headers or {}).items()}
    resolved = bind_southbound_trace_id(trace_id or extract_trace_id(merged))
    return {
        key: value
        for key, value in merged.items()
        if str(key).lower() != "x-trace-id"
    } | {TRACE_ID_HEADER: resolved}

@dataclass(frozen=True)
class RuntimeProbeSettings:
    """TCP probe timings and GET wait knobs (Environment camelCase).

    Startup/liveness override via ``gateway.agentos.probes`` /
    ``AGENTOS_PROBE_*``. GET wait override via
    ``gateway.agentos.wait_running_{timeout,interval}_seconds`` /
    ``AGENTOS_WAIT_RUNNING_*`` (see ``load_router_config``).
    """

    startup_initial_delay_seconds: int = 3
    startup_period_seconds: int = 3
    startup_timeout_seconds: int = 2
    startup_failure_threshold: int = 6
    liveness_timeout_seconds: int = 2
    liveness_failure_threshold: int = 3
    wait_running_timeout_seconds: float = 90.0
    wait_running_interval_seconds: float = 1.0

    def startup_budget_seconds(self) -> float:
        """Worst-case startup TCP probe window before ``status=running``.

        ``initialDelaySeconds + periodSeconds * failureThreshold``.
        Liveness runs after the instance is already running and is not part
        of the GET wait.
        """
        return float(
            int(self.startup_initial_delay_seconds)
            + int(self.startup_period_seconds)
            * int(self.startup_failure_threshold)
        )

    def same_tcp_timings(self, other: RuntimeProbeSettings) -> bool:
        """True when startup/liveness knobs match (GET wait knobs ignored)."""
        return (
            int(self.startup_initial_delay_seconds)
            == int(other.startup_initial_delay_seconds)
            and int(self.startup_period_seconds)
            == int(other.startup_period_seconds)
            and int(self.startup_timeout_seconds)
            == int(other.startup_timeout_seconds)
            and int(self.startup_failure_threshold)
            == int(other.startup_failure_threshold)
            and int(self.liveness_timeout_seconds)
            == int(other.liveness_timeout_seconds)
            and int(self.liveness_failure_threshold)
            == int(other.liveness_failure_threshold)
        )

    def startup_tcp(self, port: int) -> dict[str, Any]:
        return {
            "tcpSocket": {"port": int(port)},
            "initialDelaySeconds": int(self.startup_initial_delay_seconds),
            "periodSeconds": int(self.startup_period_seconds),
            "timeoutSeconds": int(self.startup_timeout_seconds),
            "failureThreshold": int(self.startup_failure_threshold),
        }

    def liveness_tcp(self, port: int) -> dict[str, Any]:
        return {
            "tcpSocket": {"port": int(port)},
            "timeoutSeconds": int(self.liveness_timeout_seconds),
            "failureThreshold": int(self.liveness_failure_threshold),
        }

    def tcp_probes(self, port: int, *, with_liveness: bool = False) -> dict[str, Any]:
        probes: dict[str, Any] = {"startup": self.startup_tcp(port)}
        if with_liveness:
            probes["liveness"] = self.liveness_tcp(port)
        return probes

DEFAULT_RUNTIME_PROBE_SETTINGS = RuntimeProbeSettings()

DEFAULT_THIRD_AGENT_PROBE_SETTINGS = RuntimeProbeSettings(
    startup_initial_delay_seconds=2,
    startup_period_seconds=3,
    startup_timeout_seconds=2,
    startup_failure_threshold=8,
    liveness_timeout_seconds=2,
    liveness_failure_threshold=3,
)

class AgentMount(TypedDict, total=False):
    """Bind mount for POST Environment API ``mounts``."""

    source: str
    target: str
    readonly: bool

class AgentRootfsSpec(TypedDict, total=False):
    """Inline ``runtime_spec.rootfs`` for POST Environment API."""

    type: str
    path: str
    readonly: bool
    imageurl: str
    user: str
    ports: list[str]

class AgentRuntimeSpec(TypedDict, total=False):
    """Framework launch configuration assembled before Environment creation."""

    runtime: str
    sandbox_type: str
    rootfs: AgentRootfsSpec
    cpu: int
    memory: int
    code_path: str
    cmds: list[list[str]]
    probes: dict[str, Any]

@dataclass
class SandboxInfo:
    """AgentOS lifecycle record produced by the Environment control adapter."""

    sandbox_id: str
    status: str = "ready"
    metadata: dict[str, Any] = field(default_factory=dict)

class AgentApiError(RuntimeError):
    """Raised when Environment Environment API returns a non-success response."""

class AgentRequestNotSentError(AgentApiError):
    """The request could not be dispatched because connection setup failed."""

class AgentDeleteRejectedError(AgentApiError):
    """The frontend explicitly rejected a delete request."""

class AgentCreateUncertainError(AgentApiError):
    """A create response does not establish whether an instance was created."""

class AgentTimeoutError(AgentApiError):
    """Environment did not respond in time. A write may already have taken effect."""

class AgentNotVisibleError(AgentTimeoutError):
    """Every completed observation during a guarded wait reported absence."""

class AgentNotFoundError(AgentApiError):
    """GET explicitly reports the instance as not found or not running."""

class AgentGenerationMismatchError(AgentApiError):
    """GET returned an instance belonging to a different generation."""

class AgentFailedError(AgentApiError):
    """The requested generation reached a terminal failure status."""

@dataclass(frozen=True)
class AgentFileDownloadChunk:
    """One chunk from GET Environment API/:instanceId/files/download."""

    data: bytes
    path: str
    offset: int
    chunk_size: int
    size: int
    content_type: str
    eof: bool

class AgentFileError(RuntimeError):
    """Raised when Environment agent file upload/download/list/mkdir fails."""

    def __init__(self, message: str, *, http_status: int = 500, error_code: str = "INTERNAL_ERROR") -> None:
        super().__init__(message)
        self.http_status = int(http_status)
        self.error_code = str(error_code)
