"""Employee-scoped Agent Mail runtime contract and secret-free status store."""

from __future__ import annotations

import importlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import closing
from typing import Any, Protocol
from urllib.parse import urlsplit

from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.sqlite_store import SQLiteStore

_SCOPE_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_STATES = frozenset({"unavailable", "not_connected", "authorizing", "connected", "error"})
_MAIL_OPERATIONS = frozenset({
    "mail.list", "mail.search", "mail.read", "mail.attachment.read",
    "mail.send", "mail.reply", "mail.forward", "mail.delete",
})
_PROVIDER_METHODS = (
    "status",
    "begin_auth",
    "poll_auth",
    "cancel_auth",
    "disconnect",
    "execute",
    "remove_runtime",
)
logger = logging.getLogger(__name__)


def load_agent_mail_runtime_provider() -> AgentMailRuntimePort | None:
    """Load the packaged local provider or a trusted host override; invalid configuration stays unavailable."""
    configured = os.environ.get(
        "JIUWENMATE_AGENT_MAIL_RUNTIME_PROVIDER",
        "jiuwenswarm.server.jiuwenmate.agent_mail_local_provider:create_provider",
    ).strip()
    if not configured:
        return None
    module_name, separator, factory_name = configured.partition(":")
    if not separator or not module_name or not factory_name:
        logger.warning("Agent Mail runtime provider configuration is invalid")
        return None
    try:
        factory = getattr(importlib.import_module(module_name), factory_name)
        provider = factory()
    except Exception as exc:
        logger.warning("Agent Mail runtime provider could not be loaded: %s", type(exc).__name__)
        return None
    if provider is None:
        return None
    if not all(callable(getattr(provider, method, None)) for method in _PROVIDER_METHODS):
        logger.warning("Agent Mail runtime provider does not implement the required lifecycle contract")
        return None
    return provider


@dataclass(frozen=True)
class AgentMailScope:
    """An identity passed to the employee-scoped runtime; it is never used as a filesystem path."""

    namespace_id: str
    agent_id: str

    def __post_init__(self) -> None:
        if not _SCOPE_PART.fullmatch(self.namespace_id) or not _SCOPE_PART.fullmatch(self.agent_id):
            raise ValueError("invalid Agent Mail scope")


class AgentMailRuntimePort(Protocol):
    """Operations supported by an employee-isolated Agent Mail runtime provider."""

    async def status(self, scope: AgentMailScope) -> dict[str, Any]: ...

    async def begin_auth(self, scope: AgentMailScope) -> dict[str, Any]: ...

    async def poll_auth(self, scope: AgentMailScope, operation_id: str) -> dict[str, Any]: ...

    async def cancel_auth(self, scope: AgentMailScope, operation_id: str) -> dict[str, Any]: ...

    async def disconnect(self, scope: AgentMailScope) -> dict[str, Any]: ...

    async def execute(self, scope: AgentMailScope, operation: str, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def remove_runtime(self, scope: AgentMailScope) -> None: ...


class AgentMailRuntimeStore(SQLiteStore):
    """Persists status metadata only; OAuth material, URLs and mail data stay in the employee runtime."""

    _schema = """
        CREATE TABLE IF NOT EXISTS agent_mail_runtime (
            namespace_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            status TEXT NOT NULL,
            cli_version TEXT,
            updated_at TEXT NOT NULL,
            error_code TEXT,
            PRIMARY KEY (namespace_id, agent_id)
        );
        CREATE TABLE IF NOT EXISTS agent_mail_bindings (
            namespace_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            email TEXT NOT NULL,
            email_key TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (namespace_id, agent_id),
            UNIQUE (namespace_id, email_key)
        );
    """

    def get(self, scope: AgentMailScope) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT status, cli_version, updated_at, error_code FROM agent_mail_runtime "
                "WHERE namespace_id = ? AND agent_id = ?",
                (scope.namespace_id, scope.agent_id),
            ).fetchone()
        return None if row is None else dict(row)

    def put(self, scope: AgentMailScope, status: str, cli_version: str | None, error_code: str | None) -> None:
        if status not in _STATES:
            raise ValueError("invalid Agent Mail runtime status")
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO agent_mail_runtime(namespace_id, agent_id, status, cli_version, updated_at, error_code) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(namespace_id, agent_id) DO UPDATE SET "
                "status=excluded.status, cli_version=excluded.cli_version, updated_at=excluded.updated_at, "
                "error_code=excluded.error_code",
                (scope.namespace_id, scope.agent_id, status, cli_version, updated_at, error_code),
            )

    def delete(self, scope: AgentMailScope) -> None:
        with self._transaction() as conn:
            conn.execute(
                "DELETE FROM agent_mail_runtime WHERE namespace_id = ? AND agent_id = ?",
                (scope.namespace_id, scope.agent_id),
            )
            conn.execute(
                "DELETE FROM agent_mail_bindings WHERE namespace_id = ? AND agent_id = ?",
                (scope.namespace_id, scope.agent_id),
            )

    def mailbox_owner(self, namespace_id: str, email: str) -> str | None:
        email_key = email.strip().casefold()
        with closing(self._connect()) as conn:
            owner = conn.execute(
                "SELECT agent_id FROM agent_mail_bindings WHERE namespace_id = ? AND email_key = ?",
                (namespace_id, email_key),
            ).fetchone()
        return None if owner is None else str(owner["agent_id"])

    def claim_mailbox(self, scope: AgentMailScope, email: str) -> None:
        """Move this mailbox onto the employee, releasing whoever held it before."""
        normalized_email = email.strip()
        email_key = normalized_email.casefold()
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._transaction() as conn:
            owner = conn.execute(
                "SELECT agent_id FROM agent_mail_bindings WHERE namespace_id = ? AND email_key = ?",
                (scope.namespace_id, email_key),
            ).fetchone()
            if owner is not None and owner["agent_id"] != scope.agent_id:
                conn.execute(
                    "DELETE FROM agent_mail_bindings WHERE namespace_id = ? AND agent_id = ?",
                    (scope.namespace_id, owner["agent_id"]),
                )
                conn.execute(
                    "UPDATE agent_mail_runtime SET status = ?, updated_at = ?, error_code = NULL "
                    "WHERE namespace_id = ? AND agent_id = ?",
                    ("not_connected", updated_at, scope.namespace_id, owner["agent_id"]),
                )
            conn.execute(
                "INSERT INTO agent_mail_bindings(namespace_id, agent_id, email, email_key, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(namespace_id, agent_id) DO UPDATE SET "
                "email=excluded.email, email_key=excluded.email_key, updated_at=excluded.updated_at",
                (scope.namespace_id, scope.agent_id, normalized_email, email_key, updated_at),
            )

    def release_mailbox(self, scope: AgentMailScope) -> None:
        with self._transaction() as conn:
            conn.execute(
                "DELETE FROM agent_mail_bindings WHERE namespace_id = ? AND agent_id = ?",
                (scope.namespace_id, scope.agent_id),
            )


class AgentMailRuntimeService:
    """Coordinates lifecycle calls and keeps only allowlisted status fields in Swarm."""

    def __init__(self, *, store: AgentMailRuntimeStore, runtime: AgentMailRuntimePort | None = None) -> None:
        self._store = store
        self._runtime = runtime

    @staticmethod
    def _safe_status(result: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise PersistentAgentError("invalid Agent Mail runtime response", code="AGENT_MAIL_RUNTIME_ERROR")
        status = result.get("status")
        if status not in _STATES:
            raise PersistentAgentError("invalid Agent Mail runtime response", code="AGENT_MAIL_RUNTIME_ERROR")
        version = result.get("cli_version")
        if version is not None and not isinstance(version, str):
            version = None
        safe = {"status": status, "cli_version": version}
        email = result.get("email")
        if status == "connected":
            if not isinstance(email, str) or "@" not in email or len(email.strip()) > 320:
                raise PersistentAgentError("Agent Mail runtime did not return a verified mailbox", code="AGENT_MAIL_RUNTIME_ERROR")
            safe["email"] = email.strip()
        operation_id = result.get("operation_id")
        if status == "authorizing":
            if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 512:
                raise PersistentAgentError("invalid Agent Mail runtime response", code="AGENT_MAIL_RUNTIME_ERROR")
            safe["operation_id"] = operation_id
        return safe

    def _require_runtime(self) -> AgentMailRuntimePort:
        if self._runtime is None:
            raise PersistentAgentError(
                "Agent Mail requires an employee runtime provider", code="AGENT_MAIL_RUNTIME_UNAVAILABLE"
            )
        return self._runtime

    @staticmethod
    async def _invoke(call):
        try:
            return await call
        except PersistentAgentError:
            raise
        except Exception as exc:
            raise PersistentAgentError("Agent Mail runtime operation failed", code="AGENT_MAIL_RUNTIME_ERROR") from exc

    async def status(self, scope: AgentMailScope) -> dict[str, Any]:
        if self._runtime is None:
            result = {"status": "unavailable", "cli_version": None}
        else:
            try:
                result = self._safe_status(await self._invoke(self._runtime.status(scope)))
            except Exception as exc:
                if isinstance(exc, PersistentAgentError):
                    raise
                self._store.put(scope, "error", None, "AGENT_MAIL_RUNTIME_ERROR")
                raise PersistentAgentError("Agent Mail runtime status failed", code="AGENT_MAIL_RUNTIME_ERROR") from exc
        return await self._record_status(scope, result)

    async def begin_auth(self, scope: AgentMailScope) -> dict[str, Any]:
        result = await self._invoke(self._require_runtime().begin_auth(scope))
        safe = self._safe_status(result)
        if safe["status"] == "connected":
            return await self._record_status(scope, safe)
        auth_url, operation_id = result.get("auth_url"), result.get("operation_id")
        parsed_url = urlsplit(auth_url) if isinstance(auth_url, str) else None
        trusted_host = parsed_url is not None and parsed_url.hostname is not None and (
            parsed_url.hostname == "qq.com"
            or parsed_url.hostname.endswith(".qq.com")
            or parsed_url.hostname == "tencent.com"
            or parsed_url.hostname.endswith(".tencent.com")
        )
        if (
            safe["status"] != "authorizing"
            or parsed_url is None
            or parsed_url.scheme != "https"
            or not trusted_host
            or len(auth_url) > 4096
            or not isinstance(operation_id, str)
            or not operation_id
            or len(operation_id) > 512
        ):
            raise PersistentAgentError("invalid Agent Mail authorization response", code="AGENT_MAIL_RUNTIME_ERROR")
        self._store.put(scope, safe["status"], safe.get("cli_version"), None)
        return {
            "status": safe["status"],
            "cli_version": safe.get("cli_version"),
            "operation_id": operation_id,
            "auth_url": auth_url,
            "frontend_opens_auth": result.get("frontend_opens_auth") is True,
        }

    async def poll_auth(self, scope: AgentMailScope, operation_id: str) -> dict[str, Any]:
        result = await self._invoke(self._require_runtime().poll_auth(scope, operation_id))
        safe = self._safe_status(result)
        return await self._record_status(scope, safe)

    async def cancel_auth(self, scope: AgentMailScope, operation_id: str) -> dict[str, Any]:
        result = self._safe_status(await self._invoke(self._require_runtime().cancel_auth(scope, operation_id)))
        return await self._record_status(scope, result)

    async def disconnect(self, scope: AgentMailScope) -> dict[str, Any]:
        result = self._safe_status(await self._invoke(self._require_runtime().disconnect(scope)))
        return await self._record_status(scope, result)

    async def _record_status(self, scope: AgentMailScope, result: dict[str, Any]) -> dict[str, Any]:
        status = result["status"]
        if status == "connected":
            email = result["email"]
            previous = self._store.mailbox_owner(scope.namespace_id, email)
            if previous is not None and previous != scope.agent_id and self._runtime is not None:
                try:
                    await self._invoke(self._runtime.disconnect(AgentMailScope(scope.namespace_id, previous)))
                except Exception as exc:
                    logger.warning(
                        "Could not disconnect previous Agent Mail mailbox for employee %s: %s",
                        previous,
                        type(exc).__name__,
                    )
            self._store.claim_mailbox(scope, email)
        elif status == "not_connected":
            self._store.release_mailbox(scope)
        self._store.put(scope, status, result.get("cli_version"), None)
        return result

    async def execute(self, scope: AgentMailScope, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation not in _MAIL_OPERATIONS:
            raise PersistentAgentError("unsupported Agent Mail operation", code="AGENT_MAIL_OPERATION_UNSUPPORTED")
        try:
            payload_size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise PersistentAgentError("invalid Agent Mail payload", code="BAD_REQUEST") from exc
        if payload_size > 16_384:
            raise PersistentAgentError("Agent Mail payload is too large", code="BAD_REQUEST")
        if self._runtime is None:
            self._require_runtime()
        current = await self.status(scope)
        if current["status"] != "connected":
            raise PersistentAgentError("Agent Mail is not connected", code="AGENT_MAIL_NOT_CONNECTED")
        result = await self._invoke(self._require_runtime().execute(scope, operation, payload))
        if not isinstance(result, dict):
            raise PersistentAgentError("invalid Agent Mail runtime response", code="AGENT_MAIL_RUNTIME_ERROR")
        try:
            result_size = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise PersistentAgentError("invalid Agent Mail runtime response", code="AGENT_MAIL_RUNTIME_ERROR") from exc
        if result_size > 262_144:
            raise PersistentAgentError("Agent Mail response is too large", code="AGENT_MAIL_RESPONSE_TOO_LARGE")
        return result

    async def remove(self, scope: AgentMailScope) -> None:
        if self._runtime is not None:
            await self._invoke(self._runtime.remove_runtime(scope))
        self._store.delete(scope)
