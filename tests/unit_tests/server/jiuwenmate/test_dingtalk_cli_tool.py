from __future__ import annotations

from dataclasses import dataclass
import json
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
    assert '平台群历史可能不完整' in guidance
    assert '不能把未观察到的发言当成不存在' in guidance
    assert "当前员工已授权的独立钉钉账号" in guidance
    assert "不会启动登录或授权流程" in guidance
    assert "不得建议登录、OAuth、设备码" in guidance
    assert dingtalk_execution_guidance(SimpleNamespace(channel="feishu")) is None


def test_dingtalk_employee_identity_is_published_card_not_authorizing_user_or_sender():
    card = SimpleNamespace(id='employee-1', name='系统架构工程师')
    guidance = dingtalk_execution_guidance(SimpleNamespace(channel='dingtalk'), card=card)

    assert json.loads(guidance.split('```json\n', 1)[1].split('\n```', 1)[0]) == {
        'agent_id': 'employee-1', 'name': '系统架构工程师',
    }
    assert 'OAuth 授权账号仅提供数据访问权限' in guidance
    assert '消息发送者也不等于你' in guidance
    assert '自我介绍使用上述员工名称和已配置职责' in guidance
    assert '历史回复中的错误自称不得沿用' in guidance
    assert dingtalk_execution_guidance(SimpleNamespace(channel='feishu'), card=card) is None
    assert dingtalk_execution_guidance(None, card=card) is None


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
    assert "isolated user profile" in description
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
async def test_history_open_identity_is_not_used_as_a_calendar_staff_id():
    runtime = _Runtime(bound=True)
    context = SimpleNamespace(execution=_Execution(), extras={})
    await prepare_dingtalk_cli_invocation(
        _Spec(), context, DingTalkCliRuntimeService(runtime=runtime),
        request_input=SimpleNamespace(kind='group_message', source=SimpleNamespace(sender_id='dingtalk-open:person')),
        route_snapshot=SimpleNamespace(channel='dingtalk', target_id='cid-known', app_id='app-one'),
    )
    tool = build_dingtalk_cli_tool({}, context)
    result = await tool.invoke({
        'operation': 'create_calendar_event', 'title': 'Meeting',
        'start_at': '2026-10-31T10:00:00+08:00', 'end_at': '2026-10-31T11:00:00+08:00',
        'time_zone': 'Asia/Shanghai',
    })
    assert result == {'error': 'calendar_event_requires_a_trusted_dingtalk_message_sender'}
    assert 'senderOpenDingTalkId' in tool._card.description


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['upload_group_file', 'edit_group_document'])
async def test_history_open_identity_cannot_reach_staff_id_group_file_operations(operation):
    class NoStaffCalls(_Runtime):
        async def upload_group_file(self, *args, **kwargs):
            raise AssertionError('An open ID cannot be sent to a staffId operation')

        async def edit_group_document(self, *args, **kwargs):
            raise AssertionError('An open ID cannot be sent to a staffId operation')

    context = SimpleNamespace(execution=_Execution(), extras={})
    await prepare_dingtalk_cli_invocation(
        _Spec(), context, DingTalkCliRuntimeService(runtime=NoStaffCalls(bound=True)),
        request_input=SimpleNamespace(kind='group_message', source=SimpleNamespace(sender_id='dingtalk-open:person')),
        route_snapshot=SimpleNamespace(channel='dingtalk', target_id='cid-known', app_id='app-one'),
    )
    result = await build_dingtalk_cli_tool({}, context).invoke({
        'operation': operation, 'file_path': 'output/report.txt', 'dentry_id': 'file-one', 'content': 'new text',
    })
    assert result == {'error': 'group_file_operation_requires_a_trusted_dingtalk_staff_id'}


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
@pytest.mark.parametrize("endpoint", [
    "/v1.0/calendar/users/union-42/calendars/primary/events",
    "https://api.dingtalk.com/v1.0/calendar/users/union-42/calendars/primary/events?source=test",
    "https://oapi.dingtalk.com/topapi/calendar/v2/event/create",
])
async def test_raw_calendar_create_api_is_blocked_in_favor_of_calendar_operation(endpoint):
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
            "api", "POST", endpoint,
            "--data", "{}",
        ],
    })

    assert runtime.calls == 0
    assert result["code"] == "DINGTALK_CLI_USE_CALENDAR_EVENT_OPERATION"
    assert result["stop_retrying"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [None, "api"])
@pytest.mark.parametrize("endpoint", [
    "/v1.0/im/innerGroups/members/query",
    "https://api.dingtalk.com/v1.0/im/innerGroups/members/query?source=test",
    "https://api.dingtalk.com/v1.0/im/sceneGroups/members",
    "https://api.dingtalk.com/v1.0/im/sceneGroups/members/batchQuery",
])
async def test_raw_group_member_api_is_blocked_without_stopping_trusted_calendar_operation(operation, endpoint):
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

    inputs = {"argv": ["api", "POST", endpoint]}
    if operation is not None:
        inputs["operation"] = operation
    blocked = await tool.invoke(inputs)
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
async def test_group_document_missing_user_authorization_stops_repeated_calls():
    class _UnauthorizedRuntime(_Runtime):
        def __init__(self):
            super().__init__(bound=True)
            self.calls = 0

        async def create_group_document(self, scope, **kwargs):
            self.calls += 1
            raise PersistentAgentError(
                "请先在该 AI 员工的钉钉渠道设置中完成用户授权，再创建在线群文档",
                code="DINGTALK_CLI_USER_AUTH_REQUIRED",
            )

    runtime = _UnauthorizedRuntime()
    context = SimpleNamespace(execution=_Execution(), extras={})
    await prepare_dingtalk_cli_invocation(
        _Spec(), context, DingTalkCliRuntimeService(runtime=runtime),
        request_input=SimpleNamespace(kind="group_message", source=SimpleNamespace(sender_id="staff-42")),
        route_snapshot=SimpleNamespace(channel="dingtalk", target_id="cid-current-group"),
    )
    tool = build_dingtalk_cli_tool({}, context)
    inputs = {"operation": "create_group_document", "title": "test"}

    first = await tool.invoke(inputs)
    second = await tool.invoke(inputs)

    assert first["code"] == "DINGTALK_CLI_USER_AUTH_REQUIRED"
    assert first["stop_retrying"] is True
    assert second == first
    assert runtime.calls == 1


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




@pytest.mark.asyncio
async def test_calendar_title_update_uses_employee_scope_and_existing_event_id():
    class CalendarRuntime(_Runtime):
        async def update_calendar_event(self, scope, *, event_id, title):
            self.update = (scope, event_id, title)
            return {'updated': True, 'eventId': event_id, 'summary': title}
    runtime = CalendarRuntime(bound=True)
    context = SimpleNamespace(execution=_Execution(), extras={})
    await prepare_dingtalk_cli_invocation(
        _Spec(), context, DingTalkCliRuntimeService(runtime=runtime),
        request_input=SimpleNamespace(kind='group_message', source=SimpleNamespace(sender_id='staff-42')),
        route_snapshot=SimpleNamespace(channel='dingtalk', target_id='cid-current-group', app_id='app-1'),
    )
    tool = build_dingtalk_cli_tool({}, context)
    result = await tool.invoke({'operation': 'update_calendar_event', 'event_id': 'existing-event', 'title': 'AI同事讨论'})
    assert result == {'updated': True, 'eventId': 'existing-event', 'summary': 'AI同事讨论'}
    assert runtime.update == (DingTalkCliScope('local', 'employee-1', app_id='app-1'), 'existing-event', 'AI同事讨论')


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['PATCH', 'PUT', 'DELETE'])
async def test_raw_calendar_mutations_must_not_bypass_calendar_operations(method):
    runtime = _Runtime(bound=True)
    context = SimpleNamespace(execution=_Execution(), extras={})
    await prepare_dingtalk_cli_invocation(_Spec(), context, DingTalkCliRuntimeService(runtime=runtime))
    tool = build_dingtalk_cli_tool({}, context)
    result = await tool.invoke({'operation': 'api', 'argv': ['api', method, 'https://api.dingtalk.com/v1.0/calendar/users/staff/calendars/primary/events/existing-event', '--data', '{"summary":"new"}']})
    assert result['code'] == 'DINGTALK_CLI_USE_CALENDAR_EVENT_OPERATION'


@pytest.mark.asyncio
async def test_legacy_calendar_read_post_remains_available():
    context = SimpleNamespace(execution=_Execution(), extras={})
    await prepare_dingtalk_cli_invocation(_Spec(), context, DingTalkCliRuntimeService(runtime=_Runtime(bound=True)))
    result = await build_dingtalk_cli_tool({}, context).invoke({
        'operation': 'api', 'argv': ['api', 'POST', '/topapi/calendar/v2/event/get', '--data', '{}'],
    })
    assert result == {'ok': True}


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_id", ["employee-1", "employee-2"])
@pytest.mark.parametrize("operation,arguments,response", [
    ("search_online_documents", {"query": "项目", "cursor": "page-2", "limit": 5},
     {"documents": [{"nodeId": "node-1", "name": "项目", "url": "https://alidocs.dingtalk.com/i/nodes/node-1"}],
      "complete": True, "hasMore": False, "nextCursor": None}),
    ("read_online_document", {"node_id": "node-1"},
     {"nodeId": "node-1", "title": "项目", "content": "# 项目", "documentType": "online_doc"}),
    ("edit_online_document", {"node_id": "node-1", "edit_mode": "append", "content": "新内容",
                              "old_text": None, "new_text": None},
     {"updated": True, "nodeId": "node-1", "editMode": "append", "verified": True}),
    ("edit_online_document", {"node_id": "node-1", "edit_mode": "overwrite", "content": "完整内容",
                              "old_text": None, "new_text": None},
     {"updated": True, "nodeId": "node-1", "editMode": "overwrite", "verified": True}),
    ("edit_online_document", {"node_id": "node-1", "edit_mode": "replace_text", "content": None,
                              "old_text": "删除这句", "new_text": ""},
     {"updated": True, "nodeId": "node-1", "editMode": "replace_text", "verified": True}),
])
async def test_online_document_operations_use_employee_scope_without_message_sender(agent_id, operation, arguments, response):
    calls = []

    class OnlineRuntime:
        async def search_online_documents(self, scope, **params):
            calls.append(("search_online_documents", scope, params))
            return response

        async def read_online_document(self, scope, **params):
            calls.append(("read_online_document", scope, params))
            return response

        async def edit_online_document(self, scope, **params):
            calls.append(("edit_online_document", scope, params))
            return response

    scope = DingTalkCliScope("local", agent_id)
    tool = DingTalkCliTool(DingTalkCliRuntimeService(runtime=OnlineRuntime()), scope)

    result = await tool.invoke({"operation": operation, **arguments})

    assert result == response
    assert calls == [(operation, scope, arguments)]


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [
    {"operation": "search_online_documents", "query": ""},
    {"operation": "search_online_documents", "query": "x" * 513},
    {"operation": "search_online_documents", "query": "项目", "cursor": ""},
    {"operation": "search_online_documents", "query": "项目", "cursor": "x" * 2049},
    {"operation": "search_online_documents", "query": "项目", "limit": True},
    {"operation": "search_online_documents", "query": "项目", "limit": 0},
    {"operation": "search_online_documents", "query": "项目", "limit": 31},
    {"operation": "read_online_document", "node_id": ""},
    {"operation": "read_online_document", "node_id": "-node"},
    {"operation": "read_online_document", "node_id": "bad node"},
    {"operation": "read_online_document", "node_id": "x" * 513},
    {"operation": "edit_online_document", "node_id": "node-1", "edit_mode": "delete"},
    {"operation": "edit_online_document", "node_id": "node-1", "edit_mode": "append"},
    {"operation": "edit_online_document", "node_id": "node-1", "edit_mode": "overwrite", "content": ""},
    {"operation": "edit_online_document", "node_id": "node-1", "edit_mode": "append", "content": "x" * 100001},
    {"operation": "edit_online_document", "node_id": "node-1", "edit_mode": "append", "content": "new", "old_text": "old"},
    {"operation": "edit_online_document", "node_id": "node-1", "edit_mode": "replace_text", "old_text": "old"},
    {"operation": "edit_online_document", "node_id": "node-1", "edit_mode": "replace_text", "old_text": "", "new_text": "new"},
    {"operation": "edit_online_document", "node_id": "node-1", "edit_mode": "replace_text", "old_text": "old", "new_text": "new", "content": "all"},
])
async def test_online_document_invalid_requests_fail_before_provider_call(arguments):
    tool = DingTalkCliTool(DingTalkCliRuntimeService(runtime=object()), DingTalkCliScope("local", "employee-1"))

    result = await tool.invoke(arguments)

    assert result["code"] == "DINGTALK_CLI_ONLINE_DOC_REQUEST_INVALID"
    assert "stop_retrying" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["old_text", "new_text"])
async def test_online_document_replace_text_rejects_oversized_arguments_before_provider_call(field):
    calls = []

    class OnlineRuntime:
        async def edit_online_document(self, scope, **params):
            calls.append((scope, params))
            return {"updated": True, "nodeId": "node-1", "editMode": "replace_text", "verified": True}

    tool = DingTalkCliTool(DingTalkCliRuntimeService(runtime=OnlineRuntime()), DingTalkCliScope("local", "employee-1"))
    arguments = {
        "operation": "edit_online_document", "node_id": "node-1", "edit_mode": "replace_text",
        "old_text": "old", "new_text": "new",
    }
    arguments[field] = "x" * 4001

    result = await tool.invoke(arguments)

    assert result["code"] == "DINGTALK_CLI_ONLINE_DOC_REQUEST_INVALID"
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,arguments,failure_code", [
    ("search_online_documents", {"query": "项目"}, "DINGTALK_CLI_ONLINE_DOC_SEARCH_FAILED"),
    ("read_online_document", {"node_id": "node-1"}, "DINGTALK_CLI_ONLINE_DOC_READ_FAILED"),
    ("edit_online_document", {"node_id": "node-1", "edit_mode": "append", "content": "new"},
     "DINGTALK_CLI_ONLINE_DOC_EDIT_FAILED"),
])
@pytest.mark.parametrize("failure", ["exception", "invalid_response", "unavailable", "authorization"])
async def test_online_document_failures_stop_execution_retries(operation, arguments, failure_code, failure):
    calls = []

    async def call(scope, **params):
        calls.append((scope, params))
        if failure == "exception":
            raise RuntimeError("upstream secret must not be exposed")
        if failure == "authorization":
            raise PersistentAgentError("请先完成员工用户授权", code="DINGTALK_CLI_USER_AUTH_REQUIRED")
        return []

    runtime = SimpleNamespace()
    if failure != "unavailable":
        setattr(runtime, operation, call)
    tool = DingTalkCliTool(DingTalkCliRuntimeService(runtime=runtime), DingTalkCliScope("local", "employee-1"))

    first = await tool.invoke({"operation": operation, **arguments})
    second = await tool.invoke({"operation": operation, **arguments})

    expected_code = {
        "exception": failure_code,
        "invalid_response": "DINGTALK_CLI_ONLINE_DOC_RESULT_INVALID",
        "unavailable": "DINGTALK_CLI_ONLINE_DOC_UNAVAILABLE",
        "authorization": "DINGTALK_CLI_USER_AUTH_REQUIRED",
    }[failure]
    assert first["code"] == expected_code
    assert first["stop_retrying"] is True
    assert second == first
    assert len(calls) == (0 if failure == "unavailable" else 1)
    assert "upstream secret" not in first["error"]
