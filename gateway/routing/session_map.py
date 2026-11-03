from __future__ import annotations

import json
from enum import Enum
from pathlib import Path

from gateway.common.paths import get_checkpoint_dir, logger


class SessionMapScope(str, Enum):
    """How SessionMap keys and agent session_id strings are derived from inbound identity."""

    # (default) One agent session per (provider, chat, bot); users in the same chat share context.
    PER_CHAT_BOT = "per_chat_bot"
    # One agent session per (provider, chat, bot, user)
    PER_CHAT_BOT_USER = "per_chat_bot_user"


def load_session_map_scope() -> SessionMapScope:
    default = SessionMapScope.PER_CHAT_BOT
    try:
        from gateway.common.config import get_config

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


class SessionMap:
    """Map stable identity (per config scope) -> rotating agent ``session_id``.

    - 个人版：读写 ``session_map.json``。
    - 企业版：启动时注入 ``SessionMapRepository`` 后，经存储屏蔽层读写 DB 表
      ``session_map``；进程内仍用 ``_mapping`` 做写穿缓存。
    """

    def __init__(self, *, scope: SessionMapScope | None = None) -> None:
        self._scope = scope if scope is not None else load_session_map_scope()
        self._store_path: Path = get_checkpoint_dir() / "session_map.json"
        self._mapping: dict[str, str] = {}
        if self._repository() is None:
            self._load()

    @staticmethod
    def _repository():
        from gateway.routing.session_map_access import get_session_map_repository

        return get_session_map_repository()

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

    def _persist_repository(self, identity_key: str, session_id: str) -> None:
        from gateway.routing.session_map_repository import Session
        from gateway.storage.async_bridge import run_awaitable

        repo = self._repository()
        if repo is None:
            return
        try:
            run_awaitable(
                repo.upsert(
                    identity_key,
                    Session(
                        session_id=session_id,
                        service_id=session_id,
                        agent_id=None,
                    ),
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("SessionMap repository upsert failed: %s", exc)

    def _lookup_repository(self, identity_key: str) -> str | None:
        from gateway.storage.async_bridge import run_awaitable

        repo = self._repository()
        if repo is None:
            return None
        try:
            session = run_awaitable(repo.get(identity_key))
        except Exception as exc:  # noqa: BLE001
            logger.warning("SessionMap repository get failed: %s", exc)
            return None
        if session is None:
            return None
        sid = str(session.session_id or "").strip()
        return sid or None

    def get_session_id(
        self,
        provider: str,
        chat_id: str,
        bot_id: str,
        user_id: str,
        *,
        rotate: bool = False,
    ) -> str:
        existing = self.find_session_id(provider, chat_id, bot_id, user_id)
        if existing and not rotate:
            return existing
        raise RuntimeError(
            "SessionMap cannot allocate session IDs; use AgentServer session.create"
        )

    def find_session_id(
        self,
        provider: str,
        chat_id: str,
        bot_id: str,
        user_id: str,
    ) -> str | None:
        key = _make_key(self._scope, provider, chat_id, bot_id, user_id)
        cached = self._mapping.get(key)
        if cached:
            return cached
        if self._repository() is None:
            return None
        sid = self._lookup_repository(key)
        if sid:
            self._mapping[key] = sid
        return sid

    def set_session_id(
        self,
        provider: str,
        chat_id: str,
        bot_id: str,
        user_id: str,
        session_id: str,
    ) -> None:
        key = _make_key(self._scope, provider, chat_id, bot_id, user_id)
        sid = str(session_id).strip()
        if not sid:
            raise ValueError("session_id is required")
        if self._mapping.get(key) == sid:
            return
        self._mapping[key] = sid
        if self._repository() is not None:
            self._persist_repository(key, sid)
        else:
            self._save()
