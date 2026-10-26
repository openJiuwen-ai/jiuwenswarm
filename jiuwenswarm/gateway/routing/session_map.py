from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Any

from jiuwenswarm.common.utils import logger
from jiuwenswarm.gateway.routing.session_storage import (
    LocalSessionStorage,
    SessionStorage,
)


class SessionMapScope(str, Enum):
    """How SessionMap keys and agent session_id strings are derived from inbound identity."""

    # (default) One agent session per (provider, chat, bot); users in the same chat share context.
    PER_CHAT_BOT = "per_chat_bot"
    # One agent session per (provider, chat, bot, user)
    PER_CHAT_BOT_USER = "per_chat_bot_user"


def load_session_map_scope() -> SessionMapScope:
    default = SessionMapScope.PER_CHAT_BOT
    try:
        from jiuwenswarm.common.config import get_config

        raw = str((get_config().get("gateway") or {}).get("session_map_scope") or default.value).strip().lower()
        return SessionMapScope(raw)
    except ValueError:
        logger.warning("Unknown gateway.session_map_scope %r, using %s", raw, default.value)
        return default
    except Exception as exc:  # noqa: BLE001
        logger.warning("SessionMap scope load failed, using %s: %s", default.value, exc)
        return default


def invoke_service_id(chat_id: str, bot_id: str) -> str:
    """Stable runtime ``service_id`` from chat + bot: ``md5(chat_id + "::" + bot_id)``.

    Used wherever SessionMap-backed traffic maps to a deployable service key (HTTP
    invocations, runtime management, etc.).
    """
    return hashlib.md5("::".join((chat_id, bot_id)).encode("utf-8")).hexdigest()


def invoke_ids_from_identity(
    chat_id: str,
    bot_id: str,
    user_id: str,
    scope: SessionMapScope,
) -> tuple[str, str | None]:
    """Derive ``(service_id, agent_id)`` from stable IM identity and SessionMap scope."""
    sid = invoke_service_id(chat_id, bot_id)
    if scope == SessionMapScope.PER_CHAT_BOT_USER:
        return sid, user_id or None
    return sid, None


def invoke_ids_from_session_id_string(session_id: str) -> tuple[str, str | None]:
    """Derive ``(service_id, agent_id)`` from SessionMap-shaped agent ``session_id`` string.

    For migration or client-side fallback parsing only.
    """
    parts = session_id.split("::")
    if len(parts) == 6:
        _provider, chat_id, bot_id, user_id, _ts, _suffix = parts
        return invoke_service_id(chat_id, bot_id), user_id or None
    if len(parts) == 5:
        _provider, chat_id, bot_id, _ts, _suffix = parts
        return invoke_service_id(chat_id, bot_id), None
    return hashlib.md5(session_id.encode("utf-8")).hexdigest(), None


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


@dataclass(frozen=True)
class Session:
    """Gateway-persisted agent session row: ``session_id`` plus runtime ``service_id`` / ``agent_id``."""

    session_id: str
    service_id: str
    agent_id: str | None


def _session_from_stored_value(raw: Any) -> Session | None:
    if isinstance(raw, str) and raw.strip():
        sid = raw.strip()
        svc, aid = invoke_ids_from_session_id_string(sid)
        return Session(session_id=sid, service_id=svc, agent_id=aid)
    if isinstance(raw, dict):
        sid = str(raw.get("session_id") or "").strip()
        if not sid:
            return None
        svc = str(raw.get("service_id") or "").strip()
        aid_raw = raw.get("agent_id")
        if aid_raw is None or aid_raw == "":
            aid: str | None = None
        else:
            aid = str(aid_raw).strip() or None
        if not svc:
            svc, aid = invoke_ids_from_session_id_string(sid)
        return Session(session_id=sid, service_id=svc, agent_id=aid)
    return None


def _session_to_json_dict(sess: Session) -> dict[str, Any]:
    return {
        "session_id": sess.session_id,
        "service_id": sess.service_id,
        "agent_id": sess.agent_id,
    }


class SessionMap:
    """Map stable identity (per config scope) -> :class:`Session` (agent ``session_id`` + invoke ids).

    存储：Repository 已注入时走 PersistentStore；否则走 ``LocalSessionStorage``（``.checkpoint/session_map.json``）。
    不在 Gateway 侧分配 session_id；由 AgentServer ``session.create`` 创建后经 ``set_session_id`` 写入。
    """

    def __init__(self, *, scope: SessionMapScope | None = None) -> None:
        self._scope = scope if scope is not None else load_session_map_scope()
        self._storage = self._resolve_storage()

    @staticmethod
    def _resolve_storage() -> SessionStorage:
        from jiuwenswarm.gateway.routing.session_map_access import (
            PersistentSessionStorage,
            get_session_map_repository,
            session_map_read_through_enabled,
        )

        repo = get_session_map_repository()
        if repo is not None:
            return PersistentSessionStorage(
                repo,
                read_through=session_map_read_through_enabled(),
            )

        return LocalSessionStorage()

    def get_identity_key(
        self,
        provider: str,
        chat_id: str,
        bot_id: str,
        user_id: str,
    ) -> str:
        """Return the stable identity key for the configured scope."""
        return _make_key(self._scope, provider, chat_id, bot_id, user_id)

    def get_session(
        self,
        provider: str,
        chat_id: str,
        bot_id: str,
        user_id: str,
        *,
        rotate: bool = False,
    ) -> Session:
        """Return persisted Session; does not allocate new session IDs."""
        key = self.get_identity_key(provider, chat_id, bot_id, user_id)
        existing = self._storage.get(key)
        if existing and not rotate:
            return existing
        raise RuntimeError(
            "SessionMap cannot allocate session IDs; use AgentServer session.create"
        )

    def get_session_id(
        self,
        provider: str,
        chat_id: str,
        bot_id: str,
        user_id: str,
        *,
        rotate: bool = False,
    ) -> str:
        return self.get_session(
            provider, chat_id, bot_id, user_id, rotate=rotate
        ).session_id

    def find_session(
        self,
        provider: str,
        chat_id: str,
        bot_id: str,
        user_id: str,
    ) -> Session | None:
        key = self.get_identity_key(provider, chat_id, bot_id, user_id)
        return self._storage.get(key)

    def find_session_id(
        self,
        provider: str,
        chat_id: str,
        bot_id: str,
        user_id: str,
    ) -> str | None:
        sess = self.find_session(provider, chat_id, bot_id, user_id)
        return sess.session_id if sess is not None else None

    def set_session_id(
        self,
        provider: str,
        chat_id: str,
        bot_id: str,
        user_id: str,
        session_id: str,
    ) -> None:
        key = self.get_identity_key(provider, chat_id, bot_id, user_id)
        sid = str(session_id).strip()
        if not sid:
            raise ValueError("session_id is required")
        svc, aid = invoke_ids_from_identity(chat_id, bot_id, user_id, self._scope)
        sess = Session(session_id=sid, service_id=svc, agent_id=aid)
        existing = self._storage.get(key)
        if existing != sess:
            self._storage.set(key, sess)

    def reload(self) -> None:
        """从存储后端重新加载（如本地缓存需刷新时）。"""
        self._storage.load()
