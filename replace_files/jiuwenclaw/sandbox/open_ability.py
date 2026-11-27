from __future__ import annotations

import os
from dataclasses import dataclass

_OA_USE_TLS = False
_OA_CONNECT_TIMEOUT_SECONDS = 10.0
_OA_READINESS_POLL_INTERVAL_SECONDS = 0.5
_OA_READINESS_TIMEOUT_SECONDS = 60.0
_OA_RECONNECT_TIMEOUT_SECONDS = 600.0
_OA_REQUEST_TIMEOUT_SECONDS = 600.0


class OpenAbilityReconnectTimeoutError(RuntimeError):
    """Raised when Gateway exhausts the OA reconnect window (DCS poll + connect + probe)."""


@dataclass(frozen=True)
class OpenAbilityEndpoint:
    host: str
    port: int


@dataclass(frozen=True)
class OpenAbilityConfig:
    ws_path: str
    use_tls: bool = _OA_USE_TLS
    connect_timeout_seconds: float = _OA_CONNECT_TIMEOUT_SECONDS
    readiness_poll_interval_seconds: float = _OA_READINESS_POLL_INTERVAL_SECONDS
    readiness_timeout_seconds: float = _OA_READINESS_TIMEOUT_SECONDS
    reconnect_timeout_seconds: float = _OA_RECONNECT_TIMEOUT_SECONDS
    request_timeout_seconds: float = _OA_REQUEST_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls) -> OpenAbilityConfig:
        ws_path = os.environ.get("GATEWAY_TO_OA_WS_PATH", "").strip()
        if not ws_path:
            raise RuntimeError(
                "GATEWAY_TO_OA_WS_PATH environment variable is required when sandbox routing is enabled"
            )
        request_timeout_seconds = _env_float(
            "SANDBOX_TO_OA_REQUEST_TIMEOUT_SECONDS",
            default=_OA_REQUEST_TIMEOUT_SECONDS,
        )
        reconnect_timeout_seconds = _env_float(
            "SANDBOX_OA_RECONNECT_TIMEOUT_SECONDS",
            default=_OA_RECONNECT_TIMEOUT_SECONDS,
        )
        return cls(
            ws_path=ws_path,
            reconnect_timeout_seconds=max(1.0, reconnect_timeout_seconds),
            request_timeout_seconds=max(1.0, request_timeout_seconds),
        )


def _env_float(name: str, *, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except ValueError:
        return default


def format_openability_endpoint(endpoint: OpenAbilityEndpoint) -> str:
    return f"{endpoint.host}:{endpoint.port}"


def build_openability_ws_uri(endpoint: OpenAbilityEndpoint, *, ws_path: str) -> str:
    scheme = "wss" if _OA_USE_TLS else "ws"
    path = ws_path if ws_path.startswith("/") else f"/{ws_path}"
    return f"{scheme}://{endpoint.host}:{endpoint.port}{path}"
