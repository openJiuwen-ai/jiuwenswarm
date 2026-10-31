"""Execution-scoped Dingtalk Workspace CLI tool routed by trusted employee identity."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit

from openjiuwen.core.foundation.tool.base import Tool, ToolCard
from openjiuwen.harness.schema.deep_agent_spec import BuiltinToolSpec, register_tool_provider
from openjiuwen.harness.persistent_agent import EventType

from jiuwenswarm.server.jiuwenmate.dingtalk_cli_runtime import DingTalkCliRuntimeService, DingTalkCliScope
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError

PROVIDER = "jiuwenmate.dingtalk_cli"
_CONTEXT_KEY = "jiuwenmate.dingtalk_cli"
_DINGTALK_EXECUTION_GUIDANCE = (
    "# 钉钉工具调用事实\n"
    "普通 OpenAPI 使用渠道配置的应用 App Token。全群日程读取群成员时，后端先调用应用接口；若该接口被拒绝，"
    "会自动改用官方 DWS `chat group members` 命令读取服务主机上已有的共享用户登录态。此回退不会启动登录或授权流程。\n"
    "聊天历史中的旧回复不是本次调用结果。用户明确要求重试时，本次必须调用 dingtalk_cli 一次；禁止通过 shell、Bash、Python、curl、npx 或 DWS 命令代替，也不得只复述历史。"
    "当 dingtalk_cli 返回 DINGTALK_CLI_API_FORBIDDEN 或 stop_retrying=true 时，停止本轮相关 API 操作。"
    "只有完整读取并核验当前发送者属于该群后才可创建日程。两种读取方式都失败时，只报告名单未取得完整、日程未创建；"
    "不得建议登录、OAuth、设备码、"
    "搜索或申请权限，也不得使用部分成员创建全群日程。若用户仍要继续，只询问其是否提供参会人 staffId 名单。"
)
_STOP_RETRY_ERROR_CODES = frozenset({
    "DINGTALK_CLI_API_FORBIDDEN", "DINGTALK_CLI_CACHE_BUSY",
    "DINGTALK_CLI_EXECUTION_ERROR", "DINGTALK_CLI_API_ERROR", "DINGTALK_CLI_TIMEOUT",
    "DINGTALK_CLI_IDENTITY_UNAVAILABLE", "DINGTALK_CLI_GROUP_SPACE_UNAVAILABLE",
    "DINGTALK_CLI_UPLOAD_INFO_INVALID", "DINGTALK_CLI_UPLOAD_FAILED",
    "DINGTALK_CLI_UPLOAD_FILE_INVALID", "DINGTALK_CLI_UPLOAD_RESULT_INVALID",
    "DINGTALK_CLI_GROUP_FILE_UPLOAD_UNAVAILABLE", "DINGTALK_CLI_USE_GROUP_FILE_UPLOAD",
    "DINGTALK_CLI_CREATE_RESULT_INVALID", "DINGTALK_CLI_CREATE_GROUP_DOC_FAILED",
    "DINGTALK_CLI_GROUP_DOC_NOT_FOUND", "DINGTALK_CLI_DOCUMENT_TYPE_UNSUPPORTED",
    "DINGTALK_CLI_DOCUMENT_CHANGED", "DINGTALK_CLI_UPDATE_RESULT_INVALID",
    "DINGTALK_CLI_EDIT_GROUP_DOC_FAILED",
    "DINGTALK_CLI_CALENDAR_RESULT_INVALID", "DINGTALK_CLI_CALENDAR_CREATE_FAILED",
    "DINGTALK_CLI_CALENDAR_UNAVAILABLE", "DINGTALK_CLI_IDENTITY_LOOKUP_FAILED",
    "DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE", "DINGTALK_CLI_CALENDAR_PARTICIPANTS_PARTIAL",
    "DINGTALK_CLI_CREATE_GROUP_CALENDAR_FAILED",
    "DINGTALK_CLI_USE_CALENDAR_EVENT_OPERATION",
})


@dataclass
class _DingTalkCliInvocationState:
    stopped_error: tuple[str, str] | None = None
    final_reply: str | None = None


class DingTalkCliCommitGuard:
    """Replace model claims after a failed all-members calendar request with the verified outcome."""

    def __init__(self, delegate) -> None:
        self._delegate = delegate
        self._state: _DingTalkCliInvocationState | None = None

    def bind_context(self, context: Any) -> None:
        invocation = getattr(context, "extras", {}).get(_CONTEXT_KEY)
        self._state = invocation[2] if isinstance(invocation, tuple) and len(invocation) > 2 else None

    async def record(self, execution: Any, event: Any) -> None:
        reply = self._state.final_reply if self._state is not None else None
        if reply and event.event_type == EventType.REPLY_DELTA:
            return
        if (
            reply
            and event.event_type == EventType.OUTPUT_PROPOSED
            and event.payload.share_mode == "message"
        ):
            payload = event.payload.model_copy(update={"text": reply})
            event = event.model_copy(update={"payload": payload})
        await self._delegate.record(execution, event)

    async def read_execution_log(self, execution: Any, execution_id: str):
        return await self._delegate.read_execution_log(execution, execution_id)


def _group_calendar_failure_reply(error: PersistentAgentError) -> str | None:
    endpoint = "/v1.0/im/innerGroups/members/query"
    if error.code != "DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE" and endpoint not in str(error):
        return None
    if error.code == "DINGTALK_CLI_API_FORBIDDEN" and endpoint in str(error):
        return (
            f"日程未创建：钉钉拒绝了群成员查询接口 POST {endpoint} "
            "（DINGTALK_CLI_API_FORBIDDEN），完整成员名单未取得。本次没有创建部分成员日程。"
            "如需继续创建，请提供完整参会人 staffId 列表。"
        )
    return (
        f"日程未创建：群成员查询失败（{error.code}），完整成员名单未取得；本次没有创建部分成员日程。"
        "如需继续创建，请提供完整参会人 staffId 列表。"
    )


@dataclass(frozen=True)
class _DingTalkGroupContext:
    conversation_id: str
    sender_staff_id: str


def _tool_inputs(inputs: Any) -> dict[str, Any]:
    if isinstance(inputs, dict):
        return inputs
    dump = getattr(inputs, "model_dump", None)
    if callable(dump):
        value = dump()
        if isinstance(value, dict):
            return value
    return {}


def _trusted_group_context(request_input: Any, route_snapshot: Any) -> _DingTalkGroupContext | None:
    if getattr(route_snapshot, "channel", None) != "dingtalk":
        return None
    if getattr(request_input, "kind", None) != "group_message":
        return None
    conversation_id = getattr(route_snapshot, "target_id", None)
    source = getattr(request_input, "source", None)
    sender_staff_id = getattr(source, "sender_id", None)
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        return None
    if not isinstance(sender_staff_id, str) or not sender_staff_id.strip():
        return None
    return _DingTalkGroupContext(conversation_id=conversation_id, sender_staff_id=sender_staff_id)


def _trusted_sender_staff_id(request_input: Any, route_snapshot: Any) -> str | None:
    if getattr(route_snapshot, "channel", None) != "dingtalk":
        return None
    if getattr(request_input, "kind", None) not in {"group_message", "direct_message"}:
        return None
    source = getattr(request_input, "source", None)
    sender_staff_id = getattr(source, "sender_id", None)
    if not isinstance(sender_staff_id, str) or not sender_staff_id.strip():
        return None
    return sender_staff_id.strip()


def dingtalk_execution_guidance(route_snapshot: Any) -> str | None:
    """Add high-priority, request-scoped facts for DingTalk App Token operations."""
    if getattr(route_snapshot, "channel", None) != "dingtalk":
        return None
    return _DINGTALK_EXECUTION_GUIDANCE


def _is_group_member_api(argv: Any) -> bool:
    if not isinstance(argv, list) or len(argv) < 3 or not isinstance(argv[2], str):
        return False
    target = argv[2]
    try:
        parsed = urlsplit(target if "://" in target else f"https://api.dingtalk.com{target}")
    except ValueError:
        return False
    endpoint = parsed.path.rstrip("/")
    return endpoint in {
        "/v1.0/im/innerGroups/members/query",
        "/v1.0/im/sceneGroups/members",
        "/v1.0/im/sceneGroups/members/batchQuery",
    }


class DingTalkCliTool(Tool):
    def __init__(
        self,
        service: DingTalkCliRuntimeService,
        scope: DingTalkCliScope,
        invocation_state: _DingTalkCliInvocationState | None = None,
        group_context: _DingTalkGroupContext | None = None,
        sender_staff_id: str | None = None,
    ) -> None:
        super().__init__(
            ToolCard(
                id="jiuwenmate-dingtalk-cli",
                name="dingtalk_cli",
                description=(
                    "Use the channel's configured DingTalk application and App Token. For raw OpenAPI calls, pass "
                    "`operation=api` and `dws api` arguments after `api` as argv, for example "
                    "['api', 'GET', '/v1.0/microApp/allApps']. To create a document in the current group, "
                    "use `operation=create_group_document` with `title` and optional plain-text `content`; the tool "
                    "resolves the trusted group and sender from the incoming message and uploads into that group's "
                    "file space. If DingTalk returns the root dentry UUID, it requests online-DOC conversion; otherwise "
                    "it creates a DOCX file and reports `documentType=docx_file`. This is a group document, "
                    "not a knowledge-base workspace document. To replace a DOCX created in this group, use "
                    "`operation=edit_group_document` with its `dentry_id` and the complete replacement `content`; "
                    "partial append is not supported. The tool verifies the file belongs to the current group. "
                    "The shared default application's credentials are configured automatically from the DingTalk "
                    "channel settings; no employee OAuth login is needed. Group document creation uses the current "
                    "sender's DingTalk user identity and can fail if the app lacks group-file-space read or storage "
                    "upload/write permissions. To put an existing employee output file in the current group's Files "
                    "list, use `operation=upload_group_file` with `file_path` like `output/report.pptx`; it uploads "
                    "the original bytes and returns a real `dentryId` only after DingTalk confirms the commit. "
                    "Do not call raw `uploadInfos/query` or `/files/commit` APIs for file uploads, and never claim "
                    "a file was uploaded unless this operation returns `uploaded=true` and a `dentryId`. "
                    "Reading a preview link is separate; if that fails, the result still "
                    "reports creation and marks `linkUnavailable`. Product commands such as `dws doc create` still "
                    "require user OAuth. "
                    "If DingTalk returns a missing scope, report it only when the scope is verified for that endpoint; "
                    "for the InnerGroup member query, report the endpoint and error code without guessing a permission "
                    "name or requesting authorization. Then stop; never claim the operation succeeded. If any call fails, explain the returned error instead "
                    "of probing DingTalk with Bash, curl, or another tool. If a tool result sets `stop_retrying` "
                    "to true, do not call this tool again during the request. To create a calendar event on the "
                    "trusted sender's primary calendar, use `operation=create_calendar_event` with a title, ISO-8601 "
                    "start/end values including their UTC offsets, and an IANA `time_zone`. Ask for a missing title or "
                    "end time; do not invent attendees. Attendees, description, reminder, and a DingTalk meeting link "
                    "are optional. The shared app-token path requires `Calendar.Event.Write` (日历应用中日程写权限) "
                    "and `qyapi_get_member` for mapping sender/attendee IDs; no employee OAuth login is needed. "
                    "Only report creation when the result contains `created=true` and a real `eventId`. For one shared "
                    "event with everyone in the current group, use `include_group_members=true` in a trusted group "
                    "message. The backend reads the current group roster using trusted conversation and sender IDs. "
                    "It first tries DingTalk's InnerGroup API with the configured app token; if DingTalk denies that "
                    "endpoint, it falls back to the official DWS `chat group members` command using only an existing "
                    "shared user session on the service host. The fallback never starts a login flow. It then invites "
                    "the other members after resolving the complete paginated roster. If both roster paths fail, "
                    "report that the complete roster was unavailable and do not create the event. Known "
                    "attendees can instead be passed explicitly. Never "
                    "query group-member endpoints through `operation=api`; those raw paths are blocked so the trusted "
                    "current group and member-query operation are used. Never guess or supply a different group ID; "
                    "this option is unavailable in direct messages."
                    + self._group_context_text(group_context)
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "operation": {"type": "string", "enum": ["api", "create_group_document", "edit_group_document", "upload_group_file", "create_calendar_event"]},
                        "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 128},
                        "title": {"type": "string", "maxLength": 120},
                        "content": {"type": "string", "maxLength": 100000},
                        "dentry_id": {"type": "string", "maxLength": 256},
                        "file_path": {"type": "string", "maxLength": 512},
                        "start_at": {"type": "string", "maxLength": 80},
                        "end_at": {"type": "string", "maxLength": 80},
                        "time_zone": {"type": "string", "maxLength": 128},
                        "description": {"type": "string", "maxLength": 4000},
                        "reminder_minutes": {"type": "integer", "minimum": 0, "maximum": 10080},
                        "attendee_staff_ids": {
                            "type": "array", "items": {"type": "string", "maxLength": 128}, "maxItems": 100,
                        },
                        "create_video_meeting": {"type": "boolean"},
                        "include_group_members": {"type": "boolean"},
                    },
                    "additionalProperties": False,
                },
                parallel_safe=False,
            )
        )
        self._service = service
        self._scope = scope
        self._invocation_state = invocation_state or _DingTalkCliInvocationState()
        self._group_context = group_context
        self._sender_staff_id = sender_staff_id.strip() if isinstance(sender_staff_id, str) else None

    @staticmethod
    def _group_context_text(group_context: _DingTalkGroupContext | None) -> str:
        if group_context is None:
            return ""
        return (
            "\n\nTrusted context from the accepted DingTalk group message (do not infer or substitute IDs):\n"
            f"- conversationId: {json.dumps(group_context.conversation_id, ensure_ascii=False)}\n"
            f"- senderStaffId: {json.dumps(group_context.sender_staff_id, ensure_ascii=False)}\n"
            "The conversationId is the current group target. senderStaffId is the organization user ID from the callback; "
            "it is not a verified unionId/operatorId."
        )

    async def invoke(self, inputs: Any, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        if self._invocation_state.stopped_error is not None:
            message, code = self._invocation_state.stopped_error
            return {"error": message, "code": code, "stop_retrying": True}
        payload = _tool_inputs(inputs)
        if payload.get("operation") == "create_calendar_event":
            if self._sender_staff_id is None:
                return {"error": "calendar_event_requires_a_trusted_dingtalk_message_sender"}
            required = ("title", "start_at", "end_at", "time_zone")
            missing = [key for key in required if not isinstance(payload.get(key), str) or not payload[key].strip()]
            if missing:
                return {"error": "missing_calendar_event_fields", "missing_fields": missing}
            description = payload.get("description", "")
            reminder_minutes = payload.get("reminder_minutes")
            attendee_staff_ids = payload.get("attendee_staff_ids", [])
            create_video_meeting = payload.get("create_video_meeting", False)
            include_group_members = payload.get("include_group_members", False)
            if (
                not isinstance(description, str)
                or (reminder_minutes is not None and (isinstance(reminder_minutes, bool) or not isinstance(reminder_minutes, int)))
                or not isinstance(attendee_staff_ids, list)
                or any(not isinstance(item, str) for item in attendee_staff_ids)
                or not isinstance(create_video_meeting, bool)
                or not isinstance(include_group_members, bool)
            ):
                return {"error": "invalid_calendar_event_request"}
            if include_group_members and self._group_context is None:
                return {"error": "group_calendar_event_requires_a_trusted_dingtalk_group_message"}
            try:
                calendar_request = {
                    "sender_staff_id": self._sender_staff_id,
                    "title": payload["title"],
                    "start_at": payload["start_at"],
                    "end_at": payload["end_at"],
                    "time_zone": payload["time_zone"],
                    "description": description,
                    "reminder_minutes": reminder_minutes,
                    "attendee_staff_ids": attendee_staff_ids,
                    "create_video_meeting": create_video_meeting,
                }
                if include_group_members:
                    calendar_request["include_group_members"] = True
                    calendar_request["conversation_id"] = self._group_context.conversation_id
                return await self._service.create_calendar_event(
                    self._scope,
                    **calendar_request,
                )
            except PersistentAgentError as exc:
                if include_group_members:
                    self._invocation_state.final_reply = _group_calendar_failure_reply(exc)
                if exc.code in _STOP_RETRY_ERROR_CODES or exc.code == "DINGTALK_CLI_API_FORBIDDEN":
                    self._invocation_state.stopped_error = (str(exc), exc.code)
                    return {"error": str(exc), "code": exc.code, "stop_retrying": True}
                return {"error": str(exc), "code": exc.code}
        if payload.get("operation") == "upload_group_file":
            if self._group_context is None:
                return {"error": "group_file_upload_requires_a_trusted_dingtalk_group_message"}
            file_path = payload.get("file_path")
            if not isinstance(file_path, str):
                return {"error": "invalid_group_file_upload_request"}
            try:
                return await self._service.upload_group_file(
                    self._scope,
                    conversation_id=self._group_context.conversation_id,
                    sender_staff_id=self._group_context.sender_staff_id,
                    file_path=file_path,
                )
            except PersistentAgentError as exc:
                if exc.code in _STOP_RETRY_ERROR_CODES:
                    self._invocation_state.stopped_error = (str(exc), exc.code)
                    return {"error": str(exc), "code": exc.code, "stop_retrying": True}
                return {"error": str(exc), "code": exc.code}
        if payload.get("operation") == "create_group_document":
            if self._group_context is None:
                return {"error": "group_document_creation_requires_a_trusted_dingtalk_group_message"}
            title = payload.get("title")
            content = payload.get("content", "")
            if not isinstance(title, str) or not isinstance(content, str):
                return {"error": "invalid_group_document_request"}
            try:
                result = await self._service.create_group_document(
                    self._scope,
                    conversation_id=self._group_context.conversation_id,
                    sender_staff_id=self._group_context.sender_staff_id,
                    title=title,
                    content=content,
                )
                return result
            except PersistentAgentError as exc:
                if exc.code in _STOP_RETRY_ERROR_CODES:
                    self._invocation_state.stopped_error = (str(exc), exc.code)
                    return {"error": str(exc), "code": exc.code, "stop_retrying": True}
                return {"error": str(exc), "code": exc.code}
        if payload.get("operation") == "edit_group_document":
            if self._group_context is None:
                return {"error": "group_document_edit_requires_a_trusted_dingtalk_group_message"}
            dentry_id = payload.get("dentry_id")
            content = payload.get("content")
            if not isinstance(dentry_id, str) or not isinstance(content, str):
                return {"error": "invalid_group_document_edit_request"}
            try:
                return await self._service.edit_group_document(
                    self._scope,
                    conversation_id=self._group_context.conversation_id,
                    sender_staff_id=self._group_context.sender_staff_id,
                    dentry_id=dentry_id,
                    content=content,
                )
            except PersistentAgentError as exc:
                if exc.code in _STOP_RETRY_ERROR_CODES:
                    self._invocation_state.stopped_error = (str(exc), exc.code)
                    return {"error": str(exc), "code": exc.code, "stop_retrying": True}
                return {"error": str(exc), "code": exc.code}
        argv = payload.get("argv")
        if not isinstance(argv, list):
            return {"error": "invalid_dingtalk_cli_request"}
        if payload.get("operation") == "api" and _is_group_member_api(argv):
            return {
                "error": (
                    "群成员查询不能通过原始 API 调用。若要把当前群成员加入一个日程，请使用 "
                    "operation=create_calendar_event 并设置 include_group_members=true；该操作从可信群消息上下文读取完整名单，"
                    "先尝试应用 App Token，若被拒绝则复用服务主机已有的共享 DWS 用户登录态，不会启动登录流程。"
                ),
                "code": "DINGTALK_CLI_GROUP_MEMBERS_USE_CALENDAR_OPERATION",
            }
        if len(argv) >= 3 and argv[0].lower() == "api" and isinstance(argv[2], str):
            api_path = argv[2].split("?", 1)[0].rstrip("/")
            method = argv[1].upper() if len(argv) > 1 else ""
            if (
                method == "POST"
                and (
                    (
                        api_path.startswith("/v1.0/calendar/users/")
                        and "/calendars/" in api_path
                        and api_path.endswith("/events")
                    )
                    or api_path == "/topapi/calendar/v2/event/create"
                )
            ):
                error = (
                    "Use operation=create_calendar_event to create calendar events; raw calendar writes are disabled."
                )
                code = "DINGTALK_CLI_USE_CALENDAR_EVENT_OPERATION"
                self._invocation_state.stopped_error = (error, code)
                return {"error": error, "code": code, "stop_retrying": True}
            if api_path.endswith(("/files/uploadInfos/query", "/files/commit")):
                error = (
                    "Use operation=upload_group_file with a shared output path for group file uploads; "
                    "raw upload API calls are disabled."
                )
                code = "DINGTALK_CLI_USE_GROUP_FILE_UPLOAD"
                self._invocation_state.stopped_error = (error, code)
                return {"error": error, "code": code, "stop_retrying": True}
        try:
            return await self._service.execute(self._scope, argv)
        except PersistentAgentError as exc:
            if exc.code in _STOP_RETRY_ERROR_CODES:
                self._invocation_state.stopped_error = (str(exc), exc.code)
                return {"error": str(exc), "code": exc.code, "stop_retrying": True}
            return {"error": str(exc), "code": exc.code}

    async def stream(self, inputs: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        yield await self.invoke(inputs, **kwargs)


async def prepare_dingtalk_cli_invocation(
    spec,
    context,
    service: DingTalkCliRuntimeService | None,
    *,
    request_input: Any = None,
    route_snapshot: Any = None,
):
    """Expose app-token API access automatically when a shared default DingTalk app is configured."""
    if service is None:
        return spec
    execution = context.execution
    app_id = getattr(route_snapshot, "app_id", None) if getattr(route_snapshot, "channel", None) == "dingtalk" else None
    scope = DingTalkCliScope(execution.namespace_id, execution.agent_id, app_id=app_id)
    if not await asyncio.to_thread(service.is_available, scope):
        return spec
    context.extras[_CONTEXT_KEY] = (
        service,
        scope,
        _DingTalkCliInvocationState(),
        _trusted_group_context(request_input, route_snapshot),
        _trusted_sender_staff_id(request_input, route_snapshot),
    )
    tools = list(getattr(spec, "tools", None) or [])
    if any(
        (item.get("type") if isinstance(item, dict) else getattr(item, "type", None)) == PROVIDER
        for item in tools
    ):
        raise PersistentAgentError("duplicate DingTalk CLI tool declaration", code="INVALID_CONFIG")
    runtime_spec = spec.model_copy(update={"tools": [*tools, BuiltinToolSpec(type=PROVIDER)]})
    context.execution = replace(context.execution, config_fingerprint=runtime_spec.fingerprint())
    return runtime_spec


def build_dingtalk_cli_tool(params: dict[str, Any], context):
    if params or _CONTEXT_KEY not in context.extras:
        raise ValueError("DingTalk CLI requires a connected employee runtime")
    service, scope, invocation_state, group_context, sender_staff_id = context.extras[_CONTEXT_KEY]
    return DingTalkCliTool(service, scope, invocation_state, group_context, sender_staff_id)


register_tool_provider(PROVIDER, build_dingtalk_cli_tool)
