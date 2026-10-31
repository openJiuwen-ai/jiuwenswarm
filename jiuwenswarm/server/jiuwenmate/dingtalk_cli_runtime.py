"""DingTalk application APIs and employee OAuth operations."""

from __future__ import annotations

import importlib
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Protocol

from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError

_SCOPE_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_GROUP_HISTORY_READER = re.compile(r"[a-f0-9]{64}")
_FORBIDDEN_FLAGS = frozenset({
    "--client-id", "--client-secret", "--config", "--config-dir", "--token", "--app-secret",
})
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DingTalkCliScope:
    """Trusted employee identity; values are never used directly as filesystem paths."""

    namespace_id: str
    agent_id: str
    app_id: str | None = None

    def __post_init__(self) -> None:
        if not _SCOPE_PART.fullmatch(self.namespace_id) or not _SCOPE_PART.fullmatch(self.agent_id):
            raise ValueError("invalid DingTalk CLI scope")
        if self.app_id is not None and not _SCOPE_PART.fullmatch(self.app_id):
            raise ValueError("invalid DingTalk CLI application")


class DingTalkCliRuntimePort(Protocol):
    """Operations supported by the default app-token DWS CLI integration."""

    def is_available(self, scope: DingTalkCliScope) -> bool: ...

    async def execute(self, scope: DingTalkCliScope, argv: list[str]) -> dict[str, Any]: ...

    async def dingtalk_auth_status(self, scope: DingTalkCliScope) -> dict[str, Any]: ...

    async def dingtalk_auth_begin(self, scope: DingTalkCliScope) -> dict[str, Any]: ...

    async def dingtalk_auth_cancel(self, scope: DingTalkCliScope, operation_id: str) -> dict[str, Any]: ...

    async def dingtalk_auth_disconnect(self, scope: DingTalkCliScope) -> dict[str, Any]: ...

    async def group_history_identity(self, scope: DingTalkCliScope) -> str | None: ...

    async def read_group_history(
        self, scope: DingTalkCliScope, *, conversation_id: str, since: str,
    ) -> dict[str, Any]: ...

    async def search_online_documents(
        self, scope: DingTalkCliScope, *, query: str, cursor: str | None = None, limit: int = 10,
    ) -> dict[str, Any]: ...

    async def read_online_document(self, scope: DingTalkCliScope, *, node_id: str) -> dict[str, Any]: ...

    async def edit_online_document(
        self, scope: DingTalkCliScope, *, node_id: str, edit_mode: str,
        content: str | None = None, old_text: str | None = None, new_text: str | None = None,
    ) -> dict[str, Any]: ...

    async def create_group_document(
        self,
        scope: DingTalkCliScope,
        *,
        conversation_id: str,
        sender_staff_id: str,
        title: str,
        content: str = "",
    ) -> dict[str, Any]: ...

    async def edit_group_document(
        self,
        scope: DingTalkCliScope,
        *,
        conversation_id: str,
        sender_staff_id: str,
        dentry_id: str,
        content: str,
    ) -> dict[str, Any]: ...

    async def upload_group_file(
        self,
        scope: DingTalkCliScope,
        *,
        conversation_id: str,
        sender_staff_id: str,
        file_path: str,
    ) -> dict[str, Any]: ...

    async def update_calendar_event(
        self, scope: DingTalkCliScope, *, event_id: str, title: str,
    ) -> dict[str, Any]: ...

    async def create_calendar_event(
        self,
        scope: DingTalkCliScope,
        *,
        sender_staff_id: str,
        title: str,
        start_at: str,
        end_at: str,
        time_zone: str,
        description: str = "",
        reminder_minutes: int | None = None,
        attendee_staff_ids: list[str] | None = None,
        create_video_meeting: bool = False,
        include_group_members: bool = False,
        conversation_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def remove_runtime(self, scope: DingTalkCliScope) -> None: ...


def load_dingtalk_cli_runtime_provider() -> DingTalkCliRuntimePort | None:
    """Load the built-in provider or an explicitly configured trusted provider."""
    configured = os.environ.get(
        "JIUWENMATE_DINGTALK_CLI_RUNTIME_PROVIDER",
        "jiuwenswarm.server.jiuwenmate.dingtalk_cli_local_provider:create_provider",
    ).strip()
    if not configured:
        return None
    module_name, separator, factory_name = configured.partition(":")
    if not separator or not module_name or not factory_name:
        logger.warning("DingTalk CLI provider configuration is invalid")
        return None
    try:
        provider = getattr(importlib.import_module(module_name), factory_name)()
    except Exception as exc:
        logger.warning("DingTalk CLI provider could not be loaded: %s", type(exc).__name__)
        return None
    methods = ("is_available", "execute", "remove_runtime")
    if provider is None or not all(callable(getattr(provider, method, None)) for method in methods):
        logger.warning("DingTalk CLI provider does not implement the required app-token contract")
        return None
    return provider


class DingTalkCliRuntimeService:
    """Route DingTalk app and user operations through an employee-scoped provider."""

    def __init__(self, *, runtime: DingTalkCliRuntimePort | None = None) -> None:
        self._runtime = runtime

    def is_available(self, scope: DingTalkCliScope) -> bool:
        if self._runtime is None:
            return False
        try:
            return bool(self._runtime.is_available(scope))
        except Exception as exc:
            logger.warning("DingTalk CLI binding lookup failed (%s)", type(exc).__name__)
            return False

    async def dingtalk_auth_status(self, scope: DingTalkCliScope) -> dict[str, Any]:
        return await self._auth_call(scope, "dingtalk_auth_status")

    async def group_history_identity(self, scope: DingTalkCliScope) -> str | None:
        """Return only the authorized reader hash for host-owned group polling."""
        identity = getattr(self._runtime, "group_history_identity", None)
        if not callable(identity):
            return None
        try:
            if not self._runtime.is_available(scope):
                return None
            result = await identity(scope)
        except PersistentAgentError:
            raise
        except Exception as exc:
            logger.warning("DingTalk group history identity lookup failed (%s)", type(exc).__name__)
            raise PersistentAgentError(
                "DingTalk group history identity is unavailable", code="DINGTALK_CLI_GROUP_HISTORY_UNAVAILABLE",
            ) from exc
        if result is not None and (not isinstance(result, str) or not _GROUP_HISTORY_READER.fullmatch(result)):
            raise PersistentAgentError("Invalid DingTalk history identity", code="DINGTALK_CLI_GROUP_HISTORY_INVALID")
        return result

    async def read_group_history(
        self, scope: DingTalkCliScope, *, conversation_id: str, since: str,
    ) -> dict[str, Any]:
        """Read validated history only for targets selected by the host."""
        reader = getattr(self._runtime, "read_group_history", None)
        if not callable(reader):
            raise PersistentAgentError("DingTalk group history is unavailable", code="DINGTALK_CLI_GROUP_HISTORY_UNAVAILABLE")
        try:
            if not self._runtime.is_available(scope):
                raise PersistentAgentError("DingTalk group history is unavailable", code="DINGTALK_CLI_GROUP_HISTORY_UNAVAILABLE")
            result = await reader(scope, conversation_id=conversation_id, since=since)
        except PersistentAgentError:
            raise
        except Exception as exc:
            logger.warning("DingTalk group history read failed (%s)", type(exc).__name__)
            raise PersistentAgentError(
                "DingTalk group history could not be read", code="DINGTALK_CLI_GROUP_HISTORY_UNAVAILABLE",
            ) from exc
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("readerKey"), str)
            or not _GROUP_HISTORY_READER.fullmatch(result["readerKey"])
            or not isinstance(result.get("messages"), list)
            or not isinstance(result.get("complete"), bool)
            or not isinstance(result.get("hasMore"), bool)
            or not isinstance(result.get("botCodes"), list)
            or any(
                not isinstance(code, str) or not code or len(code) > 512
                or any(char.isspace() or ord(char) < 32 for char in code)
                for code in result["botCodes"]
            )
        ):
            raise PersistentAgentError("Invalid DingTalk group history response", code="DINGTALK_CLI_GROUP_HISTORY_INVALID")
        senders = result.get('botSenders', {})
        if (not isinstance(senders, dict) or any(
            code not in result['botCodes'] or not isinstance(sender, str) or not sender or len(sender) > 512
            or any(char.isspace() or ord(char) < 32 for char in sender)
            for code, sender in senders.items()
        ) or len(set(senders.values())) != len(senders)):
            raise PersistentAgentError("Invalid DingTalk bot sender identities", code="DINGTALK_CLI_GROUP_HISTORY_INVALID")
        fields = ("readerKey", "messages", "complete", "hasMore", "nextCursor", "botCodes", "botSenders")
        return {key: result[key] for key in fields if key in result}

    async def dingtalk_auth_begin(self, scope: DingTalkCliScope) -> dict[str, Any]:
        return await self._auth_call(scope, "dingtalk_auth_begin")

    async def dingtalk_auth_cancel(self, scope: DingTalkCliScope, operation_id: str) -> dict[str, Any]:
        return await self._auth_call(scope, "dingtalk_auth_cancel", operation_id)

    async def dingtalk_auth_disconnect(self, scope: DingTalkCliScope) -> dict[str, Any]:
        return await self._auth_call(scope, "dingtalk_auth_disconnect")

    async def _auth_call(self, scope: DingTalkCliScope, method: str, *args: Any) -> dict[str, Any]:
        if self._runtime is None or not self.is_available(scope):
            raise PersistentAgentError("DingTalk app is unavailable for this employee", code="DINGTALK_CLI_APP_NOT_CONFIGURED")
        call = getattr(self._runtime, method, None)
        if not callable(call):
            raise PersistentAgentError("DingTalk user authorization is unavailable", code="DINGTALK_CLI_AUTH_UNAVAILABLE")
        try:
            result = await call(scope, *args)
        except PersistentAgentError:
            raise
        except Exception as exc:
            logger.warning("DingTalk user authorization operation failed (%s)", type(exc).__name__)
            raise PersistentAgentError("DingTalk user authorization operation failed", code="DINGTALK_CLI_AUTH_ERROR") from exc
        if not isinstance(result, dict) or result.get("status") not in {"unavailable", "not_connected", "authorizing", "connected", "error"}:
            raise PersistentAgentError("invalid DingTalk authorization response", code="DINGTALK_CLI_RESPONSE_INVALID")
        safe = {"status": result["status"], "cli_version": result.get("cli_version")}
        if result['status'] == 'error' and result.get('error_code') in {
            'org_access_denied', 'authorization_expired', 'authorization_failed',
        }:
            safe['error_code'] = result['error_code']
        for key in ("operation_id", "auth_url", "user_code", "account", "organization"):
            value = result.get(key)
            if isinstance(value, str) and len(value) <= 2048:
                safe[key] = value
        auth_url = safe.get("auth_url")
        if auth_url is not None:
            from urllib.parse import urlsplit
            parsed = urlsplit(auth_url)
            if parsed.scheme != "https" or parsed.hostname not in {"login.dingtalk.com", "open.dingtalk.com"}:
                raise PersistentAgentError("invalid DingTalk authorization URL", code="DINGTALK_CLI_RESPONSE_INVALID")
        return safe

    async def execute(self, scope: DingTalkCliScope, argv: list[str]) -> dict[str, Any]:
        checked = self._validate_argv(argv)
        if self._runtime is None:
            raise PersistentAgentError("DingTalk CLI is unavailable", code="DINGTALK_CLI_RUNTIME_UNAVAILABLE")
        if not self.is_available(scope):
            raise PersistentAgentError(
                "No enabled DingTalk application is available for this employee",
                code="DINGTALK_CLI_APP_NOT_CONFIGURED",
            )
        try:
            result = await self._runtime.execute(scope, checked)
        except PersistentAgentError:
            raise
        except Exception as exc:
            logger.warning("DingTalk app-token API request failed (%s)", type(exc).__name__)
            raise PersistentAgentError("DingTalk app-token API request failed", code="DINGTALK_CLI_EXECUTION_ERROR") from exc
        if not isinstance(result, dict):
            raise PersistentAgentError("invalid DingTalk CLI response", code="DINGTALK_CLI_RESPONSE_INVALID")
        return result

    async def create_group_document(
        self,
        scope: DingTalkCliScope,
        *,
        conversation_id: str,
        sender_staff_id: str,
        title: str,
        content: str = "",
    ) -> dict[str, Any]:
        if self._runtime is None:
            raise PersistentAgentError("DingTalk CLI is unavailable", code="DINGTALK_CLI_RUNTIME_UNAVAILABLE")
        creator = getattr(self._runtime, "create_group_document", None)
        if not callable(creator):
            raise PersistentAgentError(
                "DingTalk runtime does not support creating group documents",
                code="DINGTALK_CLI_GROUP_DOC_UNAVAILABLE",
            )
        try:
            result = await creator(
                scope,
                conversation_id=conversation_id,
                sender_staff_id=sender_staff_id,
                title=title,
                content=content,
            )
        except PersistentAgentError:
            raise
        except Exception as exc:
            logger.warning("DingTalk group document creation failed (%s)", type(exc).__name__)
            raise PersistentAgentError(
                "DingTalk group document creation failed", code="DINGTALK_CLI_CREATE_GROUP_DOC_FAILED"
            ) from exc
        if not isinstance(result, dict):
            raise PersistentAgentError(
                "invalid DingTalk group document response", code="DINGTALK_CLI_RESPONSE_INVALID"
            )
        return result

    async def search_online_documents(
        self, scope: DingTalkCliScope, *, query: str, cursor: str | None = None, limit: int = 10,
    ) -> dict[str, Any]:
        if (
            not isinstance(query, str) or not query.strip() or len(query) > 512 or "\x00" in query
            or (cursor is not None and (
                not isinstance(cursor, str) or not cursor or len(cursor) > 2048 or "\x00" in cursor
            ))
            or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 30
        ):
            raise PersistentAgentError("Invalid online document search request", code="DINGTALK_CLI_ONLINE_DOC_REQUEST_INVALID")
        return await self._online_document_call(
            scope, "search_online_documents", {"query": query, "cursor": cursor, "limit": limit},
            "DINGTALK_CLI_ONLINE_DOC_SEARCH_FAILED",
        )

    async def read_online_document(self, scope: DingTalkCliScope, *, node_id: str) -> dict[str, Any]:
        self._validate_online_document_id(node_id)
        return await self._online_document_call(
            scope, "read_online_document", {"node_id": node_id}, "DINGTALK_CLI_ONLINE_DOC_READ_FAILED",
        )

    async def edit_online_document(
        self, scope: DingTalkCliScope, *, node_id: str, edit_mode: str,
        content: str | None = None, old_text: str | None = None, new_text: str | None = None,
    ) -> dict[str, Any]:
        self._validate_online_document_id(node_id)
        if edit_mode in ("append", "overwrite"):
            valid = isinstance(content, str) and bool(content) and len(content) <= 100_000
            valid = valid and old_text is None and new_text is None
        elif edit_mode == "replace_text":
            valid = isinstance(old_text, str) and bool(old_text) and len(old_text) <= 4000
            valid = valid and isinstance(new_text, str) and len(new_text) <= 4000 and content is None
        else:
            valid = False
        if not valid:
            raise PersistentAgentError("Invalid online document edit request", code="DINGTALK_CLI_ONLINE_DOC_REQUEST_INVALID")
        return await self._online_document_call(
            scope, "edit_online_document",
            {"node_id": node_id, "edit_mode": edit_mode, "content": content, "old_text": old_text, "new_text": new_text},
            "DINGTALK_CLI_ONLINE_DOC_EDIT_FAILED",
        )

    async def _online_document_call(
        self, scope: DingTalkCliScope, method: str, request: dict[str, Any], failure_code: str,
    ) -> dict[str, Any]:
        call = getattr(self._runtime, method, None)
        if not callable(call):
            raise PersistentAgentError("Online document operation is unavailable", code="DINGTALK_CLI_ONLINE_DOC_UNAVAILABLE")
        try:
            result = await call(scope, **request)
        except PersistentAgentError:
            raise
        except Exception as exc:
            logger.warning("DingTalk online document operation %s failed (%s)", method, type(exc).__name__)
            raise PersistentAgentError(
                "Online document operation was not confirmed; check the document before retrying", code=failure_code,
            ) from exc
        if not isinstance(result, dict):
            raise PersistentAgentError("Invalid online document response", code="DINGTALK_CLI_ONLINE_DOC_RESULT_INVALID")
        return result

    @staticmethod
    def _validate_online_document_id(node_id: str) -> None:
        if (
            not isinstance(node_id, str) or not node_id or len(node_id) > 512 or node_id.startswith("-")
            or any(char.isspace() or ord(char) < 32 for char in node_id)
        ):
            raise PersistentAgentError("Invalid online document identity", code="DINGTALK_CLI_ONLINE_DOC_REQUEST_INVALID")

    async def edit_group_document(
        self,
        scope: DingTalkCliScope,
        *,
        conversation_id: str,
        sender_staff_id: str,
        dentry_id: str,
        content: str,
    ) -> dict[str, Any]:
        if self._runtime is None:
            raise PersistentAgentError("DingTalk CLI is unavailable", code="DINGTALK_CLI_RUNTIME_UNAVAILABLE")
        editor = getattr(self._runtime, "edit_group_document", None)
        if not callable(editor):
            raise PersistentAgentError(
                "DingTalk runtime does not support editing group documents",
                code="DINGTALK_CLI_EDIT_GROUP_DOC_UNAVAILABLE",
            )
        try:
            result = await editor(
                scope,
                conversation_id=conversation_id,
                sender_staff_id=sender_staff_id,
                dentry_id=dentry_id,
                content=content,
            )
        except PersistentAgentError:
            raise
        except Exception as exc:
            logger.warning("DingTalk group document edit failed (%s)", type(exc).__name__)
            raise PersistentAgentError(
                "DingTalk group document edit failed", code="DINGTALK_CLI_EDIT_GROUP_DOC_FAILED"
            ) from exc
        if not isinstance(result, dict):
            raise PersistentAgentError(
                "invalid DingTalk group document edit response", code="DINGTALK_CLI_RESPONSE_INVALID"
            )
        return result

    async def upload_group_file(
        self,
        scope: DingTalkCliScope,
        *,
        conversation_id: str,
        sender_staff_id: str,
        file_path: str,
    ) -> dict[str, Any]:
        if self._runtime is None:
            raise PersistentAgentError("DingTalk CLI is unavailable", code="DINGTALK_CLI_RUNTIME_UNAVAILABLE")
        uploader = getattr(self._runtime, "upload_group_file", None)
        if not callable(uploader):
            raise PersistentAgentError(
                "DingTalk runtime does not support group file uploads",
                code="DINGTALK_CLI_GROUP_FILE_UPLOAD_UNAVAILABLE",
            )
        try:
            result = await uploader(
                scope,
                conversation_id=conversation_id,
                sender_staff_id=sender_staff_id,
                file_path=file_path,
            )
        except PersistentAgentError:
            raise
        except Exception as exc:
            logger.warning("DingTalk group file upload failed (%s)", type(exc).__name__)
            raise PersistentAgentError(
                "DingTalk group file upload failed", code="DINGTALK_CLI_UPLOAD_FAILED"
            ) from exc
        if not isinstance(result, dict):
            raise PersistentAgentError(
                "invalid DingTalk group file upload response", code="DINGTALK_CLI_RESPONSE_INVALID"
            )
        return result

    async def create_calendar_event(
        self,
        scope: DingTalkCliScope,
        *,
        sender_staff_id: str,
        title: str,
        start_at: str,
        end_at: str,
        time_zone: str,
        description: str = "",
        reminder_minutes: int | None = None,
        attendee_staff_ids: list[str] | None = None,
        create_video_meeting: bool = False,
        include_group_members: bool = False,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        if self._runtime is None:
            raise PersistentAgentError("DingTalk CLI is unavailable", code="DINGTALK_CLI_RUNTIME_UNAVAILABLE")
        creator = getattr(self._runtime, "create_calendar_event", None)
        if not callable(creator):
            raise PersistentAgentError(
                "DingTalk runtime does not support calendar event creation",
                code="DINGTALK_CLI_CALENDAR_UNAVAILABLE",
            )
        try:
            request = {
                "sender_staff_id": sender_staff_id,
                "title": title,
                "start_at": start_at,
                "end_at": end_at,
                "time_zone": time_zone,
                "description": description,
                "reminder_minutes": reminder_minutes,
                "attendee_staff_ids": attendee_staff_ids,
                "create_video_meeting": create_video_meeting,
            }
            if include_group_members:
                request["include_group_members"] = True
                request["conversation_id"] = conversation_id
            result = await creator(
                scope,
                **request,
            )
        except PersistentAgentError:
            raise
        except Exception as exc:
            logger.warning("DingTalk calendar event creation failed (%s)", type(exc).__name__)
            raise PersistentAgentError(
                "DingTalk calendar event creation failed; check the calendar before retrying",
                code="DINGTALK_CLI_CALENDAR_CREATE_FAILED",
            ) from exc
        if not isinstance(result, dict):
            raise PersistentAgentError(
                "invalid DingTalk calendar event response", code="DINGTALK_CLI_CALENDAR_RESULT_INVALID"
            )
        return result

    async def update_calendar_event(
        self, scope: DingTalkCliScope, *, event_id: str, title: str,
    ) -> dict[str, Any]:
        if not self.is_available(scope):
            raise PersistentAgentError("DingTalk app is unavailable", code="DINGTALK_CLI_APP_NOT_CONFIGURED")
        updater = getattr(self._runtime, "update_calendar_event", None)
        if not callable(updater):
            raise PersistentAgentError("DingTalk runtime does not support calendar updates", code="DINGTALK_CLI_CALENDAR_UNAVAILABLE")
        try:
            result = await updater(scope, event_id=event_id, title=title)
        except PersistentAgentError:
            raise
        except Exception as exc:
            logger.warning("DingTalk calendar update failed (%s)", type(exc).__name__)
            raise PersistentAgentError(
                "DingTalk calendar update was not confirmed; check the original event before retrying",
                code="DINGTALK_CLI_CALENDAR_UPDATE_FAILED",
            ) from exc
        if not isinstance(result, dict) or result.get("updated") is not True or result.get("eventId") != event_id:
            raise PersistentAgentError("invalid DingTalk calendar update response", code="DINGTALK_CLI_CALENDAR_UPDATE_RESULT_INVALID")
        return result

    async def remove(self, scope: DingTalkCliScope) -> None:
        if self._runtime is not None:
            await self._runtime.remove_runtime(scope)

    @staticmethod
    def _validate_argv(argv: list[str]) -> list[str]:
        if not isinstance(argv, list) or not argv or len(argv) > 128:
            raise PersistentAgentError("invalid DingTalk CLI arguments", code="BAD_REQUEST")
        if any(not isinstance(item, str) or not item or "\x00" in item or len(item) > 8192 for item in argv):
            raise PersistentAgentError("invalid DingTalk CLI arguments", code="BAD_REQUEST")
        if sum(len(item.encode("utf-8")) for item in argv) > 32_768:
            raise PersistentAgentError("DingTalk CLI arguments are too large", code="BAD_REQUEST")
        if argv[0].lower() != "api":
            raise PersistentAgentError(
                "DingTalk CLI is preconfigured for app-token API calls; user-login commands are unavailable",
                code="DINGTALK_CLI_COMMAND_UNSUPPORTED",
            )
        if any(item.split("=", 1)[0].lower() in _FORBIDDEN_FLAGS for item in argv):
            raise PersistentAgentError("DingTalk CLI credential and profile overrides are not allowed",
                                       code="DINGTALK_CLI_COMMAND_UNSUPPORTED")
        return list(argv)
