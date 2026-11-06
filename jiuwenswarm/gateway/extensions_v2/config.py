# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Configuration for the Gateway-side extension runtime.

Gateway and AgentServer share one install directory
(``extensions.gateway.install_dir``, default ``extension_pkgs``) and tell their
packages apart through the manifest ``domain`` field, so one directory serves
both hosts while each side loads only its own packages. Every value has a safe
default: an empty allowlist denies every outbound request.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DEFAULT_INSTALL_DIR = "extension_pkgs"
BUNDLED_EXAMPLES = Path("extensions_v2") / "examples"
DEFAULT_HTTP_TIMEOUT_MS = 3000
DEFAULT_HTTP_MAX_BODY_BYTES = 1024 * 1024
MAX_HTTP_TIMEOUT_MS = 5000
MAX_HTTP_BODY_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class HttpPolicy:
    """Outbound request policy used by ``ops.http.fetch``."""

    allowed_hosts: tuple[str, ...] = ()
    timeout_ms: int = DEFAULT_HTTP_TIMEOUT_MS
    max_body_bytes: int = DEFAULT_HTTP_MAX_BODY_BYTES
    follow_redirects: bool = False

    def host_allowed(self, url: str) -> bool:
        """Return True when ``url`` matches one allowlist entry.

        An entry is either ``host`` (any port) or ``host:port``. IPv6 literals
        use brackets (``[::1]:8080``). ``host:port`` also matches a URL that
        omits the default port for its scheme. An empty allowlist denies
        everything, which keeps the default deployment closed.
        """

        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        host = parsed.hostname.lower()
        try:
            port = parsed.port
        except ValueError:
            return False
        if port is None:
            port = 443 if parsed.scheme == "https" else 80
        for entry in self.allowed_hosts:
            allowed_host, allowed_port = _split_entry(entry)
            if not allowed_host or allowed_host != host:
                continue
            if not allowed_port or allowed_port == str(port):
                return True
        return False


def _split_entry(entry: str) -> tuple[str, str]:
    """Split an allowlist entry into ``(host, port)``; empty port means any."""

    text = entry.strip().lower()
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            return "", ""
        rest = text[end + 1:]
        return text[1:end], rest[1:] if rest.startswith(":") else ""
    host, separator, port = text.rpartition(":")
    if not separator:
        return text, ""
    return host, port


@dataclass(frozen=True, slots=True)
class GatewayExtensionConfig:
    """Configuration of the Gateway extension runtime."""

    enabled: bool = True
    install_dir: Path = Path(DEFAULT_INSTALL_DIR)
    http: HttpPolicy = HttpPolicy()


def load_gateway_config(config: Mapping[str, Any] | None = None) -> GatewayExtensionConfig:
    """Read ``extensions.gateway`` from the JiuwenSwarm configuration."""

    source = config if config is not None else _read_config()
    extensions = source.get("extensions")
    gateway = extensions.get("gateway") if isinstance(extensions, Mapping) else None
    if not isinstance(gateway, Mapping):
        gateway = {}

    enabled = gateway.get("enabled")
    install_dir = gateway.get("install_dir")
    if not isinstance(install_dir, str) or not install_dir.strip():
        install_dir = DEFAULT_INSTALL_DIR
    return GatewayExtensionConfig(
        enabled=enabled if isinstance(enabled, bool) else True,
        install_dir=_resolve_dir(install_dir),
        http=_read_http_policy(gateway.get("http")),
    )


def _read_http_policy(raw: Any) -> HttpPolicy:
    policy = raw if isinstance(raw, Mapping) else {}
    hosts = policy.get("allowed_hosts")
    allowed_hosts = tuple(
        entry.strip() for entry in hosts if isinstance(entry, str) and entry.strip()
    ) if isinstance(hosts, list) else ()
    timeout_ms = _bounded_int(policy.get("timeout_ms"), DEFAULT_HTTP_TIMEOUT_MS, MAX_HTTP_TIMEOUT_MS)
    max_body_bytes = _bounded_int(
        policy.get("max_body_bytes"), DEFAULT_HTTP_MAX_BODY_BYTES, MAX_HTTP_BODY_BYTES
    )
    follow_redirects = policy.get("follow_redirects")
    return HttpPolicy(
        allowed_hosts=allowed_hosts,
        timeout_ms=timeout_ms,
        max_body_bytes=max_body_bytes,
        follow_redirects=follow_redirects if isinstance(follow_redirects, bool) else False,
    )


def _bounded_int(value: Any, default: int, upper: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return default
    return min(value, upper)


def _resolve_dir(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        from jiuwenswarm.common.utils import get_user_workspace_dir

        path = get_user_workspace_dir() / path
    return path.resolve()


def resolve_examples_dir(config: Mapping[str, Any] | None = None) -> Path:
    """Resolve ``extensions.examples_dir``, defaulting to the bundled presets."""

    source = config if config is not None else _read_config()
    extensions = source.get("extensions")
    raw = extensions.get("examples_dir") if isinstance(extensions, Mapping) else None
    if isinstance(raw, str) and raw.strip():
        return _resolve_dir(raw.strip())
    return (Path(__file__).resolve().parents[2] / BUNDLED_EXAMPLES).resolve()


def _read_config() -> Mapping[str, Any]:
    try:
        from jiuwenswarm.common.config import get_config

        value = get_config()
    except Exception:  # noqa: BLE001 - a broken config only disables the feature
        return {}
    return value if isinstance(value, Mapping) else {}
