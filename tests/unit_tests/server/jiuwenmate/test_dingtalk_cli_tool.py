from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.jiuwenmate.dingtalk_cli_runtime import DingTalkCliRuntimeService, DingTalkCliScope
from jiuwenswarm.server.jiuwenmate.dingtalk_cli_tool import (
    DingTalkCliCommitGuard,
    DingTalkCliTool,
    PROVIDER,
    build_dingtalk_cli_tool,
    dingtalk_execution_guidance,
    prepare_dingtalk_cli_invocation,
)
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError


@dataclass(frozen=True)
class _Execution:
    namespace_id: str = "local"
    agent_id: str = "employee-1"
    config_fingerprint: str = "original"


class _Spec:
    def __init__(self, tools=None):
        self.tools = tools or []

    def model_copy(self, *, update):
        return _Spec(update.get("tools", self.tools))

    def fingerprint(self):
        return "with-dingtalk-api-tool"


def test_dingtalk_execution_guidance_blocks_login_and_authorization_inference():
    guidance = dingtalk_execution_guidance(SimpleNamespace(channel="dingtalk"))

    assert guidance is not None
    assert "App Token" in guidance
    assert "已有的共享用户登录态" in guidance
    assert "不会启动登录或授权流程" in guidance
    assert "不得建议登录、OAuth、设备码" in guidance
    assert dingtalk_execution_guidance(SimpleNamespace(channel="feishu")) is None


class _SpecWithoutTools:
    def model_copy(self, *, update):
        return _Spec(update.get("tools", []))

    def fingerprint(self):
        return "with-dingtalk-api-tool"


class _Runtime:
    def __init__(self, bound):
        self.bound = bound

    def is_available(self, scope):
        assert (scope.namespace_id, scope.agent_id) == ("local", "employee-1")
        return self.bound

    async def execute(self, scope, argv):
        return {"ok": True}

    async def upload_group_file(self, scope, **kwargs):
        return {"uploaded": True, "dentryId": "dentry-77", **kwargs}

    async def create_calendar_event(self, scope, **kwargs):
        return {"created": True, "eventId": "event-91", **kwargs}

    async def remove_runtime(self, scope):
        return None


@pytest.mark.asyncio
async def test_employee_gets_the_shared_default_dingtalk_cli_tool_automatically():
    context = SimpleNamespace(execution=_Execution(), extras={})
    result = await prepare_dingtalk_cli_invocation(
        _Spec(), context, DingTalkCliRuntimeService(runtime=_Runtime(bound=True))
    )

    assert len(result.tools) == 1
    assert result.tools[0].type == PROVIDER
    assert context.execution.config_fingerprint == "with-dingtalk-api-tool"
    assert context.extras["jiuwenmate.dingtalk_cli"][1] == DingTalkCliScope("local", "employee-1")


@pytest.mark.asyncio
async def test_tool_injection_supports_specs_without_a_tools_field():
    context = SimpleNamespace(execution=_Execution(), extras={})

    result = await prepare_dingtalk_cli_invocation(
        _SpecWithoutTools(), context, DingTalkCliRuntimeService(runtime=_Runtime(bound=True))
    )

    assert len(result.tools) == 1
    assert result.tools[0].type == PROVIDER


@pytest.mark.asyncio
async def test_employee_does_not_get_dingtalk_cli_tool_without_a_default_app():
    spec = _Spec()
    context = SimpleNamespace(execution=_Execution(), extras={})

    result = await prepare_dingtalk_cli_invocation(
        spec, context, DingTalkCliRuntimeService(runtime=_Runtime(bound=False))
    )

    assert result is spec
    assert not context.extras


@pytest.mark.asyncio
async def test_group_request_exposes_only_trusted_dingtalk_group_context_to_the_cli_tool():
    context = SimpleNamespace(execution=_Execution(), extras={})
    request_input = SimpleNamespace(
        kind="group_message",
        reply_target="cid-from-input",
        source=SimpleNamespace(sender_id="staff-42"),
    )
    route = SimpleNamespace(
        channel="dingtalk",
        target_id="cid-current-group",
        app_id="dingtalk-app",
        source_message_id="message-1",
    )

    result = await prepare_dingtalk_cli_invocation(
        _Spec(), context, DingTalkCliRuntimeService(runtime=_Runtime(bound=True)),
        request_input=request_input, route_snapshot=route,
    )
    tool = build_dingtalk_cli_tool({}, context)
    description = result.tools[0].type + "\n" + tool._card.description

    assert context.extras["jiuwenmate.dingtalk_cli"][1].app_id == route.app_id

    assert "cid-current-group" in description
    assert "staff-42" in description
    assert "cid-from-input" not in description
    assert "knowledge-base workspace document" in description
    assert "senderStaffId" in description
    assert "operatorId" in description
    assert "existing shared user session on the service host" in description
    assert "never starts a login flow" in description
    assert "without guessing a permission name" in description
    assert "upload_group_file" in description


@pytest.mark.asyncio
async def test_direct_message_does_not_receive_group_document_context():
    context = SimpleNamespace(execution=_Execution(), extras={})
    request_input = SimpleNamespace(kind="direct_message", reply_target="staff-42", source=None)
    route = SimpleNamespace(
        channel="dingtalk", target_id="staff-42", app_id="dingtalk-app", source_message_id="message-1",
    )

    await prepare_dingtalk_cli_invocation(
        _Spec(), context, DingTalkCliRuntimeService(runtime=_Runtime(bound=True)),
        request_input=request_input, route_snapshot=route,
    )
    tool = build_dingtalk_cli_tool({}, context)

    assert "cid-current-group" not in tool._card.description


@pytest.mark.asyncio
@pytest.mark.parametrize("request_kind", ["direct_message", "group_message"])
async def test_calendar_event_creation_uses_trusted_sender_from_dingtalk_message(request_kind):
    class _CalendarRuntime(_Runtime):
        def __init__(self):
            super().__init__(bound=True)
            self.created = None

        async def create_calendar_event(self, scope, **kwargs):
            self.created = (scope, kwargs)
            return {"created": True, "eventId": "event-91"}

    runtime = _CalendarRuntime()
    context = SimpleNamespace(execution=_Execution(), extras={})
    request_input = SimpleNamespace(
        kind=request_kind,
        source=SimpleNamespace(sender_id="staff-42"),
    )
    target_id = "staff-42" if request_kind == "direct_message" else "cid-current-group"
    route = SimpleNamespace(channel="dingtalk", target_id=target_id, app_id="app-1")
    service = DingTalkCliRuntimeService(runtime=runtime)
    await prepare_dingtalk_cli_invocation(
        _Spec(), context, service, request_input=request_input, route_snapshot=route
    )

    result = await build_dingtalk_cli_tool({}, context).invoke({
        "operation": "create_calendar_event",
        "title": "项目评审",
        "start_at": "2026-10-29T10:30:00+08:00",
        "end_at": "2026-10-29T11:00:00+08:00",
        "time_zone": "Asia/Shanghai",
    })

    assert result == {"created": True, "eventId": "event-91"}
    assert runtime.created == (
        DingTalkCliScope("local", "employee-1", app_id="app-1"),
        {
            "sender_staff_id": "staff-42",
            "title": "项目评审",
            "start_at": "2026-10-29T10:30:00+08:00",
            "end_at": "2026-10-29T11:00:00+08:00",
            "time_zone": "Asia/Shanghai",
            "description": "",
            "reminder_minutes": None,
            "attendee_staff_ids": [],
            "create_video_meeting": False,
        },
    )


@pytest.mark.asyncio
async def test_calendar_event_can_include_members_of_only_the_trusted_current_group():
    class _CalendarRuntime(_Runtime):
        def __init__(self):
            super().__init__(bound=True)
            self.created = None

        async def create_calendar_event(self, scope, **kwargs):
            self.created = (scope, kwargs)
            return {"created": True, "eventId": "event-91", "attendeeCount": 3}

    runtime = _CalendarRuntime()
    context = SimpleNamespace(execution=_Execution(), extras={})
    request_input = SimpleNamespace(
        kind="group_message",
        source=SimpleNamespace(sender_id="staff-42"),
    )
    route = SimpleNamespace(channel="dingtalk", target_id="cid-current-group", app_id="app-1")
    await prepare_dingtalk_cli_invocation(
        _Spec(), context, DingTalkCliRuntimeService(runtime=runtime),
        request_input=request_input, route_snapshot=route,
    )

    result = await build_dingtalk_cli_tool({}, context).invoke({
        "operation": "create_calendar_event",
        "title": "全员会议",
        "start_at": "2026-10-29T10:30:00+08:00",
        "end_at": "2026-10-29T11:30:00+08:00",
        "time_zone": "Asia/Shanghai",
        "include_group_members": True,
    })

    assert result == {"created": True, "eventId": "event-91", "attendeeCount": 3}
    assert runtime.created == (
        DingTalkCliScope("local", "employee-1", app_id="app-1"),
        {
            "sender_staff_id": "staff-42",
            "title": "全员会议",
            "start_at": "2026-10-29T10:30:00+08:00",
            "end_at": "2026-10-29T11:30:00+08:00",
            "time_zone": "Asia/Shanghai",
            "description": "",
            "reminder_minutes": None,
            "attendee_staff_ids": [],
            "include_group_members": True,
            "conversation_id": "cid-current-group",
            "create_video_meeting": False,
        },
    )


@pytest.mark.asyncio
async def test_group_calendar_failure_replaces_model_reply_with_verified_result():
    class _DeniedRuntime(_Runtime):
        async def create_calendar_event(self, scope, **kwargs):
            raise PersistentAgentError(
                "钉钉拒绝 POST /v1.0/im/innerGroups/members/query",
                code="DINGTALK_CLI_API_FORBIDDEN",
            )

    class _CommitPort:
        def __init__(self):
            self.events = []

        async def record(self, execution, event):
            self.events.append(event)

        async def read_execution_log(self, execution, execution_id):
            return ()

    context = SimpleNamespace(execution=_Execution(), extras={})
    request_input = SimpleNamespace(
        kind="group_message", source=SimpleNamespace(sender_id="staff-42"),
    )
    route = SimpleNamespace(channel="dingtalk", target_id="cid-current-group", app_id="app-1")
    await prepare_dingtalk_cli_invocation(
        _Spec(), context, DingTalkCliRuntimeService(runtime=_DeniedRuntime(bound=True)),
        request_input=request_input, route_snapshot=route,
    )
    tool = build_dingtalk_cli_tool({}, context)
    result = await tool.invoke({
        "operation": "create_calendar_event",
        "title": "全员会议",
        "start_at": "2026-10-30T09:00:00+08:00",
        "end_at": "2026-10-30T10:00:00+08:00",
        "time_zone": "Asia/Shanghai",
        "include_group_members": True,
    })
    assert result["stop_retrying"] is True

    class _Payload:
        share_mode = "message"
        text = "请去开发者平台开权限，或者回复建。"

        def model_copy(self, *, update):
            return SimpleNamespace(share_mode=self.share_mode, text=update["text"])

    class _Event:
        event_type = "output.proposed"
        payload = _Payload()

        def model_copy(self, *, update):
            return SimpleNamespace(event_type=self.event_type, payload=update["payload"])

    port = _CommitPort()
    guard = DingTalkCliCommitGuard(port)
    guard.bind_context(context)
    await guard.record(context.execution, _Event())

    delivered_text = port.events[0].payload.text
    assert "DINGTALK_CLI_API_FORBIDDEN" in delivered_text
    assert "成员名单未取得" in delivered_text
    assert "日程未创建" in delivered_text
    assert "开发者平台" not in delivered_text
    assert "登录" not in delivered_text
    assert "授权" not in delivered_text


@pytest.mark.asyncio
async def test_calendar_event_creation_requires_complete_details_without_calling_api():
    class _CountingRuntime(_Runtime):
        def __init__(self):
            super().__init__(bound=True)
            self.calls = 0

        async def create_calendar_event(self, scope, **kwargs):
            self.calls += 1
            return {"created": True, "eventId": "unexpected"}

    runtime = _CountingRuntime()
    tool = DingTalkCliTool(
        DingTalkCliRuntimeService(runtime=runtime),
        DingTalkCliScope("local", "employee-1"),
        sender_staff_id="staff-42",
    )

    result = await tool.invoke({
        "operation": "create_calendar_event",
        "start_at": "2026-10-29T10:30:00+08:00",
        "end_at": "2026-10-29T11:00:00+08:00",
        "time_zone": "Asia/Shanghai",
    })

    assert result == {"error": "missing_calendar_event_fields", "missing_fields": ["title"]}
    assert runtime.calls == 0


@pytest.mark.asyncio
async def test_raw_calendar_create_api_is_blocked_in_favor_of_calendar_operation():
    class _CountingRuntime(_Runtime):
        def __init__(self):
            super().__init__(bound=True)
            self.calls = 0

        async def execute(self, scope, argv):
            self.calls += 1
            return {"unexpected": True}

    runtime = _CountingRuntime()
    tool = DingTalkCliTool(DingTalkCliRuntimeService(runtime=runtime), DingTalkCliScope("local", "employee-1"))

    result = await tool.invoke({
        "operation": "api",
        "argv": [
            "api", "POST", "/v1.0/calendar/users/union-42/calendars/primary/events",
            "--data", "{}",
        ],
    })

    assert runtime.calls == 0
    assert result["code"] == "DINGTALK_CLI_USE_CALENDAR_EVENT_OPERATION"
    assert result["stop_retrying"] is True


@pytest.mark.asyncio
async def test_raw_group_member_api_is_blocked_without_stopping_trusted_calendar_operation():
    class _CalendarRuntime(_Runtime):
        def __init__(self):
            super().__init__(bound=True)
            self.api_calls = 0
            self.created = None

        async def execute(self, scope, argv):
            self.api_calls += 1
            return {"unexpected": True}

        async def create_calendar_event(self, scope, **kwargs):
            self.created = (scope, kwargs)
            return {"created": True, "eventId": "event-91"}

    runtime = _CalendarRuntime()
    context = SimpleNamespace(execution=_Execution(), extras={})
    request_input = SimpleNamespace(kind="group_message", source=SimpleNamespace(sender_id="staff-42"))
    route = SimpleNamespace(channel="dingtalk", target_id="cid-current-group", app_id="app-1")
    await prepare_dingtalk_cli_invocation(
        _Spec(), context, DingTalkCliRuntimeService(runtime=runtime),
        request_input=request_input, route_snapshot=route,
    )
    tool = build_dingtalk_cli_tool({}, context)

    blocked = await tool.invoke({
        "operation": "api",
        "argv": ["api", "POST", "/v1.0/im/innerGroups/members/query"],
    })
    created = await tool.invoke({
        "operation": "create_calendar_event",
        "title": "全员会议",
        "start_at": "2026-10-29T10:30:00+08:00",
        "end_at": "2026-10-29T11:30:00+08:00",
        "time_zone": "Asia/Shanghai",
        "include_group_members": True,
    })

    assert blocked["code"] == "DINGTALK_CLI_GROUP_MEMBERS_USE_CALENDAR_OPERATION"
    assert "include_group_members=true" in blocked["error"]
    assert "stop_retrying" not in blocked
    assert runtime.api_calls == 0
    assert created == {"created": True, "eventId": "event-91"}
    assert runtime.created[1]["conversation_id"] == "cid-current-group"


@pytest.mark.asyncio
async def test_group_document_creation_uses_only_trusted_group_context():
    class _CreatingRuntime(_Runtime):
        def __init__(self):
            super().__init__(bound=True)
            self.created = None

        async def create_group_document(self, scope, **kwargs):
            self.created = (scope, kwargs)
            return {"created": True, "url": "https://alidocs.dingtalk.com/i/nodes/doc-1"}

    runtime = _CreatingRuntime()
    context = SimpleNamespace(execution=_Execution(), extras={})
    request_input = SimpleNamespace(
        kind="group_message",
        reply_target="cid-untrusted",
        source=SimpleNamespace(sender_id="staff-42"),
    )
    route = SimpleNamespace(channel="dingtalk", target_id="cid-trusted", app_id="app-1")
    service = DingTalkCliRuntimeService(runtime=runtime)
    await prepare_dingtalk_cli_invocation(
        _Spec(), context, service, request_input=request_input, route_snapshot=route
    )

    result = await build_dingtalk_cli_tool({}, context).invoke({
        "operation": "create_group_document", "title": "test", "content": "hello",
    })

    assert result["created"] is True
    assert runtime.created == (
        DingTalkCliScope("local", "employee-1", app_id="app-1"),
        {"conversation_id": "cid-trusted", "sender_staff_id": "staff-42", "title": "test", "content": "hello"},
    )


@pytest.mark.asyncio
async def test_group_file_upload_uses_trusted_group_and_sender_context():
    class _UploadingRuntime(_Runtime):
        def __init__(self):
            super().__init__(bound=True)
            self.uploaded = None

        async def upload_group_file(self, scope, **kwargs):
            self.uploaded = (scope, kwargs)
            return {"uploaded": True, "dentryId": "dentry-77"}

    runtime = _UploadingRuntime()
    context = SimpleNamespace(execution=_Execution(), extras={})
    request_input = SimpleNamespace(
        kind="group_message", source=SimpleNamespace(sender_id="staff-42"),
    )
    route = SimpleNamespace(channel="dingtalk", target_id="cid-trusted")
    service = DingTalkCliRuntimeService(runtime=runtime)
    await prepare_dingtalk_cli_invocation(
        _Spec(), context, service, request_input=request_input, route_snapshot=route
    )

    result = await build_dingtalk_cli_tool({}, context).invoke({
        "operation": "upload_group_file", "file_path": "output/ppttest.pptx",
    })

    assert result == {"uploaded": True, "dentryId": "dentry-77"}
    assert runtime.uploaded == (
        DingTalkCliScope("local", "employee-1"),
        {
            "conversation_id": "cid-trusted",
            "sender_staff_id": "staff-42",
            "file_path": "output/ppttest.pptx",
        },
    )


@pytest.mark.asyncio
async def test_raw_storage_upload_api_is_blocked_in_favor_of_group_upload_operation():
    class _CountingRuntime(_Runtime):
        def __init__(self):
            super().__init__(bound=True)
            self.calls = 0

        async def execute(self, scope, argv):
            self.calls += 1
            return {"unexpected": True}

    runtime = _CountingRuntime()
    tool = DingTalkCliTool(DingTalkCliRuntimeService(runtime=runtime), DingTalkCliScope("local", "employee-1"))

    result = await tool.invoke({
        "operation": "api",
        "argv": ["api", "POST", "/v1.0/storage/spaces/space-1/files/uploadInfos/query"],
    })

    assert runtime.calls == 0
    assert result["code"] == "DINGTALK_CLI_USE_GROUP_FILE_UPLOAD"
    assert result["stop_retrying"] is True


@pytest.mark.asyncio
async def test_group_document_creation_is_not_available_without_trusted_group_context():
    service = _Runtime(bound=True)
    tool = DingTalkCliTool(service, DingTalkCliScope("local", "employee-1"))

    result = await tool.invoke({"operation": "create_group_document", "title": "test"})

    assert result["error"] == "group_document_creation_requires_a_trusted_dingtalk_group_message"


@pytest.mark.asyncio
async def test_group_document_edit_uses_only_trusted_group_and_sender_context():
    class _EditingRuntime(_Runtime):
        def __init__(self):
            super().__init__(bound=True)
            self.edited = None

        async def edit_group_document(self, scope, **kwargs):
            self.edited = (scope, kwargs)
            return {"updated": True, "dentryId": kwargs["dentry_id"]}

    runtime = _EditingRuntime()
    context = SimpleNamespace(execution=_Execution(), extras={})
    request_input = SimpleNamespace(
        kind="group_message",
        reply_target="cid-untrusted",
        source=SimpleNamespace(sender_id="staff-42"),
    )
    route = SimpleNamespace(channel="dingtalk", target_id="cid-trusted", app_id="app-1")
    service = DingTalkCliRuntimeService(runtime=runtime)
    await prepare_dingtalk_cli_invocation(
        _Spec(), context, service, request_input=request_input, route_snapshot=route
    )

    result = await build_dingtalk_cli_tool({}, context).invoke({
        "operation": "edit_group_document", "dentry_id": "dentry-7", "content": "hello",
    })

    assert result == {"updated": True, "dentryId": "dentry-7"}
    assert runtime.edited == (
        DingTalkCliScope("local", "employee-1", app_id="app-1"),
        {
            "conversation_id": "cid-trusted", "sender_staff_id": "staff-42",
            "dentry_id": "dentry-7", "content": "hello",
        },
    )


@pytest.mark.asyncio
async def test_group_document_edit_is_not_available_without_trusted_group_context():
    service = _Runtime(bound=True)
    tool = DingTalkCliTool(service, DingTalkCliScope("local", "employee-1"))

    result = await tool.invoke({
        "operation": "edit_group_document", "dentry_id": "dentry-7", "content": "hello",
    })

    assert result["error"] == "group_document_edit_requires_a_trusted_dingtalk_group_message"


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code", [
    "DINGTALK_CLI_EXECUTION_ERROR", "DINGTALK_CLI_API_ERROR", "DINGTALK_CLI_API_FORBIDDEN",
])
async def test_cli_failure_stops_repeated_calls_for_the_same_execution(error_code):
    class _FailingService:
        def __init__(self):
            self.calls = 0

        async def execute(self, scope, argv):
            self.calls += 1
            raise PersistentAgentError("DingTalk app-token API request failed", code=error_code)

    service = _FailingService()
    tool = DingTalkCliTool(service, DingTalkCliScope("local", "employee-1"))
    inputs = {"argv": ["api", "GET", "/v1.0/doc/workspaces"]}

    first = await tool.invoke(inputs)
    second = await tool.invoke(inputs)

    assert service.calls == 1
    assert first["code"] == error_code
    assert first["stop_retrying"] is True
    assert second["stop_retrying"] is True


@pytest.mark.asyncio
async def test_cli_failure_stop_survives_tool_reconstruction_within_one_execution():
    class _FailingRuntime(_Runtime):
        def __init__(self):
            super().__init__(bound=True)
            self.calls = 0

        async def execute(self, scope, argv):
            self.calls += 1
            raise PersistentAgentError("DingTalk CLI package cache is busy", code="DINGTALK_CLI_CACHE_BUSY")

    runtime = _FailingRuntime()
    service = DingTalkCliRuntimeService(runtime=runtime)
    context = SimpleNamespace(execution=_Execution(), extras={})
    await prepare_dingtalk_cli_invocation(_Spec(), context, service)
    inputs = {"argv": ["api", "GET", "/v1.0/doc/workspaces"]}

    first_tool = build_dingtalk_cli_tool({}, context)
    first = await first_tool.invoke(inputs)
    rebuilt_tool = build_dingtalk_cli_tool({}, context)
    second = await rebuilt_tool.invoke(inputs)

    assert runtime.calls == 1
    assert first["code"] == "DINGTALK_CLI_CACHE_BUSY"
    assert first["stop_retrying"] is True
    assert second["code"] == "DINGTALK_CLI_CACHE_BUSY"
    assert second["stop_retrying"] is True


