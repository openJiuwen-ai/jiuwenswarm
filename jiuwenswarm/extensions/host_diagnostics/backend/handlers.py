"""RPC handler registration for the host diagnostics plugin.

The handlers are plain coroutines bound to a duck-typed web channel, which
is exactly the contract ``ApplicationPluginExtension.bind_web_channel``
receives.  Keeping them in their own module (rather than inline in
``extension.py``) lets tests drive every method through a fake channel
without importing the extension loader or the Gateway.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .collector import (
    MAX_ENV_ENTRIES,
    collect_health,
    collect_paths,
    collect_summary,
    describe_application_plugins,
    describe_typed_extensions,
)

#: Environment variable controlling whether the plugin exposes its features.
ENABLED_ENV = "HOST_DIAGNOSTICS_ENABLED"

#: Environment variable seeding the default for ``include_env``.
INCLUDE_ENV_ENV = "HOST_DIAGNOSTICS_INCLUDE_ENV"

#: Values (case-folded) that turn an environment flag off.
_FALSY = frozenset({"0", "false", "no", "off", ""})

#: Hard cap so a hostile/buggy caller cannot ask for an unbounded snapshot.
MAX_ENV_LIMIT = 512

Handler = Callable[[Any, Any, Any, Any], Awaitable[None]]


def _flag(name: str, *, default: bool) -> bool:
    """Read a boolean environment flag, falling back to ``default``."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().casefold() not in _FALSY


def diagnostics_enabled() -> bool:
    """Return whether the plugin should serve requests (default: enabled)."""
    return _flag(ENABLED_ENV, default=True)


def set_enabled(enabled: bool) -> None:
    """Set the in-process enabled flag (mirrors the video-duplex pattern)."""
    os.environ[ENABLED_ENV] = "true" if enabled else "false"


@dataclass
class DiagnosticsSettings:
    """Mutable, validated options for the diagnostics payloads.

    ``include_env`` defaults to off because the environment snapshot is the
    most sensitive part of the output; operators opt in explicitly.
    """

    include_env: bool = False
    env_limit: int = MAX_ENV_ENTRIES

    @classmethod
    def from_env(cls) -> DiagnosticsSettings:
        """Build settings from the process environment."""
        return cls(include_env=_flag(INCLUDE_ENV_ENV, default=False))

    def payload(self, *, enabled: bool | None = None) -> dict[str, Any]:
        """Return the settings view sent to the frontend."""
        return {
            "enabled": diagnostics_enabled() if enabled is None else bool(enabled),
            "include_env": self.include_env,
            "env_limit": self.env_limit,
            "max_env_limit": MAX_ENV_LIMIT,
            "enabled_env": ENABLED_ENV,
            "include_env_env": INCLUDE_ENV_ENV,
        }

    def update(self, values: Mapping[str, Any]) -> None:
        """Apply a partial update.

        Raises:
            ValueError: on unknown keys, wrong types, or an out-of-range limit.
        """
        if not isinstance(values, Mapping):
            raise TypeError("values must be an object")

        allowed = {"include_env", "env_limit"}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(
                "unknown host diagnostics settings: " + ", ".join(sorted(unknown))
            )

        if "include_env" in values:
            raw = values["include_env"]
            if not isinstance(raw, bool):
                raise TypeError("include_env must be a boolean")
            self.include_env = raw

        if "env_limit" in values:
            raw = values["env_limit"]
            if isinstance(raw, bool) or not isinstance(raw, int):
                raise TypeError("env_limit must be an integer")
            if not 1 <= raw <= MAX_ENV_LIMIT:
                raise ValueError(f"env_limit must be between 1 and {MAX_ENV_LIMIT}")
            self.env_limit = raw


async def _ok(channel: Any, ws: Any, req_id: Any, payload: Any) -> None:
    await channel.send_response(ws, req_id, ok=True, payload=payload)


async def _fail(
    channel: Any,
    ws: Any,
    req_id: Any,
    message: str,
    code: str = "INTERNAL_ERROR",
) -> None:
    await channel.send_response(ws, req_id, ok=False, error=message, code=code)


def _guarded(channel: Any, handler: Handler) -> Handler:
    """Wrap a handler so it always answers instead of raising into the channel."""

    async def wrapped(ws: Any, req_id: Any, params: Any, session_id: Any) -> None:
        try:
            await handler(ws, req_id, params, session_id)
        except (TypeError, ValueError) as exc:
            await _fail(channel, ws, req_id, str(exc), "BAD_REQUEST")
        except Exception as exc:  # noqa: BLE001
            await _fail(
                channel,
                ws,
                req_id,
                f"{type(exc).__name__}: {exc}",
                "INTERNAL_ERROR",
            )

    return wrapped


def build_handlers(
    channel: Any,
    *,
    registry: Any = None,
    settings: DiagnosticsSettings | None = None,
    extension_dir: str | None = None,
) -> dict[str, Handler]:
    """Build the ``{method_name: handler}`` mapping this plugin owns."""
    options = settings if settings is not None else DiagnosticsSettings()

    async def ping(ws: Any, req_id: Any, params: Any, session_id: Any) -> None:
        del params, session_id
        await _ok(channel, ws, req_id, collect_health())

    async def summary(ws: Any, req_id: Any, params: Any, session_id: Any) -> None:
        del session_id
        include_env = options.include_env
        limit = options.env_limit
        if isinstance(params, Mapping) and "include_env" in params:
            raw = params["include_env"]
            if not isinstance(raw, bool):
                raise TypeError("include_env must be a boolean")
            include_env = raw
        await _ok(
            channel,
            ws,
            req_id,
            collect_summary(include_env=include_env, env_limit=limit),
        )

    async def paths(ws: Any, req_id: Any, params: Any, session_id: Any) -> None:
        del params, session_id
        await _ok(
            channel,
            ws,
            req_id,
            collect_paths(extension_dir=extension_dir),
        )

    async def plugins(ws: Any, req_id: Any, params: Any, session_id: Any) -> None:
        del params, session_id
        await _ok(
            channel,
            ws,
            req_id,
            {
                "application_plugins": describe_application_plugins(registry),
                "typed_extensions": describe_typed_extensions(registry),
            },
        )

    async def settings_get(ws: Any, req_id: Any, params: Any, session_id: Any) -> None:
        del params, session_id
        await _ok(channel, ws, req_id, options.payload())

    async def settings_update(
        ws: Any, req_id: Any, params: Any, session_id: Any
    ) -> None:
        del session_id
        if not isinstance(params, Mapping):
            raise TypeError("params must be an object")
        values = params.get("values")
        options.update(values if values is not None else {})
        await _ok(channel, ws, req_id, options.payload())

    async def settings_set_enabled(
        ws: Any, req_id: Any, params: Any, session_id: Any
    ) -> None:
        del session_id
        if not isinstance(params, Mapping):
            raise TypeError("params must be an object")
        enabled = params.get("enabled")
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a boolean")
        set_enabled(enabled)
        await _ok(channel, ws, req_id, options.payload())

    return {
        "host.diagnostics.ping": ping,
        "host.diagnostics.summary": summary,
        "host.diagnostics.paths": paths,
        "host.diagnostics.plugins": plugins,
        "host.diagnostics.settings.get": settings_get,
        "host.diagnostics.settings.update": settings_update,
        "host.diagnostics.settings.set_enabled": settings_set_enabled,
    }


#: Methods that must stay reachable while the plugin is disabled so an
#: operator can re-enable it from its own management UI.
MANAGEMENT_METHODS = frozenset(
    {
        "host.diagnostics.settings.get",
        "host.diagnostics.settings.update",
        "host.diagnostics.settings.set_enabled",
    }
)


def register_handlers(
    channel: Any,
    *,
    registry: Any = None,
    settings: DiagnosticsSettings | None = None,
    extension_dir: str | None = None,
) -> dict[str, Handler]:
    """Register every handler on ``channel`` and return the mapping.

    All methods are ``local_only``: diagnostics are answered by the Gateway
    process itself and must never be forwarded to an AgentServer.
    """
    handlers = build_handlers(
        channel,
        registry=registry,
        settings=settings,
        extension_dir=extension_dir,
    )
    for method, handler in handlers.items():
        channel.register_method(
            method,
            _guarded(channel, handler),
            local_only=True,
            available_when_disabled=method in MANAGEMENT_METHODS,
        )
    return handlers
