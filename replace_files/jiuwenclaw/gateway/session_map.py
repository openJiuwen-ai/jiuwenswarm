from __future__ import annotations

import json
import secrets
import time
from enum import Enum
from pathlib import Path

from jiuwenclaw.utils import get_checkpoint_dir, logger


class SessionMapScope(str, Enum):
    """How SessionMap keys and agent session_id strings are derived from inbound identity."""

    # (default) One agent session per (provider, chat, bot); users in the same chat share context.
    PER_CHAT_BOT = "per_chat_bot"
    # One agent session per (provider, chat, bot, user)
    PER_CHAT_BOT_USER = "per_chat_bot_user"


def load_session_map_scope() -> SessionMapScope:
    default = SessionMapScope.PER_CHAT_BOT
    try:
        from jiuwenclaw.config import get_config

        raw = str((get_config().get("gateway") or {}).get("session_map_scope") or default.value).strip().lower()
        return SessionMapScope(raw)
    except ValueError:
        logger.warning("Unknown gateway.session_map_scope %r, using %s", raw, default.value)
        return default
    except Exception as exc:  # noqa: BLE001
        logger.warning("SessionMap scope load failed, using %s: %s", default.value, exc)
        return default


def _make_key(
    scope: SessionMapScope,
    provider: str,
    chat_id: str,
    bot_id: str,
    user_id: str,
) -> str:
    if scope == SessionMapScope.PER_CHAT_BOT:
        return f"{provider}::{chat_id}::{bot_id}"
    return f"{provider}::{chat_id}::{bot_id}::{user_id}"


def _make_session_id(
    scope: SessionMapScope,
    provider: str,
    chat_id: str,
    bot_id: str,
    user_id: str,
) -> str:
    ts = format(int(time.time() * 1000), "x")
    suffix = secrets.token_hex(3)
    if scope == SessionMapScope.PER_CHAT_BOT:
        return f"{provider}::{chat_id}::{bot_id}::{ts}::{suffix}"
    return f"{provider}::{chat_id}::{bot_id}::{user_id}::{ts}::{suffix}"


class SessionMap:
    """Map stable identity (per config scope) -> rotating agent ``session_id``."""

    def __init__(self, *, scope: SessionMapScope | None = None) -> None:
        self._scope = scope if scope is not None else load_session_map_scope()
        self._store_path: Path = get_checkpoint_dir() / "session_map.json"
        self._mapping: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            if not self._store_path.exists():
                return
            with open(self._store_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._mapping = {str(k): str(v) for k, v in data.items() if isinstance(v, str) and v}
        except Exception as exc:  # noqa: BLE001
            logger.warning("SessionMap load failed: %s", exc)

    def _save(self) -> None:
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._store_path, "w", encoding="utf-8") as f:
                json.dump(self._mapping, f, ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001
            logger.warning("SessionMap save failed: %s", exc)

    def get_session_id(
        self,
        provider: str,
        chat_id: str,
        bot_id: str,
        user_id: str,
        *,
        rotate: bool = False,
    ) -> str:
        key = _make_key(self._scope, provider, chat_id, bot_id, user_id)
        existing = self._mapping.get(key)
        if existing and not rotate:
            return existing

        sid = _make_session_id(self._scope, provider, chat_id, bot_id, user_id)
        if existing == sid:
            return sid
        self._mapping[key] = sid
        self._save()
        return sid
