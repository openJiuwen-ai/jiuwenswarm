"""RPC handlers for the Notes Vault plugin.

Handlers are coroutines bound to a duck-typed web channel -- the exact
contract ``ApplicationPluginExtension.bind_web_channel`` receives.  Each
method is a plain ``fn(options, session_id) -> payload`` wrapped by
:func:`_wrap`, which keeps channel boilerplate in one place.  Every method
scopes its data by the caller's ``session_id``, so one chat session never
sees another's notes.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .store import (
    DEFAULT_SCOPE,
    MAX_NOTES_PER_SCOPE,
    NotesStore,
)

#: Environment variable controlling whether the plugin serves requests.
ENABLED_ENV = "NOTES_VAULT_ENABLED"

#: Environment variable overriding the storage directory.
STORAGE_DIR_ENV = "NOTES_VAULT_DIR"

#: Values (case-folded) that turn an environment flag off.
_FALSY = frozenset({"0", "false", "no", "off", ""})

Handler = Callable[[Any, Any, Any, Any], Awaitable[None]]
Core = Callable[[Mapping[str, Any], Any], Any]


def _flag(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().casefold() not in _FALSY


def notes_vault_enabled() -> bool:
    """Return whether the plugin should serve requests (default: enabled)."""
    return _flag(ENABLED_ENV, default=True)


def set_enabled(enabled: bool) -> None:
    """Set the in-process enabled flag."""
    os.environ[ENABLED_ENV] = "true" if enabled else "false"


def default_storage_dir() -> Path:
    """Return the fallback storage directory (no Jiuwen imports required)."""
    override = (os.getenv(STORAGE_DIR_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".jiuwenswarm" / "notes_vault"


@dataclass
class VaultState:
    """Owns the live store so settings can rebuild it in place."""

    storage_dir: Path
    max_notes: int = MAX_NOTES_PER_SCOPE
    store: NotesStore = field(init=False)

    def __post_init__(self) -> None:
        self.store = NotesStore(self.storage_dir, max_notes=self.max_notes)

    def rebuild(self, *, max_notes: int | None = None) -> None:
        """Rebuild the store, optionally with a new per-scope note limit."""
        if max_notes is not None:
            self.max_notes = max_notes
        self.store = NotesStore(self.storage_dir, max_notes=self.max_notes)

    def payload(self, *, enabled: bool | None = None) -> dict[str, Any]:
        """Return the settings view sent to the frontend."""
        return {
            "enabled": notes_vault_enabled() if enabled is None else bool(enabled),
            "storage_dir": str(self.storage_dir),
            "max_notes": self.max_notes,
            "max_notes_limit": MAX_NOTES_PER_SCOPE,
            "enabled_env": ENABLED_ENV,
            "storage_dir_env": STORAGE_DIR_ENV,
        }


def _params(params: Any) -> Mapping[str, Any]:
    """Return ``params`` as a mapping, tolerating ``None``."""
    if params is None:
        return {}
    if not isinstance(params, Mapping):
        raise TypeError("params must be an object")
    return params


async def _ok(channel: Any, ws: Any, req_id: Any, payload: Any) -> None:
    await channel.send_response(ws, req_id, ok=True, payload=payload)


def _guarded(channel: Any, handler: Handler) -> Handler:
    """Wrap a handler so it always answers instead of raising into the channel."""

    async def wrapped(ws: Any, req_id: Any, params: Any, session_id: Any) -> None:
        try:
            await handler(ws, req_id, params, session_id)
        except (TypeError, ValueError) as exc:
            await channel.send_response(
                ws, req_id, ok=False, error=str(exc), code="BAD_REQUEST"
            )
        except Exception as exc:  # noqa: BLE001
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                code="INTERNAL_ERROR",
            )

    return wrapped


def build_handlers(channel: Any, state: VaultState) -> dict[str, Handler]:
    """Build the ``{method_name: handler}`` mapping this plugin owns."""

    def _wrap(core: Core) -> Handler:
        async def handler(ws: Any, req_id: Any, params: Any, session_id: Any) -> None:
            await _ok(channel, ws, req_id, core(_params(params), session_id))

        return handler

    def list_notes(options: Mapping[str, Any], session_id: Any) -> Any:
        notes = state.store.list_notes(
            session_id,
            query=options.get("query"),
            tag=options.get("tag"),
            limit=options.get("limit"),
        )
        return {
            "notes": notes,
            "total": len(notes),
            "scope": session_id or DEFAULT_SCOPE,
        }

    def get_note(options: Mapping[str, Any], session_id: Any) -> Any:
        return {"note": state.store.get_note(session_id, options.get("note_id"))}

    def create_note(options: Mapping[str, Any], session_id: Any) -> Any:
        return {
            "note": state.store.create_note(
                session_id,
                title=options.get("title"),
                body=options.get("body"),
                tags=options.get("tags"),
                pinned=options.get("pinned", False),
            )
        }

    def update_note(options: Mapping[str, Any], session_id: Any) -> Any:
        return {
            "note": state.store.update_note(
                session_id,
                options.get("note_id"),
                title=options.get("title"),
                body=options.get("body"),
                tags=options.get("tags"),
                pinned=options.get("pinned"),
            )
        }

    def delete_note(options: Mapping[str, Any], session_id: Any) -> Any:
        return {"note": state.store.delete_note(session_id, options.get("note_id"))}

    def stats(options: Mapping[str, Any], session_id: Any) -> Any:
        del options
        return state.store.stats(session_id)

    def export_markdown(options: Mapping[str, Any], session_id: Any) -> Any:
        del options
        return {"markdown": state.store.export_markdown(session_id)}

    def settings_get(options: Mapping[str, Any], session_id: Any) -> Any:
        del options, session_id
        return state.payload()

    def settings_update(options: Mapping[str, Any], session_id: Any) -> Any:
        del session_id
        values = options.get("values") or {}
        if not isinstance(values, Mapping):
            raise TypeError("values must be an object")
        unknown = set(values) - {"max_notes"}
        if unknown:
            raise ValueError(
                "unknown notes vault settings: " + ", ".join(sorted(unknown))
            )
        if "max_notes" in values:
            raw = values["max_notes"]
            if isinstance(raw, bool) or not isinstance(raw, int):
                raise ValueError("max_notes must be an integer")
            if not 1 <= raw <= MAX_NOTES_PER_SCOPE:
                raise ValueError(
                    f"max_notes must be between 1 and {MAX_NOTES_PER_SCOPE}"
                )
            state.rebuild(max_notes=raw)
        return state.payload()

    def settings_set_enabled(options: Mapping[str, Any], session_id: Any) -> Any:
        del session_id
        enabled = options.get("enabled")
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a boolean")
        set_enabled(enabled)
        return state.payload()

    return {
        "notes.list": _wrap(list_notes),
        "notes.get": _wrap(get_note),
        "notes.create": _wrap(create_note),
        "notes.update": _wrap(update_note),
        "notes.delete": _wrap(delete_note),
        "notes.stats": _wrap(stats),
        "notes.export": _wrap(export_markdown),
        "notes.settings.get": _wrap(settings_get),
        "notes.settings.update": _wrap(settings_update),
        "notes.settings.set_enabled": _wrap(settings_set_enabled),
    }


#: Methods that stay reachable while the plugin is disabled so an operator
#: can re-enable it from its own management UI.
MANAGEMENT_METHODS = frozenset(
    {
        "notes.settings.get",
        "notes.settings.update",
        "notes.settings.set_enabled",
    }
)


def register_handlers(channel: Any, state: VaultState) -> dict[str, Handler]:
    """Register every handler on ``channel`` (all ``local_only``) and return them.

    Notes are answered by the Gateway process itself, never forwarded onward.
    """
    handlers = build_handlers(channel, state)
    for method, handler in handlers.items():
        channel.register_method(
            method,
            _guarded(channel, handler),
            local_only=True,
            available_when_disabled=method in MANAGEMENT_METHODS,
        )
    return handlers
