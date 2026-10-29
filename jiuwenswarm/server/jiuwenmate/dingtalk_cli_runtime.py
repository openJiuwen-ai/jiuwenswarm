"""DingTalk CLI access backed by the shared default channel application."""

from __future__ import annotations

import importlib
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Protocol

from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError

_SCOPE_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_FORBIDDEN_FLAGS = frozenset({
    "--client-id", "--client-secret", "--config", "--config-dir", "--token", "--app-secret",
})
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DingTalkCliScope:
    """Trusted employee identity; values are never used directly as filesystem paths."""

    namespace_id: str
    agent_id: str

    def __post_init__(self) -> None:
        if not _SCOPE_PART.fullmatch(self.namespace_id) or not _SCOPE_PART.fullmatch(self.agent_id):
            raise ValueError("invalid DingTalk CLI scope")


class DingTalkCliRuntimePort(Protocol):
    """Operations supported by the default app-token DWS CLI integration."""

    def is_available(self, scope: DingTalkCliScope) -> bool: ...

    async def execute(self, scope: DingTalkCliScope, argv: list[str]) -> dict[str, Any]: ...

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

    async def create_group_calendar_event(
        self,
        scope: DingTalkCliScope,
        *,
        conversation_id: str,
        sender_staff_id: str,
        summary: str,
        start_datetime: str,
        end_datetime: str,
        time_zone: str = "Asia/Shanghai",
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
    """Execute shared-app-token API requests with an employee-scoped local cache."""

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

    async def create_group_calendar_event(
        self,
        scope: DingTalkCliScope,
        *,
        conversation_id: str,
        sender_staff_id: str,
        summary: str,
        start_datetime: str,
        end_datetime: str,
        time_zone: str = "Asia/Shanghai",
    ) -> dict[str, Any]:
        if self._runtime is None:
            raise PersistentAgentError("DingTalk CLI is unavailable", code="DINGTALK_CLI_RUNTIME_UNAVAILABLE")
        creator = getattr(self._runtime, "create_group_calendar_event", None)
        if not callable(creator):
            raise PersistentAgentError(
                "DingTalk runtime does not support group calendar event creation",
                code="DINGTALK_CLI_CALENDAR_UNAVAILABLE",
            )
        try:
            result = await creator(
                scope,
                conversation_id=conversation_id,
                sender_staff_id=sender_staff_id,
                summary=summary,
                start_datetime=start_datetime,
                end_datetime=end_datetime,
                time_zone=time_zone,
            )
        except PersistentAgentError:
            raise
        except Exception as exc:
            logger.warning("DingTalk group calendar event creation failed (%s)", type(exc).__name__)
            raise PersistentAgentError(
                "DingTalk group calendar event creation failed; check the calendar before retrying",
                code="DINGTALK_CLI_CREATE_GROUP_CALENDAR_FAILED",
            ) from exc
        if not isinstance(result, dict):
            raise PersistentAgentError(
                "invalid DingTalk group calendar event response", code="DINGTALK_CLI_CALENDAR_RESULT_INVALID"
            )
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
