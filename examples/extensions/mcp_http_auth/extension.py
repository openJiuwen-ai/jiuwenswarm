# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Reference extension: inject per-request auth headers at the MCP HTTP egress.

Uses ``agent_server:before_mcp_http`` (see ``jiuwenswarm.common.mcp_http_hook``),
which fires before *every* outgoing MCP HTTP request — not once per chat turn.
Use it when the gateway needs a credential that changes per call (short-lived
tokens, per-call signatures, one-time tickets). For "one token per user turn"
the cheaper route is ``chat.send`` carrying ``mcp_runtime_params`` (declared in
the registration), which this extension does not touch.

Note the difference: that route puts the value into the connection pool key, so
different values get different connections; this hook only rewrites headers on
whatever connection is already selected. Use both together when a per-user
credential must also rotate per request.

The example resolves the token from an environment variable and optionally
swaps it through ``token_provider`` so the same file can be adapted to a real
STS / vault lookup: the provider is called on every request, never cached.

Config (``config.yaml``)::

    extensions:
      mcp_http_auth:
        enabled: true
        header_name: Authorization
        token_prefix: "Bearer "
        token_env: CORP_MCP_TOKEN
        servers:
          corp-mcp-gw: {}          # 只对这些服务注入；不写则全部

Only header names outside the host-protected set are applied; the host drops
``Content-Type`` / ``Accept`` / ``Mcp-Session-Id`` and friends even if written.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

CONFIG_SECTION = ("extensions", "mcp_http_auth")
DEFAULT_HEADER = "Authorization"
DEFAULT_PREFIX = "Bearer "

TokenProvider = Callable[[str, str], str | None]

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"", "0", "false", "no", "off"})


def _parse_enabled(raw: Any) -> bool:
    """Strict truthiness for ``enabled``.

    YAML may hand us a quoted string (``enabled: "false"``) or a placeholder
    substitution result, where ``bool("false")`` would silently keep the hook
    on and inject the token to every MCP server. Unknown values fail closed
    with a warning instead of guessing.
    """

    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    if isinstance(raw, int):
        return raw != 0
    text = str(raw).strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    logger.warning("[mcp_http_auth] unrecognized enabled value %r; disabled", raw)
    return False


def _parse_prefix(raw: Any) -> str:
    """``token_prefix`` fallback: missing/None keeps the default.

    An explicit empty string means "no prefix", so it must not be replaced.
    """

    if raw is None:
        return DEFAULT_PREFIX
    return str(raw)


@dataclass(frozen=True)
class HttpAuthSettings:
    enabled: bool = False
    header_name: str = DEFAULT_HEADER
    token_prefix: str = DEFAULT_PREFIX
    token_env: str = ""
    servers: Mapping[str, str] = field(default_factory=dict)


def parse_settings(section: Mapping[str, Any] | None) -> HttpAuthSettings:
    if not isinstance(section, Mapping):
        return HttpAuthSettings()
    servers: dict[str, str] = {}
    raw_servers = section.get("servers")
    if raw_servers is not None and not isinstance(raw_servers, Mapping):
        # A list/str here would silently degrade to "inject to every server"
        # and leak the corporate token to unrelated MCP servers: fail closed.
        logger.warning(
            "[mcp_http_auth] 'servers' must be a mapping, got %s; staying disabled",
            type(raw_servers).__name__,
        )
        return HttpAuthSettings()
    if isinstance(raw_servers, Mapping):
        for raw_name in raw_servers:
            name = str(raw_name or "").strip()
            if name:
                servers[name] = name
    return HttpAuthSettings(
        enabled=_parse_enabled(section.get("enabled", False)),
        header_name=str(section.get("header_name") or DEFAULT_HEADER).strip() or DEFAULT_HEADER,
        token_prefix=_parse_prefix(section.get("token_prefix", DEFAULT_PREFIX)),
        token_env=str(section.get("token_env") or "").strip(),
        servers=servers,
    )


def load_settings(config: Mapping[str, Any] | None = None) -> HttpAuthSettings:
    if config is None:
        try:
            from jiuwenswarm.common.config import get_config

            config = get_config()
        except Exception:  # noqa: BLE001 - extension must not break startup
            logger.warning("[mcp_http_auth] config unavailable; stays disabled")
            config = {}
    section: Any = config
    for key in CONFIG_SECTION:
        section = section.get(key) if isinstance(section, Mapping) else None
    return parse_settings(section if isinstance(section, Mapping) else None)


def make_handler(
    settings: HttpAuthSettings,
    token_provider: TokenProvider | None = None,
):
    """Build the ``BEFORE_MCP_HTTP`` handler.

    ``token_provider(server_name, method)`` wins over the env variable and is
    invoked per request, so short-lived tokens stay fresh.
    """

    def _resolve(server_name: str, method: str) -> str | None:
        if token_provider is not None:
            try:
                value = token_provider(server_name, method)
            except Exception:  # noqa: BLE001 - a failing provider must not break MCP
                logger.warning("[mcp_http_auth] token provider failed: %s", server_name)
                return None
            return value or None
        if not settings.token_env:
            return None
        return os.environ.get(settings.token_env) or None

    async def handle_before_mcp_http(ctx: Any) -> None:
        if not settings.enabled:
            return
        server_name = str(getattr(ctx, "server_name", "") or "")
        if settings.servers and server_name not in settings.servers:
            return
        token = _resolve(server_name, str(getattr(ctx, "method", "") or ""))
        if not token:
            return
        headers = getattr(ctx, "headers", None)
        if not isinstance(headers, dict):
            return
        headers[settings.header_name] = f"{settings.token_prefix}{token}"

    return handle_before_mcp_http


class McpHttpAuthExtension:
    def __init__(self, settings: HttpAuthSettings) -> None:
        self.settings = settings
        try:
            from jiuwenswarm.extensions.types import ExtensionMetadata

            self.metadata = ExtensionMetadata(
                id="mcp_http_auth",
                name="MCP Per-Request HTTP Auth",
                version="0.1.0",
                description="Inject per-request MCP HTTP auth headers",
                author="jiuwenswarm",
                min_jiuwenswarm_version="0.2.3",
                dependencies={},
                config_schema={"type": "object"},
            )
        except Exception:  # noqa: BLE001 - listing metadata is best-effort
            self.metadata = None

    async def shutdown(self) -> None:
        return None


async def register_extensions(registry: Any) -> list[Any]:
    settings = load_settings()
    if not settings.enabled:
        logger.info("[mcp_http_auth] disabled; no hook registered")
        return []
    try:
        from jiuwenswarm.extensions.hook_event import AgentServerHookEvents
    except Exception:  # noqa: BLE001 - extension framework unavailable
        logger.warning("[mcp_http_auth] extension framework unavailable")
        return []
    registry.register(
        AgentServerHookEvents.BEFORE_MCP_HTTP,
        make_handler(settings),
        priority=10,
    )
    logger.info(
        "[mcp_http_auth] enabled: header=%s servers=%s",
        settings.header_name,
        sorted(settings.servers) or "all",
    )
    return [McpHttpAuthExtension(settings)]


__all__ = [
    "HttpAuthSettings",
    "McpHttpAuthExtension",
    "make_handler",
    "parse_settings",
    "register_extensions",
]
