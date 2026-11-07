from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from jiuwenswarm.server.jiuwenmate.dingtalk_cli_runtime import DingTalkCliScope
from jiuwenswarm.server.jiuwenmate.dingtalk_cli_local_provider import LocalDingTalkCliRuntime
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError


def _online_document_runtime(tmp_path, monkeypatch, responses):
    from unittest.mock import AsyncMock

    calls = []

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        calls.append((args, input_text, env, cwd))
        assert args[args.index('--profile') + 1] == 'corp:employee'
        assert '/user-oauth/' in env['DWS_CONFIG_DIR']
        body = responses.pop(0)
        return subprocess.CompletedProcess(args, 0, json.dumps(body), '')

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app('employee-1', 'ding-app', 'secret', is_default=True)),
        dws_path=Path('/tools/dws'), runner=runner,
    )
    monkeypatch.setattr(runtime, 'dingtalk_auth_status', AsyncMock(return_value={
        'status': 'connected', '_profile': 'corp:employee',
    }))
    return runtime, calls


@pytest.mark.asyncio
async def test_group_history_reads_bounded_pages_with_employee_oauth(tmp_path, monkeypatch):
    runtime, calls = _online_document_runtime(tmp_path, monkeypatch, [
        {'success': True, 'result': {'bots': [
            {'robotCode': 'ding-app', 'botOpenDingTalkId': 'open-bot'}, {'robotCode': 'other-bot'},
        ]}},
        {
        'contractVersion': '1.0', 'complete': True, 'hasMore': False, 'messages': [{
            'messageId': 'platform-message', 'openMessageId': 'platform-message',
            'openConversationId': 'cid-known', 'senderOpenDingTalkId': 'open-person',
            'sender': 'Person', 'sendType': 'user', 'text': 'ordinary text',
            'createTime': '2026-10-30 22:00:01',
        }],
    }])
    result = await runtime.read_group_history(
        DingTalkCliScope('local', 'employee-1'), conversation_id='cid-known',
        since='2026-10-30T14:00:00.123+00:00',
    )
    assert calls[0][0][1:6] == ['chat', 'group', 'bots', '--group', 'cid-known']
    args = calls[1][0]
    assert args[1:4] == ['chat', 'message', 'list']
    assert args[args.index('--conversation-id') + 1] == 'cid-known'
    assert args[args.index('--time') + 1] == '2026-10-30 22:00:00.123'
    assert '--page-all' in args
    assert args[args.index('--page-limit') + 1] == '3'
    assert args[args.index('--max-items') + 1] == '150'
    assert result['messages'][0]['messageId'] == 'platform-message'
    assert result['complete'] is True
    assert result['hasMore'] is False
    assert len(result['readerKey']) == 64
    assert result['botCodes'] == ['ding-app', 'other-bot']
    assert result['botSenders'] == {'ding-app': 'open-bot'}


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['wrong_group', 'wrong_message_id', 'missing_collection'])
async def test_group_history_rejects_unverified_response_identity(tmp_path, monkeypatch, failure):
    payload = {'success': True, 'complete': True, 'hasMore': False, 'messages': [{
        'messageId': 'platform-message', 'openMessageId': 'platform-message',
        'openConversationId': 'cid-known', 'senderOpenDingTalkId': 'open-person',
        'sender': 'Person', 'sendType': 'user', 'text': 'ordinary text',
        'createTime': '2026-10-30 22:00:01',
    }]}
    if failure == 'wrong_group':
        payload['messages'][0]['openConversationId'] = 'cid-other'
    elif failure == 'wrong_message_id':
        payload['messages'][0]['openMessageId'] = 'different-message'
    else:
        payload.pop('messages')
    runtime, calls = _online_document_runtime(tmp_path, monkeypatch, [
        {'success': True, 'result': {'bots': [{'robotCode': 'ding-app'}]}}, payload,
    ])
    with pytest.raises(PersistentAgentError) as exc_info:
        await runtime.read_group_history(
            DingTalkCliScope('local', 'employee-1'), conversation_id='cid-known', since='2026-10-30T14:00:00+00:00',
        )
    assert exc_info.value.code == 'DINGTALK_CLI_GROUP_HISTORY_INVALID'
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_group_history_never_falls_back_to_host_shared_authorization(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    runtime, calls = _online_document_runtime(tmp_path, monkeypatch, [])
    monkeypatch.setattr(runtime, 'dingtalk_auth_status', AsyncMock(return_value={'status': 'not_connected'}))
    with pytest.raises(PersistentAgentError) as exc_info:
        await runtime.read_group_history(
            DingTalkCliScope('local', 'employee-1'), conversation_id='cid-known', since='2026-10-30T14:00:00+00:00',
        )
    assert exc_info.value.code == 'DINGTALK_CLI_USER_AUTH_REQUIRED'
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize('payload,error_code', [
    ({'success': True, 'result': {'bots': []}}, 'DINGTALK_CLI_GROUP_BOT_NOT_PRESENT'),
    ({'success': True, 'result': {'bots': [{'robotCode': 'other', 'openBotId': 'ding-app'}]}},
     'DINGTALK_CLI_GROUP_BOT_NOT_PRESENT'),
    ({'success': False, 'error': 'not allowed'}, 'DINGTALK_CLI_GROUP_HISTORY_UNAVAILABLE'),
    ({'success': True}, 'DINGTALK_CLI_GROUP_HISTORY_INVALID'),
    ({'success': True, 'result': {'bots': {}}}, 'DINGTALK_CLI_GROUP_HISTORY_INVALID'),
    ({'success': True, 'result': {'bots': [None]}}, 'DINGTALK_CLI_GROUP_HISTORY_INVALID'),
    ({'success': True, 'result': {'bots': [{'openBotId': 'ding-app'}]}}, 'DINGTALK_CLI_GROUP_HISTORY_INVALID'),
    ({'success': True, 'result': {'bots': [{'robotCode': ''}]}}, 'DINGTALK_CLI_GROUP_HISTORY_INVALID'),
    ({'success': True, 'result': {'bots': [{'robotCode': 123}]}}, 'DINGTALK_CLI_GROUP_HISTORY_INVALID'),
    ({'success': True, 'result': {'bots': [{'robotCode': 'ding-app'}, {'robotCode': 'invalid code'}]}},
     'DINGTALK_CLI_GROUP_HISTORY_INVALID'),
])
async def test_group_history_does_not_read_messages_without_verified_current_bot(tmp_path, monkeypatch, payload, error_code):
    runtime, calls = _online_document_runtime(tmp_path, monkeypatch, [payload])

    with pytest.raises(PersistentAgentError) as failure:
        await runtime.read_group_history(
            DingTalkCliScope('local', 'employee-1'), conversation_id='cid-known', since='2026-10-30T14:00:00+00:00',
        )

    assert failure.value.code == error_code
    assert len(calls) == 1
    assert calls[0][0][1:4] == ['chat', 'group', 'bots']


@pytest.mark.asyncio
@pytest.mark.parametrize('bot_code,present', [('scope-robot', True), ('default-robot', False), ('scope-app', False)])
async def test_group_history_checks_scope_robot_code_instead_of_default_app_or_client_id(tmp_path, monkeypatch, bot_code, present):
    responses = [{'success': True, 'result': {'bots': [{'robotCode': bot_code}]}}]
    if present:
        responses.append({'contractVersion': '1.0', 'messages': [], 'complete': True, 'hasMore': False})
    runtime, calls = _online_document_runtime(tmp_path, monkeypatch, responses)
    config = _config(
        {**_app('other-employee', 'default-app', 'default-secret', is_default=True), 'robot_code': 'default-robot'},
        {**_app('employee-1', 'scope-app', 'scope-secret'), 'robot_code': 'scope-robot'},
    )
    monkeypatch.setattr(runtime, '_config', lambda: config)
    operation = runtime.read_group_history(
        DingTalkCliScope('local', 'employee-1'), conversation_id='cid-known', since='2026-10-30T14:00:00+00:00',
    )

    if present:
        result = await operation
        assert result['botCodes'] == ['scope-robot']
    else:
        with pytest.raises(PersistentAgentError) as failure:
            await operation
        assert failure.value.code == 'DINGTALK_CLI_GROUP_BOT_NOT_PRESENT'
    assert len(calls) == (2 if present else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('scope_app', [None, {
    'persistent_agent_id': 'employee-1', 'client_id': 'disabled-app', 'client_secret': 'secret', 'enabled': False,
}])
async def test_group_history_never_uses_default_app_without_enabled_exact_binding(tmp_path, monkeypatch, scope_app):
    runtime, calls = _online_document_runtime(tmp_path, monkeypatch, [
        {'success': True, 'result': {'bots': [{'robotCode': 'default-app'}]}},
    ])
    apps = [_app('other-employee', 'default-app', 'secret', is_default=True)]
    if scope_app is not None:
        apps.append(scope_app)
    monkeypatch.setattr(runtime, '_config', lambda: _config(*apps))

    with pytest.raises(PersistentAgentError) as failure:
        await runtime.read_group_history(
            DingTalkCliScope('local', 'employee-1'), conversation_id='cid-known', since='2026-10-30T14:00:00+00:00',
        )

    assert failure.value.code == 'DINGTALK_CLI_GROUP_BOT_NOT_PRESENT'
    assert all(call[0][1:4] != ['chat', 'message', 'list'] for call in calls)


@pytest.mark.asyncio
async def test_online_document_search_preserves_candidates_and_partial_pagination(tmp_path, monkeypatch):
    documents = [
        {'nodeId': 'first-node', 'name': 'report', 'url': 'https://alidocs.dingtalk.com/i/nodes/first-node', 'docType': 'adoc'},
        {'nodeId': 'second-node', 'name': 'report', 'url': 'https://alidocs.dingtalk.com/i/nodes/second-node', 'docType': 'adoc'},
    ]
    runtime, calls = _online_document_runtime(tmp_path, monkeypatch, [{
        'status': 'success', 'documents': documents, 'complete': False,
        'hasMore': True, 'nextCursor': 'next-page', 'failures': [],
    }])

    result = await runtime.search_online_documents(
        DingTalkCliScope('local', 'employee-1'), query='report', cursor='previous-page', limit=10,
    )

    assert result['documents'] == documents
    assert result['complete'] is False
    assert result['hasMore'] is True
    assert result['nextCursor'] == 'next-page'
    assert calls[0][0][1:3] == ['doc', '+search']
    assert calls[0][0][calls[0][0].index('--extensions') + 1] == 'adoc'


@pytest.mark.asyncio
@pytest.mark.parametrize('wrong_node', [False, True])
async def test_online_document_read_checks_exact_identity(tmp_path, monkeypatch, wrong_node):
    runtime, _ = _online_document_runtime(tmp_path, monkeypatch, [{
        'status': 'success', 'complete': True, 'target': {'canonicalId': 'doc-one', 'product': 'doc'},
        'content': {'success': True, 'nodeId': 'other-doc' if wrong_node else 'doc-one', 'title': 'report', 'markdown': '# report'},
    }])
    operation = runtime.read_online_document(
        DingTalkCliScope('local', 'employee-1'), node_id='https://alidocs.dingtalk.com/i/nodes/doc-one',
    )
    if wrong_node:
        with pytest.raises(PersistentAgentError) as exc_info:
            await operation
        assert exc_info.value.code == 'DINGTALK_CLI_ONLINE_DOC_RESULT_INVALID'
    else:
        result = await operation
        assert result['nodeId'] == 'doc-one'
        assert result['content'] == '# report'
        assert result['documentType'] == 'online_doc'


@pytest.mark.asyncio
@pytest.mark.parametrize('node_id', ['--token=secret', 'https://evil.example/doc', '/tmp/private', 'abc\n--profile other'])
async def test_online_document_rejects_untrusted_targets_before_cli(tmp_path, monkeypatch, node_id):
    runtime, calls = _online_document_runtime(tmp_path, monkeypatch, [])
    with pytest.raises(PersistentAgentError) as exc_info:
        await runtime.read_online_document(DingTalkCliScope('local', 'employee-1'), node_id=node_id)
    assert exc_info.value.code == 'DINGTALK_CLI_ONLINE_DOC_REQUEST_INVALID'
    assert not calls


@pytest.mark.asyncio
async def test_online_document_requires_this_employees_user_authorization(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    runtime, calls = _online_document_runtime(tmp_path, monkeypatch, [])
    monkeypatch.setattr(runtime, 'dingtalk_auth_status', AsyncMock(return_value={'status': 'not_connected'}))
    with pytest.raises(PersistentAgentError) as exc_info:
        await runtime.search_online_documents(DingTalkCliScope('local', 'employee-1'), query='report')
    assert exc_info.value.code == 'DINGTALK_CLI_USER_AUTH_REQUIRED'
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize('verified', [True, False])
async def test_online_document_edit_uses_stdin_and_requires_verified_result(tmp_path, monkeypatch, verified):
    runtime, calls = _online_document_runtime(tmp_path, monkeypatch, [{
        'status': 'success', 'ok': True, 'complete': True,
        'data': {'nodeId': 'doc-one', 'verified': verified, 'verification': {'verified': verified}},
    }])
    operation = runtime.edit_online_document(
        DingTalkCliScope('local', 'employee-1'), node_id='doc-one', edit_mode='append', content='@private.txt\nNew text',
    )
    if verified:
        result = await operation
        assert result['updated'] is True
        assert result['nodeId'] == 'doc-one'
    else:
        with pytest.raises(PersistentAgentError) as exc_info:
            await operation
        assert exc_info.value.code == 'DINGTALK_CLI_ONLINE_DOC_RESULT_INVALID'
    args, text, _, _ = calls[0]
    assert args[1:3] == ['doc', '+update']
    assert args[args.index('--content') + 1] == '-'
    assert '--yes' in args
    assert text == '@private.txt\nNew text'
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['old_text', 'new_text'])
async def test_online_document_replacement_rejects_text_exceeding_portable_argv_budget(tmp_path, monkeypatch, field):
    runtime, calls = _online_document_runtime(tmp_path, monkeypatch, [])
    arguments = {'old_text': 'before', 'new_text': 'after'}
    arguments[field] = '大' * 4001
    with pytest.raises(PersistentAgentError) as exc_info:
        await runtime.edit_online_document(
            DingTalkCliScope('local', 'employee-1'), node_id='doc-one', edit_mode='replace_text', **arguments,
        )
    assert exc_info.value.code == 'DINGTALK_CLI_ONLINE_DOC_REQUEST_INVALID'
    assert not calls


@pytest.mark.asyncio
async def test_group_calendar_reads_members_with_app_token_without_user_login(tmp_path):
    calls = []

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        if args[1:3] == ["auth", "status"]:
            return subprocess.CompletedProcess(args, 0, json.dumps({"success": True, "authenticated": False}), "")
        del input_text, timeout, cwd
        calls.append(args)
        assert args[1:3] == ["api", "POST"]
        assert env["DWS_CLIENT_ID"] == "ding-app"
        assert env["DWS_CLIENT_SECRET"] == "secret"
        assert not env["DWS_CONFIG_DIR"].endswith("shared-user/dws")
        endpoint = args[3]
        request = json.loads(args[args.index("--data") + 1])
        if endpoint == "/v1.0/im/innerGroups/members/query":
            assert request["openConversationId"] == "cid-current-group"
            assert request["userId"] == "staff-42"
            assert request["maxResults"] == 200
            if request["nextToken"] == 0:
                body = {
                    "success": True,
                    "list": [{"userId": "staff-42"}, {"userId": "staff-7"}],
                    "hasMore": True,
                    "nextToken": 1,
                }
            else:
                assert request["nextToken"] == 1
                body = {
                    "success": True,
                    "list": [{"userId": "staff-8"}],
                    "hasMore": False,
                    "nextToken": 0,
                }
        elif endpoint == "/topapi/v2/user/get":
            body = {"result": {"unionid": f"union-{request['userid']}"}}
        elif endpoint == "/v1.0/calendar/users/union-staff-42/calendars/primary/events":
            event = request
            assert [item["id"] for item in event["attendees"]] == ["union-staff-7", "union-staff-8"]
            body = {"id": "event-app-token-1"}
        else:
            raise AssertionError(f"unexpected API request: {args}")
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        dws_path=Path("/tools/dws"),
        runner=runner,
    )

    result = await runtime.create_group_calendar_event(
        DingTalkCliScope("local", "employee-1"),
        conversation_id="cid-current-group",
        sender_staff_id="staff-42",
        summary="团队会议",
        start_datetime="2026-10-29T10:30:00+08:00",
        end_datetime="2026-10-29T11:30:00+08:00",
    )

    assert result["eventId"] == "event-app-token-1"
    assert result["attendeeCount"] == 2
    assert [call[3] for call in calls[:2]] == [
        "/v1.0/im/innerGroups/members/query",
        "/v1.0/im/innerGroups/members/query",
    ]
    assert len(calls) == 6


@pytest.mark.asyncio
async def test_group_calendar_stops_without_employee_authorization_on_app_token_denial(tmp_path):
    calls = []

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        if args[1:3] == ["auth", "status"]:
            return subprocess.CompletedProcess(args, 0, json.dumps({"success": True, "authenticated": False}), "")
        del env, input_text, timeout, cwd
        calls.append(args)
        if args[1:4] == ["api", "POST", "/v1.0/im/innerGroups/members/query"]:
            error = {"error": {"message": "requiredScopes=[Chat.OrgInnerGroup.Read]"}}
            return subprocess.CompletedProcess(args, 1, "", json.dumps(error))
        if args[1:4] == ["chat", "group", "members"]:
            error = {"error": {"message": "not logged in"}}
            return subprocess.CompletedProcess(args, 1, "", json.dumps(error))
        raise AssertionError("calendar and identity APIs must not run without a complete group roster")

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        dws_path=Path("/tools/dws"),
        runner=runner,
    )

    with pytest.raises(PersistentAgentError) as exc_info:
        await runtime.create_group_calendar_event(
            DingTalkCliScope("local", "employee-1"),
            conversation_id="cid-current-group",
            sender_staff_id="staff-42",
            summary="团队会议",
            start_datetime="2026-10-29T10:30:00+08:00",
            end_datetime="2026-10-29T11:30:00+08:00",
        )

    assert exc_info.value.code == "DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_create_group_calendar_event_does_not_create_when_member_lookup_fails(tmp_path):
    calls = []

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        if args[1:3] == ["auth", "status"]:
            return subprocess.CompletedProcess(args, 0, json.dumps({"success": True, "authenticated": False}), "")
        del env, input_text, timeout, cwd
        calls.append(args)
        if args[1:4] == ["api", "POST", "/v1.0/im/innerGroups/members/query"]:
            error = {"error": {"category": "api", "code": 1,
                               "message": "API business error (errcode: 400, HTTP 400): unavailable"}}
            return subprocess.CompletedProcess(args, 1, "", json.dumps(error))
        raise AssertionError("calendar APIs must not run when group membership lookup fails")

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        dws_path=Path("/tools/dws"),
        runner=runner,
    )

    with pytest.raises(PersistentAgentError) as exc_info:
        await runtime.create_group_calendar_event(
            DingTalkCliScope("local", "employee-1"),
            conversation_id="cid-current-group",
            sender_staff_id="staff-42",
            summary="团队会议",
            start_datetime="2026-10-29T10:30:00+08:00",
            end_datetime="2026-10-29T11:30:00+08:00",
        )

    assert exc_info.value.code == "DINGTALK_CLI_API_ERROR"
    assert "innerGroups/members/query" in str(exc_info.value)
    assert "未创建日程" in str(exc_info.value)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_create_group_calendar_event_does_not_create_with_malformed_member_rows(tmp_path):
    calls = []

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        if args[1:3] == ["auth", "status"]:
            return subprocess.CompletedProcess(args, 0, json.dumps({"success": True, "authenticated": False}), "")
        del env, input_text, timeout, cwd
        calls.append(args)
        if args[1:4] == ["api", "POST", "/v1.0/im/innerGroups/members/query"]:
            body = {"success": True, "list": [None], "hasMore": False, "nextToken": 0}
        else:
            raise AssertionError("calendar APIs must not run with malformed group members")
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        dws_path=Path("/tools/dws"),
        runner=runner,
    )

    with pytest.raises(PersistentAgentError) as exc_info:
        await runtime.create_group_calendar_event(
            DingTalkCliScope("local", "employee-1"),
            conversation_id="cid-current-group",
            sender_staff_id="staff-42",
            summary="团队会议",
            start_datetime="2026-10-29T10:30:00+08:00",
            end_datetime="2026-10-29T11:30:00+08:00",
        )

    assert exc_info.value.code == "DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE"
    assert "缺少 userId" in str(exc_info.value)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_execute_preserves_safe_upstream_error_code_for_diagnosis(tmp_path):
    private_request_id = "request-id-private"
    stderr = json.dumps({
        "error": {
            "category": "api",
            "code": 1,
            "message": f"API business error (errcode: 400, HTTP 400, requestId: {private_request_id}): invalid request",
        },
    })

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        return subprocess.CompletedProcess(args, 1, "", stderr)

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        dws_path=Path("/tools/dws"),
        runner=runner,
    )

    with pytest.raises(PersistentAgentError) as exc_info:
        await runtime.execute(
            DingTalkCliScope("local", "employee-1"),
            ["api", "POST", "/v1.0/im/innerGroups/members/query"],
        )

    assert exc_info.value.code == "DINGTALK_CLI_API_ERROR"
    assert "POST /v1.0/im/innerGroups/members/query" in str(exc_info.value)
    assert "errcode=400" in str(exc_info.value)
    assert private_request_id not in str(exc_info.value)


@pytest.mark.asyncio
async def test_edit_group_docx_replaces_content_in_the_same_group_file(tmp_path):
    calls = []

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        calls.append(args)
        path = args[3]
        if path == "/topapi/v2/user/get":
            body = {"result": {"unionid": "union-42"}}
        elif path == "/v1.0/convFile/conversations/spaces/query":
            body = {"space": {"spaceId": "group-space-9"}}
        elif path == "/v1.0/storage/spaces/group-space-9/dentries/dentry-7/query":
            body = {"dentry": {
                "id": "dentry-7", "spaceId": "group-space-9", "type": "FILE",
                "parentId": "0", "uuid": "doc-uuid-7", "name": "test.docx",
                "extension": "docx", "storageDriver": "DINGTALK", "version": 1,
            }}
        elif path == "/v1.0/storage/spaces/group-space-9/files/uploadInfos/query":
            body = {
                "uploadKey": "upload-key",
                "headerSignatureInfo": {
                    "resourceUrls": ["https://storage.aliyuncs.com/signed-upload?secret=private"],
                    "headers": {"x-oss-signature": "signed-header"},
                },
            }
        elif path == "/v1.0/storage/spaces/group-space-9/files/commit":
            body = {"dentry": {
                "id": "dentry-7", "uuid": "doc-uuid-7", "name": "test.docx", "version": 2,
            }}
        else:
            raise AssertionError(f"unexpected API request: {args}")
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

    uploaded = []

    async def upload(url, headers, data):
        uploaded.append((url, headers, data))

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        dws_path=Path("/tools/dws"),
        runner=runner,
        signed_upload=upload,
    )

    result = await runtime.edit_group_document(
        DingTalkCliScope("local", "employee-1"),
        conversation_id="cid-current-group",
        sender_staff_id="staff-42",
        dentry_id="dentry-7",
        content="已写入内容",
    )

    assert result == {
        "updated": True, "dentryId": "dentry-7", "dentryUuid": "doc-uuid-7",
        "version": 2, "documentType": "docx_file",
    }
    assert len(calls) == 6
    upload_args = calls[3]
    assert upload_args[1:3] == ["api", "POST"]
    assert upload_args[3] == "/v1.0/storage/spaces/group-space-9/files/uploadInfos/query"
    upload_payload = json.loads(upload_args[upload_args.index("--data") + 1])
    assert upload_payload["option"]["preCheckParam"]["parentId"] == "0"
    assert upload_payload["option"]["preCheckParam"]["name"] == "test.docx"
    commit_args = calls[5]
    commit_payload = json.loads(commit_args[commit_args.index("--data") + 1])
    assert commit_payload["parentId"] == "0"
    assert commit_payload["name"] == "test.docx"
    assert commit_payload["option"]["conflictStrategy"] == "OVERWRITE"
    assert uploaded[0][0] == "https://storage.aliyuncs.com/signed-upload?secret=private"
    assert uploaded[0][1] == {"x-oss-signature": "signed-header"}
    assert uploaded[0][2].startswith(b"PK")


@pytest.mark.asyncio
async def test_edit_group_document_rejects_a_dentry_outside_the_current_group(tmp_path):
    calls = []

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        calls.append(args)
        path = args[3]
        if path == "/topapi/v2/user/get":
            body = {"result": {"unionid": "union-42"}}
        elif path == "/v1.0/convFile/conversations/spaces/query":
            body = {"space": {"spaceId": "group-space-9"}}
        elif path == "/v1.0/storage/spaces/group-space-9/dentries/dentry-7/query":
            body = {"dentry": {
                "id": "dentry-7", "spaceId": "another-group-space", "type": "FILE",
                "parentId": "0", "uuid": "doc-uuid-7", "name": "test.docx",
                "extension": "docx", "storageDriver": "DINGTALK", "version": 1,
            }}
        else:
            raise AssertionError(f"unexpected API request: {args}")
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        dws_path=Path("/tools/dws"),
        runner=runner,
    )

    with pytest.raises(PersistentAgentError) as exc_info:
        await runtime.edit_group_document(
            DingTalkCliScope("local", "employee-1"),
            conversation_id="cid-current-group",
            sender_staff_id="staff-42",
            dentry_id="dentry-7",
            content="must not be written",
        )

    assert exc_info.value.code == "DINGTALK_CLI_GROUP_DOC_NOT_FOUND"
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_edit_group_document_stops_if_the_file_changes_during_upload(tmp_path):
    calls = []
    dentry_queries = 0

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        nonlocal dentry_queries
        calls.append(args)
        path = args[3]
        if path == "/topapi/v2/user/get":
            body = {"result": {"unionid": "union-42"}}
        elif path == "/v1.0/convFile/conversations/spaces/query":
            body = {"space": {"spaceId": "group-space-9"}}
        elif path == "/v1.0/storage/spaces/group-space-9/dentries/dentry-7/query":
            dentry_queries += 1
            body = {"dentry": {
                "id": "dentry-7", "spaceId": "group-space-9", "type": "FILE",
                "parentId": "0", "uuid": "doc-uuid-7", "name": "test.docx",
                "extension": "docx", "storageDriver": "DINGTALK",
                "version": 1 if dentry_queries == 1 else 2,
            }}
        elif path == "/v1.0/storage/spaces/group-space-9/files/uploadInfos/query":
            body = {
                "uploadKey": "upload-key",
                "headerSignatureInfo": {
                    "resourceUrls": ["https://storage.aliyuncs.com/signed-upload"],
                    "headers": {"x-oss-signature": "signed-header"},
                },
            }
        else:
            raise AssertionError(f"unexpected API request after concurrent update: {args}")
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

    async def upload(url, headers, data):
        return None

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        dws_path=Path("/tools/dws"),
        runner=runner,
        signed_upload=upload,
    )

    with pytest.raises(PersistentAgentError) as exc_info:
        await runtime.edit_group_document(
            DingTalkCliScope("local", "employee-1"),
            conversation_id="cid-current-group",
            sender_staff_id="staff-42",
            dentry_id="dentry-7",
            content="must not overwrite a concurrent edit",
        )

    assert exc_info.value.code == "DINGTALK_CLI_DOCUMENT_CHANGED"
    assert len(calls) == 5




@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "roster", "page", "identity", "create", "missing_event"])
async def test_group_calendar_uses_employee_oauth_open_ids(tmp_path, monkeypatch, failure):
    calls = []

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        calls.append(args)
        assert "user-oauth" in env["DWS_CONFIG_DIR"]
        assert "--profile" in args
        if args[1:4] == ["chat", "group", "members"]:
            cursor = args[args.index("--cursor") + 1]
            if failure == "roster":
                return subprocess.CompletedProcess(args, 1, "", "not logged in")
            if cursor == "0":
                body = {"success": True, "result": {"list": [{"openDingtalkId": "open-owner"}], "hasMore": True, "nextCursor": "page-2"}}
            else:
                member = {} if failure == "identity" else {"openDingtalkId": "open-external"}
                body = {"success": True, "result": {"list": [member], "hasMore": failure == "page", "nextCursor": "page-2"}}
        elif args[1:4] == ["calendar", "event", "create"]:
            assert args[args.index("--open-dingtalk-ids") + 1] == "open-owner,open-external"
            assert args[args.index("--attendees") + 1] == "extra-staff"
            assert args[args.index("--remind-minutes") + 1] == "15"
            assert "--add-online-meeting=false" in args
            if failure == "create":
                return subprocess.CompletedProcess(args, 1, "", "request interrupted")
            body = {"success": True, "result": {} if failure == "missing_event" else {"eventId": "event-user-1"}}
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        dws_path=Path("/tools/dws"), runner=runner,
    )
    async def auth_status(scope):
        return {"status": "connected", "_user_id": "owner-staff", "_profile": "corp:owner-staff"}
    monkeypatch.setattr(runtime, "dingtalk_auth_status", auth_status)
    operation = runtime.create_group_calendar_event(
        DingTalkCliScope("local", "employee-1"), conversation_id="cid-current-group",
        sender_staff_id="another-requester", summary="团队会议",
        start_datetime="2026-10-31T10:00:00+08:00", end_datetime="2026-10-31T10:30:00+08:00",
        reminder_minutes=15, extra_attendee_staff_ids=["extra-staff"],
    )
    if failure:
        with pytest.raises(PersistentAgentError):
            await operation
    else:
        result = await operation
        assert result["eventId"] == "event-user-1"
        assert result["groupMemberCount"] == 2
    writes = [args for args in calls if args[1:4] == ["calendar", "event", "create"]]
    assert len(writes) == (0 if failure in {"roster", "page", "identity"} else 1)


def _config(*apps):
    return {"channels": {"dingtalk": {"apps": list(apps)}}}


def _app(agent_id, client_id, secret, *, enabled=True, is_default=False):
    return {
        "persistent_agent_id": agent_id,
        "client_id": client_id,
        "client_secret": secret,
        "enabled": enabled,
        "is_default": is_default,
    }


@pytest.mark.parametrize("authenticated", [True, False])
def test_dingtalk_auth_status_reads_cli_top_level_authentication(tmp_path, authenticated):
    calls = []

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        calls.append(args)
        if args[1:3] == ["auth", "status"]:
            payload = {"success": True, "authenticated": authenticated}
        elif args[1:3] == ["profile", "list"]:
            payload = {"success": True, "profiles": [{"current": True, "userName": "tester"}]}
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        dws_path=Path("/tools/dws"),
        runner=runner,
    )

    status = runtime._dingtalk_auth_status(DingTalkCliScope("local", "employee-1"))

    assert status["status"] == ("connected" if authenticated else "not_connected")
    if authenticated:
        assert status["account"] == "tester"
        assert len(calls) == 2
    else:
        assert len(calls) == 1



@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    None, "listing", "second_page", "group_type", "wrong_workspace", "missing_workspace",
    "wrong_title", "wrong_node", "wrong_storage_space", "wrong_storage_node", "missing_dentry", "stalled_page",
])
async def test_online_group_document_creates_directly_in_group_without_sending_user_message(tmp_path, failure):
    calls = []

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        calls.append(args)
        command = args[1:3]
        if command == ["auth", "status"]:
            body = {"success": True, "authenticated": True}
        elif command == ["profile", "list"]:
            body = {"profiles": [{"isCurrent": True, "userId": "authorized-user", "userName": "tester"}]}
        elif command == ["doc", "create"]:
            assert "user-oauth" in env["DWS_CONFIG_DIR"]
            assert args[args.index("--name") + 1] == "test"
            assert args[args.index("--workspace") + 1] == "group-space"
            assert "--folder" not in args
            body = {"success": True, "nodeId": "doc-uuid", "docUrl": "https://alidocs.dingtalk.com/i/nodes/doc-uuid"}
        elif command == ["drive", "info"]:
            node = args[args.index("--node") + 1]
            kind = "FILE" if failure == "group_type" else "ALIDOC"
            workspace = "other-space" if failure == "wrong_workspace" else "group-space"
            body = {"success": True, "result": {
                "type": kind, "extension": "adoc", "nodeId": "other-node" if failure == "wrong_node" else node,
                "name": "wrong" if failure == "wrong_title" else "test", "workspaceId": workspace,
            }}
            if failure == "missing_workspace":
                body["result"].pop("workspaceId")
        elif args[3] == "/topapi/v2/user/get":
            assert json.loads(args[args.index("--data") + 1])["userid"] == "authorized-user"
            body = {"result": {"unionid": "authorized-union"}}
        elif args[3] == "/v1.0/convFile/conversations/spaces/query":
            body = {"space": {"spaceId": "group-space"}}
        elif args[3] == "/v2.0/doc/dentries/doc-uuid/queryDentryId":
            body = {
                "dentryId": None if failure == "missing_dentry" else "group-file",
                "dentryUuid": "other-node" if failure == "wrong_storage_node" else "doc-uuid",
                "spaceId": "other-space" if failure == "wrong_storage_space" else "group-space",
            }
        elif args[3] == "/v1.0/storage/spaces/group-space/dentries":
            params = json.loads(args[args.index("--params") + 1])
            if failure == "stalled_page" or (failure == "second_page" and not params.get("nextToken")):
                body = {"dentries": [], "nextToken": "next-page"}
            else:
                body = {"dentries": [] if failure == "listing" else [{"id": "group-file", "spaceId": "group-space"}]}
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        dws_path=Path("/tools/dws"),
        runner=runner,
    )
    operation = runtime.create_group_document(
        DingTalkCliScope("local", "employee-1"), conversation_id="cid-current-group",
        sender_staff_id="different-requester", title="test", content="",
    )
    if failure not in {None, "second_page"}:
        with pytest.raises(PersistentAgentError) as exc_info:
            await operation
        assert "https://alidocs.dingtalk.com/i/nodes/doc-uuid" in str(exc_info.value)
    else:
        result = await operation
        assert result["created"] is True
        assert result["documentType"] == "online_doc"
        assert result["dentryId"] == "group-file"
        assert result["dentryUuid"] == result["documentNodeId"] == "doc-uuid"
    assert sum(args[1:3] == ["doc", "create"] for args in calls) == 1
    assert not any('/links/send' in str(arg) or str(arg).endswith('/rename') for args in calls for arg in args)
    assert not any(args[1:3] == ['chat', 'message'] for args in calls)


def test_runtime_shares_the_default_application_with_every_employee(tmp_path):
    config = _config(
        _app("employee-1", "default-app", "default-secret", is_default=True),
        _app("employee-2", "employee-app", "employee-secret"),
        _app("employee-3", "disabled-app", "disabled-secret", enabled=False),
    )
    runtime = LocalDingTalkCliRuntime(root=tmp_path, config_loader=lambda: config, dws_path=Path("/tools/dws"))

    first_env = runtime.environment(DingTalkCliScope("local", "employee-1"))
    second_env = runtime.environment(DingTalkCliScope("local", "employee-2"))
    third_env = runtime.environment(DingTalkCliScope("local", "employee-3"))

    assert runtime.is_available(DingTalkCliScope("local", "employee-1"))
    assert runtime.is_available(DingTalkCliScope("local", "employee-2"))
    assert first_env["DWS_CLIENT_ID"] == "default-app"
    assert first_env["DWS_CLIENT_SECRET"] == "default-secret"
    assert second_env["DWS_CLIENT_ID"] == "default-app"
    assert second_env["DWS_CLIENT_SECRET"] == "default-secret"
    assert third_env["DWS_CLIENT_ID"] == "default-app"
    assert first_env["DWS_CONFIG_DIR"] != second_env["DWS_CONFIG_DIR"]
    assert first_env["DWS_CACHE_DIR"].startswith(first_env["DWS_CONFIG_DIR"])
    assert first_env["npm_config_cache"] == second_env["npm_config_cache"]
    assert first_env["npm_config_cache"].replace("\\", "/").endswith("/npm-cache/1.0.65")
    assert first_env["DWS_DISABLE_KEYCHAIN"] == "1"


def test_runtime_uses_the_employee_bound_application_when_no_shared_default_exists(tmp_path):
    config = _config(
        _app("employee-1", "employee-one-app", "employee-one-secret"),
        _app("employee-2", "employee-two-app", "employee-two-secret"),
    )
    runtime = LocalDingTalkCliRuntime(root=tmp_path, config_loader=lambda: config, dws_path=Path("/tools/dws"))

    first = runtime.environment(DingTalkCliScope("local", "employee-1"))
    second = runtime.environment(DingTalkCliScope("local", "employee-2"))

    assert runtime.is_available(DingTalkCliScope("local", "employee-1"))
    assert runtime.is_available(DingTalkCliScope("local", "employee-2"))
    assert first["DWS_CLIENT_ID"] == "employee-one-app"
    assert first["DWS_CLIENT_SECRET"] == "employee-one-secret"
    assert second["DWS_CLIENT_ID"] == "employee-two-app"
    assert second["DWS_CLIENT_SECRET"] == "employee-two-secret"


def test_runtime_shares_the_only_channel_application_with_all_employees(tmp_path):
    config = _config(_app("employee-2", "shared-app", "shared-secret"))
    runtime = LocalDingTalkCliRuntime(root=tmp_path, config_loader=lambda: config, dws_path=Path("/tools/dws"))

    env = runtime.environment(DingTalkCliScope("local", "employee-1"))

    assert runtime.is_available(DingTalkCliScope("local", "employee-1"))
    assert env["DWS_CLIENT_ID"] == "shared-app"
    assert env["DWS_CLIENT_SECRET"] == "shared-secret"


def test_runtime_does_not_use_another_employees_bound_application(tmp_path):
    config = _config(
        _app("employee-1", "employee-one-app", "employee-one-secret"),
        _app("employee-2", "employee-two-app", "employee-two-secret"),
    )
    runtime = LocalDingTalkCliRuntime(root=tmp_path, config_loader=lambda: config, dws_path=Path("/tools/dws"))

    assert not runtime.is_available(DingTalkCliScope("local", "employee-3"))
    assert "DWS_CLIENT_SECRET" not in runtime.environment(DingTalkCliScope("local", "employee-3"))


@pytest.mark.parametrize(
    "apps",
    [
        (_app("employee-1", "same-app", "secret-1", is_default=True),
         _app("employee-2", "same-app", "secret-2")),
        (_app("employee-1", "app-1", "", enabled=True, is_default=True),),
        (_app("employee-1", "app-1", "secret-1", enabled=False, is_default=True),),
    ],
)
def test_runtime_fails_closed_without_a_valid_unique_default_application(tmp_path, apps):
    runtime = LocalDingTalkCliRuntime(root=tmp_path, config_loader=lambda: _config(*apps), dws_path=Path("/tools/dws"))

    assert not runtime.is_available(DingTalkCliScope("local", "employee-1"))
    assert "DWS_CLIENT_SECRET" not in runtime.environment(DingTalkCliScope("local", "employee-1"))


@pytest.mark.asyncio
async def test_execute_uses_app_token_api_mode_without_user_login(tmp_path):
    calls = []

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        calls.append((args, env, cwd))
        return subprocess.CompletedProcess(
            args, 0,
            '{"ok":true,"data":{"count":1,"clientSecret":"app-secret","accessToken":"token-value",'
            '"appToken":"app-token-value"}}',
            "",
        )

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "app-secret", is_default=True)),
        dws_path=Path("/tools/dws"),
        runner=runner,
    )

    result = await runtime.execute(
        DingTalkCliScope("local", "employee-1"),
        ["api", "GET", "/v1.0/microApp/allApps"],
    )

    assert result == {
        "ok": True,
        "data": {
            "count": 1,
            "clientSecret": "[redacted]",
            "accessToken": "[redacted]",
            "appToken": "[redacted]",
        },
    }
    assert calls[0][0] == [str(Path("/tools/dws").resolve()), "api", "GET", "/v1.0/microApp/allApps"]
    assert calls[0][1]["DWS_CLIENT_ID"] == "ding-app"
    assert calls[0][1]["DWS_CLIENT_SECRET"] == "app-secret"
    assert "auth" not in calls[0][0]
    assert "--client-secret" not in calls[0][0]


@pytest.mark.asyncio
async def test_execute_uses_a_cached_pinned_cli_binary_without_npx(tmp_path):
    package = (
        tmp_path / "shared-cli" / "home" / ".cache" / "npm" / "_npx" / "cache-key" /
        "node_modules" / "dingtalk-workspace-cli"
    )
    vendor = package / "vendor"
    vendor.mkdir(parents=True)
    (package / "package.json").write_text('{"name":"dingtalk-workspace-cli","version":"1.0.65"}')
    binary = vendor / ("dws.exe" if os.name == "nt" else "dws")
    binary.write_bytes(b"cached binary")
    binary.chmod(0o755)
    calls = []

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "{}", "")

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        npx_path=Path("/tools/npx"),
        runner=runner,
    )

    await runtime.execute(DingTalkCliScope("local", "employee-1"), ["api", "GET", "/v1.0/test"])

    assert calls == [[str(binary), "api", "GET", "/v1.0/test"]]


@pytest.mark.asyncio
async def test_execute_does_not_return_unstructured_cli_output(tmp_path):
    def runner(args, *, env, input_text=None, timeout=None, cwd):
        return subprocess.CompletedProcess(args, 0, "app_token=should-not-reach-model", "")

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        dws_path=Path("/tools/dws"),
        runner=runner,
    )

    with pytest.raises(RuntimeError, match="invalid response"):
        await runtime.execute(DingTalkCliScope("local", "employee-1"), ["api", "GET", "/v1.0/test"])


@pytest.mark.asyncio
async def test_execute_uses_a_short_timeout_and_reports_cli_timeout(tmp_path):
    calls = []

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        calls.append(timeout)
        raise subprocess.TimeoutExpired(args, timeout)

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        dws_path=Path("/tools/dws"),
        runner=runner,
    )

    with pytest.raises(PersistentAgentError) as exc_info:
        await runtime.execute(DingTalkCliScope("local", "employee-1"), ["api", "GET", "/v1.0/test"])

    assert calls == [30]
    assert exc_info.value.code == "DINGTALK_CLI_TIMEOUT"


@pytest.mark.asyncio
async def test_execute_maps_npm_cache_collisions_to_a_safe_retry_stop(tmp_path):
    def runner(args, *, env, input_text=None, timeout=None, cwd):
        return subprocess.CompletedProcess(args, 190, "", "npm error code ENOTEMPTY")

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        dws_path=Path("/tools/dws"),
        runner=runner,
    )

    with pytest.raises(PersistentAgentError) as exc_info:
        await runtime.execute(DingTalkCliScope("local", "employee-1"), ["api", "GET", "/v1.0/test"])

    assert exc_info.value.code == "DINGTALK_CLI_CACHE_BUSY"
    assert "ENOTEMPTY" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_execute_repairs_a_partial_npx_package_cache_after_enotempty(tmp_path):
    cache_root = tmp_path / "npm-cache" / "1.0.65"
    package_root = cache_root / "_npx" / "cache-key" / "node_modules"
    partial_package = package_root / "dingtalk-workspace-cli"
    stale_rename = package_root / ".dingtalk-workspace-cli-stale"
    partial_package.mkdir(parents=True)
    stale_rename.mkdir()
    calls = []

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        calls.append(args)
        if len(calls) == 1:
            return subprocess.CompletedProcess(args, 190, "", "npm error code ENOTEMPTY")
        return subprocess.CompletedProcess(args, 0, '{"ok":true}', "")

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        npx_path=Path("/tools/npx"),
        runner=runner,
    )

    result = await runtime.execute(
        DingTalkCliScope("local", "employee-1"), ["api", "GET", "/v1.0/doc/workspaces"]
    )

    assert result == {"ok": True}
    assert len(calls) == 2
    assert not partial_package.exists()
    assert not stale_rename.exists()


@pytest.mark.asyncio
async def test_execute_maps_dingtalk_http_403_without_exposing_cli_output(tmp_path):
    def runner(args, *, env, input_text=None, timeout=None, cwd):
        return subprocess.CompletedProcess(args, 1, "", "DingTalk API request failed: HTTP 403 Forbidden")

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        dws_path=Path("/tools/dws"),
        runner=runner,
    )

    with pytest.raises(PersistentAgentError) as exc_info:
        await runtime.execute(DingTalkCliScope("local", "employee-1"), ["api", "GET", "/v1.0/test"])

    assert exc_info.value.code == "DINGTALK_CLI_API_FORBIDDEN"
    assert "403" in str(exc_info.value)
    assert "HTTP 403 Forbidden" not in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [
    "Contact.User.Read",
    "ConvFile.Space.Read",
    "Storage.UploadInfo.Read",
    "qyapi_get_microapp_list",
    "Document.Workspace.Read",
    "Wiki.Node.Read",
    "Calendar.Event.Write",
])
async def test_execute_reports_the_missing_dingtalk_permission_scope_without_leaking_cli_output(tmp_path, scope):
    secret = "app-secret-must-not-leak"

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        stderr = (
            "HTTP 403 Forbidden "
            '{"code":"Forbidden.AccessDenied.AccessTokenPermissionDenied",'
            f'"message":"应用尚未开通所需的权限：[{scope}]，请申请开通",'
            f'"debug":"{secret}"}}'
        )
        return subprocess.CompletedProcess(args, 1, "", stderr)

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", secret, is_default=True)),
        dws_path=Path("/tools/dws"),
        runner=runner,
    )

    with pytest.raises(PersistentAgentError) as exc_info:
        await runtime.execute(DingTalkCliScope("local", "employee-1"), ["api", "GET", "/v1.0/test"])

    assert exc_info.value.code == "DINGTALK_CLI_API_FORBIDDEN"
    assert scope in str(exc_info.value)
    if scope == "Storage.UploadInfo.Read":
        assert "企业存储文件上传信息读权限" in str(exc_info.value)
    if scope == "Calendar.Event.Write":
        assert "日历应用中日程写权限" in str(exc_info.value)
    assert secret not in str(exc_info.value)
    assert "debug" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_group_member_api_does_not_report_unverifiable_scope_as_a_permission_to_enable(tmp_path):
    stderr = json.dumps({
        "error": {
            "category": "api",
            "code": 1,
            "message": (
                "API business error (errcode: 88, HTTP 200): permission denied "
                "[Chat.OrgInnerGroup.Read], {requiredScopes=[Chat.OrgInnerGroup.Read]}"
            ),
        },
    })

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        return subprocess.CompletedProcess(args, 1, "", stderr)

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        dws_path=Path("/tools/dws"),
        runner=runner,
    )

    with pytest.raises(PersistentAgentError) as exc_info:
        await runtime.execute(
            DingTalkCliScope("local", "employee-1"),
            ["api", "POST", "/v1.0/im/innerGroups/members/query"],
        )

    assert exc_info.value.code == "DINGTALK_CLI_API_FORBIDDEN"
    assert "POST /v1.0/im/innerGroups/members/query" in str(exc_info.value)
    assert "Chat.OrgInnerGroup.Read" not in str(exc_info.value)
    assert "开通" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_execute_reports_legacy_topapi_required_scope_from_dws_cli_error(tmp_path):
    stderr = json.dumps({
        "error": {
            "category": "api",
            "code": 1,
            "message": (
                "API business error (errcode: 88, HTTP 200, requestId: trace-private): "
                "missing permission [qyapi_get_member]; apply at "
                "https://open-dev.dingtalk.com/appscope/apply?content=scope-context#qyapi_get_member, "
                "{requiredScopes=[qyapi_get_member]}"
            ),
        },
    })

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        return subprocess.CompletedProcess(args, 1, "", stderr)

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        dws_path=Path("/tools/dws"),
        runner=runner,
    )

    with pytest.raises(PersistentAgentError) as exc_info:
        await runtime.execute(
            DingTalkCliScope("local", "employee-1"),
            ["api", "POST", "/topapi/v2/user/get", "--base-url", "https://oapi.dingtalk.com"],
        )

    assert exc_info.value.code == "DINGTALK_CLI_API_FORBIDDEN"
    assert "qyapi_get_member" in str(exc_info.value)
    assert (
        "https://open-dev.dingtalk.com/appscope/apply?content=scope-context#qyapi_get_member"
        in str(exc_info.value)
    )
    assert "trace-private" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_execute_maps_structured_stdout_permission_errors(tmp_path):
    secret = "do-not-return-this"
    response = {
        "error": {
            "code": "Forbidden.AccessDenied.AccessTokenPermissionDenied",
            "message": "missing permission [Calendar.Event.Write]",
            "debug": secret,
        },
    }

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        return subprocess.CompletedProcess(args, 1, json.dumps(response), "")

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        dws_path=Path("/tools/dws"),
        runner=runner,
    )

    with pytest.raises(PersistentAgentError) as exc_info:
        await runtime.execute(
            DingTalkCliScope("local", "employee-1"),
            ["api", "POST", "/v1.0/calendar/users/union-42/calendars/primary/events"],
        )

    assert exc_info.value.code == "DINGTALK_CLI_API_FORBIDDEN"
    assert "Calendar.Event.Write" in str(exc_info.value)
    assert "日历应用中日程写权限" in str(exc_info.value)
    assert secret not in str(exc_info.value)


@pytest.mark.asyncio
async def test_workspace_permission_error_identifies_the_api_and_explains_scope_value(tmp_path):
    def runner(args, *, env, input_text=None, timeout=None, cwd):
        stderr = (
            'HTTP 403 Forbidden {"code":"Forbidden.AccessDenied.AccessTokenPermissionDenied",'
            '"message":"permission denied [Document.Workspace.Read]"}'
        )
        return subprocess.CompletedProcess(args, 1, "", stderr)

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        dws_path=Path("/tools/dws"),
        runner=runner,
    )

    with pytest.raises(PersistentAgentError) as exc_info:
        await runtime.execute(
            DingTalkCliScope("local", "employee-1"),
            ["api", "GET", "/v1.0/doc/workspaces?operatorId=should-not-leak"],
        )

    message = str(exc_info.value)
    assert "GET /v1.0/doc/workspaces" in message
    assert "Document.Workspace.Read" in message
    assert "知识库空间读取权限" in message
    assert "控制台显示名称可能不同" in message
    assert "should-not-leak" not in message


@pytest.mark.asyncio
async def test_npx_calls_sharing_a_cache_are_serialized_across_runtime_instances(tmp_path):
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    package = (
        tmp_path / "npm-cache" / "1.0.65" / "_npx" / "cache-key" /
        "node_modules" / "dingtalk-workspace-cli"
    )
    binary = package / "vendor" / ("dws.exe" if os.name == "nt" else "dws")
    calls = []

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        calls.append(args)
        if first_entered.is_set():
            second_entered.set()
        else:
            first_entered.set()
            release_first.wait(timeout=2)
            binary.parent.mkdir(parents=True)
            (package / "package.json").write_text(
                '{"name":"dingtalk-workspace-cli","version":"1.0.65"}'
            )
            binary.write_bytes(b"cached binary")
            binary.chmod(0o755)
        return subprocess.CompletedProcess(args, 0, "{}", "")

    config = lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True))
    first = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=config,
        npx_path=Path("/tools/npx"),
        runner=runner,
    )
    second = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=config,
        npx_path=Path("/tools/npx"),
        runner=runner,
    )

    tasks = asyncio.gather(
        first.execute(DingTalkCliScope("local", "employee-1"), ["api", "GET", "/v1.0/test"]),
        second.execute(DingTalkCliScope("local", "employee-2"), ["api", "GET", "/v1.0/test"]),
    )
    assert await asyncio.to_thread(first_entered.wait, 1)
    await asyncio.sleep(0.1)
    second_was_concurrent = second_entered.is_set()
    release_first.set()
    await tasks
    assert not second_was_concurrent
    assert calls == [
        [str(Path("/tools/npx").resolve()), "--yes", "--package=dingtalk-workspace-cli@1.0.65", "--", "dws", "api", "GET", "/v1.0/test"],
        [str(binary), "api", "GET", "/v1.0/test"],
    ]


@pytest.mark.asyncio
async def test_execute_rejects_oauth_and_non_api_commands(tmp_path):
    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        dws_path=Path("/tools/dws"),
    )
    scope = DingTalkCliScope("local", "employee-1")

    with pytest.raises(RuntimeError, match="app-token API"):
        await runtime.execute(scope, ["auth", "login"])
    with pytest.raises(RuntimeError, match="app-token API"):
        await runtime.execute(scope, ["contact", "user", "get-self"])


@pytest.mark.asyncio
async def test_execute_rejects_commands_without_a_default_application(tmp_path):
    calls = []

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "{}", "")

    runtime = LocalDingTalkCliRuntime(root=tmp_path, config_loader=lambda: _config(), dws_path=Path("/tools/dws"), runner=runner)

    with pytest.raises(RuntimeError, match="no usable default or employee-bound DingTalk application"):
        await runtime.execute(DingTalkCliScope("local", "employee-1"), ["api", "GET", "/v1.0/microApp/allApps"])

    assert calls == []


def test_unbound_employee_environment_does_not_inherit_service_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("DWS_CLIENT_ID", "host-app")
    monkeypatch.setenv("DWS_CLIENT_SECRET", "host-secret")
    runtime = LocalDingTalkCliRuntime(root=tmp_path, config_loader=lambda: _config(), dws_path=Path("/tools/dws"))

    env = runtime.environment(DingTalkCliScope("local", "employee-1"))

    assert "DWS_CLIENT_ID" not in env
    assert "DWS_CLIENT_SECRET" not in env


def test_credentials_rotation_switches_to_a_new_private_token_cache(tmp_path):
    config = _config(_app("employee-1", "ding-app", "secret-v1", is_default=True))
    runtime = LocalDingTalkCliRuntime(root=tmp_path, config_loader=lambda: config, dws_path=Path("/tools/dws"))
    scope = DingTalkCliScope("local", "employee-1")
    original_config_dir = runtime.environment(scope)["DWS_CONFIG_DIR"]
    config["channels"]["dingtalk"]["apps"][0]["client_secret"] = "secret-v2"

    rotated_config_dir = runtime.environment(scope)["DWS_CONFIG_DIR"]

    assert rotated_config_dir != original_config_dir


@pytest.mark.parametrize(
    "argv",
    [
        ["api", "GET", "https://example.invalid/path"],
        ["api", "GET", "https://other.dingtalk.com/path"],
        ["api", "GET", "https://api.dingtalk.com:8443/path"],
        ["api", "GET", "https://user@api.dingtalk.com/path"],
        ["api", "GET", "/path", "--base-url", "http://api.dingtalk.com"],
        ["api", "GET", "/path", "--base-url=https://example.invalid"],
        ["api", "GET", "/path", "--base-url=https://other.dingtalk.com"],
        ["api", "GET", "/path", "--base-url=https://api.dingtalk.com:8443"],
        ["api", "GET", "//example.invalid/path"],
    ],
)
def test_cli_rejects_non_dingtalk_and_plain_http_targets(tmp_path, argv):
    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        dws_path=Path("/tools/dws"),
    )

    with pytest.raises(RuntimeError, match="DingTalk"):
        runtime._validate_argv(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["api", "GET", "https://api.dingtalk.com/v1.0/microApp/allApps"],
        ["api", "POST", "/topapi/v2/user/get", "--base-url", "https://oapi.dingtalk.com"],
    ],
)
def test_cli_accepts_only_official_https_api_hosts(tmp_path, argv):
    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        dws_path=Path("/tools/dws"),
    )

    assert runtime._validate_argv(argv) == argv


@pytest.mark.asyncio
async def test_upload_group_file_preserves_pptx_and_returns_committed_group_file(tmp_path):
    calls = []
    uploaded = []
    workspace_root = tmp_path / "workspaces"
    output_dir = workspace_root / "local" / "employee-1" / "output"
    output_dir.mkdir(parents=True)
    file_content = b"pptx-binary-content"
    (output_dir / "ppttest.pptx").write_bytes(file_content)

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        calls.append(args)
        path = args[3]
        if path == "/topapi/v2/user/get":
            body = {"result": {"unionid": "union-42"}}
        elif path == "/v1.0/convFile/conversations/spaces/query":
            body = {"space": {"spaceId": "group-space-9"}}
        elif path == "/v1.0/storage/spaces/group-space-9/files/uploadInfos/query":
            body = {
                "uploadKey": "upload-key",
                "headerSignatureInfo": {
                    "resourceUrls": ["https://storage.aliyuncs.com/signed-upload?secret=private"],
                    "headers": {"x-oss-signature": "signed-header"},
                },
            }
        elif path == "/v1.0/storage/spaces/group-space-9/files/commit":
            body = {"dentry": {"id": "dentry-77", "uuid": "uuid-77", "name": "ppttest.pptx"}}
        else:
            raise AssertionError(f"unexpected API request: {args}")
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

    async def upload(url, headers, data):
        uploaded.append((url, headers, data))

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path / "dingtalk-cli",
        workspace_root=workspace_root,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        dws_path=Path("/tools/dws"),
        runner=runner,
        signed_upload=upload,
    )

    result = await runtime.upload_group_file(
        DingTalkCliScope("local", "employee-1"),
        conversation_id="cid-current-group",
        sender_staff_id="staff-42",
        file_path="output/ppttest.pptx",
    )

    assert result == {
        "uploaded": True,
        "fileName": "ppttest.pptx",
        "size": len(file_content),
        "dentryId": "dentry-77",
        "dentryUuid": "uuid-77",
        "spaceId": "group-space-9",
    }
    assert len(calls) == 4
    upload_payload = json.loads(calls[2][calls[2].index("--data") + 1])
    precheck = upload_payload["option"]["preCheckParam"]
    assert precheck["name"] == "ppttest.pptx"
    assert precheck["size"] == len(file_content)
    assert len(uploaded) == 1
    assert uploaded[0] == (
        "https://storage.aliyuncs.com/signed-upload?secret=private",
        {"x-oss-signature": "signed-header"},
        file_content,
    )


@pytest.mark.asyncio
async def test_upload_group_file_rejects_paths_outside_shared_output(tmp_path):
    workspace_root = tmp_path / "workspaces"
    output_dir = workspace_root / "local" / "employee-1" / "output"
    output_dir.mkdir(parents=True)
    outside = tmp_path / "outside.pptx"
    outside.write_bytes(b"not allowed")
    runtime = LocalDingTalkCliRuntime(
        root=tmp_path / "dingtalk-cli",
        workspace_root=workspace_root,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        dws_path=Path("/tools/dws"),
    )

    with pytest.raises(PersistentAgentError) as exc_info:
        await runtime.upload_group_file(
            DingTalkCliScope("local", "employee-1"),
            conversation_id="cid-current-group",
            sender_staff_id="staff-42",
            file_path=str(outside),
        )

    assert exc_info.value.code == "DINGTALK_CLI_UPLOAD_FILE_INVALID"


@pytest.mark.asyncio
async def test_create_calendar_event_uses_app_token_and_returns_dingtalk_event_id(tmp_path):
    calls = []

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        calls.append(args)
        api_index = args.index("api")
        method = args[api_index + 1]
        path = args[api_index + 2]
        if path == "/topapi/v2/user/get":
            body = {"result": {"unionid": "union-42"}}
        elif path == "/v1.0/calendar/users/union-42/calendars/primary/events":
            assert method == "POST"
            body = {
                "id": "event-91",
                "summary": "项目评审",
                "start": {"dateTime": "2026-10-29T10:30:00+08:00", "timeZone": "Asia/Shanghai"},
                "end": {"dateTime": "2026-10-29T11:00:00+08:00", "timeZone": "Asia/Shanghai"},
                "onlineMeetingInfo": {"type": "dingtalk", "url": "https://meeting.dingtalk.com/event-91"},
            }
        else:
            raise AssertionError(f"unexpected API request: {args}")
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        dws_path=Path("/tools/dws"),
        runner=runner,
    )

    result = await runtime.create_calendar_event(
        DingTalkCliScope("local", "employee-1"),
        sender_staff_id="staff-42",
        title="项目评审",
        start_at="2026-10-29T10:30:00+08:00",
        end_at="2026-10-29T11:00:00+08:00",
        time_zone="Asia/Shanghai",
        reminder_minutes=10,
        create_video_meeting=True,
    )

    assert result == {
        "created": True,
        "eventId": "event-91",
        "title": "项目评审",
        "startAt": "2026-10-29T10:30:00+08:00",
        "endAt": "2026-10-29T11:00:00+08:00",
        "timeZone": "Asia/Shanghai",
        "meetingUrl": "https://meeting.dingtalk.com/event-91",
    }
    assert len(calls) == 2
    event_payload = json.loads(calls[1][calls[1].index("--data") + 1])
    assert event_payload == {
        "summary": "项目评审",
        "start": {"dateTime": "2026-10-29T10:30:00+08:00", "timeZone": "Asia/Shanghai"},
        "end": {"dateTime": "2026-10-29T11:00:00+08:00", "timeZone": "Asia/Shanghai"},
        "isAllDay": False,
        "reminders": [{"method": "dingtalk", "minutes": 10}],
        "onlineMeetingInfo": {"type": "dingtalk"},
    }


@pytest.mark.asyncio
async def test_create_calendar_event_invites_all_trusted_current_group_members(tmp_path):
    calls = []

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        if args[1:3] == ["auth", "status"]:
            return subprocess.CompletedProcess(args, 0, json.dumps({"success": True, "authenticated": False}), "")
        del input_text, timeout, cwd
        calls.append(args)
        endpoint = args[3]
        request = json.loads(args[args.index("--data") + 1])
        if endpoint == "/v1.0/im/innerGroups/members/query":
            assert request["openConversationId"] == "cid-current-group"
            assert request["userId"] == "staff-42"
            if request["nextToken"] == 0:
                body = {
                    "success": True,
                    "list": [{"userId": "staff-42"}, {"userId": "staff-7"}],
                    "hasMore": True,
                    "nextToken": 1,
                }
            else:
                assert request["nextToken"] == 1
                body = {
                    "success": True,
                    "list": [{"userId": "staff-8"}],
                    "hasMore": False,
                    "nextToken": 0,
                }
        elif endpoint == "/topapi/v2/user/get":
            body = {"result": {"unionid": f"union-{request['userid']}"}}
        elif endpoint == "/v1.0/calendar/users/union-staff-42/calendars/primary/events":
            event = request
            assert [item["id"] for item in event["attendees"]] == ["union-staff-7", "union-staff-8"]
            body = {"id": "event-group-17"}
        else:
            raise AssertionError(f"unexpected CLI request: {args}")
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        dws_path=Path("/tools/dws"),
        runner=runner,
    )

    result = await runtime.create_calendar_event(
        DingTalkCliScope("local", "employee-1"),
        sender_staff_id="staff-42",
        title="全员会议",
        start_at="2026-10-29T10:30:00+08:00",
        end_at="2026-10-29T11:30:00+08:00",
        time_zone="Asia/Shanghai",
        conversation_id="cid-current-group",
        include_group_members=True,
    )

    assert result["created"] is True
    assert result["eventId"] == "event-group-17"
    assert result["attendeeCount"] == 2
    member_calls = [call for call in calls if call[3] == "/v1.0/im/innerGroups/members/query"]
    assert len(member_calls) == 2


@pytest.mark.asyncio
async def test_create_calendar_event_rejects_missing_title_or_invalid_time_before_api_call(tmp_path):
    calls = []

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        calls.append(args)
        raise AssertionError("calendar API must not be called for invalid event data")

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        dws_path=Path("/tools/dws"),
        runner=runner,
    )

    with pytest.raises(PersistentAgentError) as missing_title:
        await runtime.create_calendar_event(
            DingTalkCliScope("local", "employee-1"),
            sender_staff_id="staff-42",
            title=" ",
            start_at="2026-10-29T10:30:00+08:00",
            end_at="2026-10-29T11:00:00+08:00",
            time_zone="Asia/Shanghai",
        )

    with pytest.raises(PersistentAgentError) as invalid_range:
        await runtime.create_calendar_event(
            DingTalkCliScope("local", "employee-1"),
            sender_staff_id="staff-42",
            title="会议",
            start_at="2026-10-29T11:00:00+08:00",
            end_at="2026-10-29T10:30:00+08:00",
            time_zone="Asia/Shanghai",
        )

    assert missing_title.value.code == "DINGTALK_CLI_CALENDAR_REQUEST_INVALID"
    assert invalid_range.value.code == "DINGTALK_CLI_CALENDAR_REQUEST_INVALID"
    assert calls == []


@pytest.mark.asyncio
async def test_create_calendar_event_reports_the_failed_create_endpoint_without_retrying(tmp_path):
    calls = []

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        api_index = args.index("api")
        path = args[api_index + 2]
        calls.append(path)
        if path == "/topapi/v2/user/get":
            return subprocess.CompletedProcess(args, 0, json.dumps({"result": {"unionid": "union-42"}}), "")
        raise RuntimeError("unstructured CLI detail must not be returned")

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        dws_path=Path("/tools/dws"),
        runner=runner,
    )

    with pytest.raises(PersistentAgentError) as exc_info:
        await runtime.create_calendar_event(
            DingTalkCliScope("local", "employee-1"),
            sender_staff_id="staff-42",
            title="项目评审",
            start_at="2026-10-29T10:30:00+08:00",
            end_at="2026-10-29T11:00:00+08:00",
            time_zone="Asia/Shanghai",
        )

    assert exc_info.value.code == "DINGTALK_CLI_CALENDAR_CREATE_FAILED"
    assert "POST /v1.0/calendar/users/union-42/calendars/primary/events" in str(exc_info.value)
    assert "不要自动重试" in str(exc_info.value)
    assert calls == [
        "/topapi/v2/user/get",
        "/v1.0/calendar/users/union-42/calendars/primary/events",
    ]


def test_oauth_environment_uses_same_install_policy_as_app(tmp_path, monkeypatch):
    monkeypatch.setenv('npm_config_ignore_scripts', 'true')
    monkeypatch.setenv('JIUWENMATE_DINGTALK_CLI_NPM_REGISTRY', 'https://registry.npmjs.org')
    runtime = LocalDingTalkCliRuntime(
        root=tmp_path, config_loader=lambda: _config(_app('employee-1', 'app', 'secret')),
        dws_path=Path('/tools/dws'),
    )
    scope = DingTalkCliScope('local', 'employee-1')
    env = runtime._oauth_environment(scope)
    assert env['npm_config_ignore_scripts'] == 'false'
    assert env['npm_config_registry'] == 'https://registry.npmjs.org'
    assert env['DWS_CONFIG_DIR'] != runtime.environment(scope)['DWS_CONFIG_DIR']


def test_user_oauth_uses_registered_employee_client_with_separate_token_store(tmp_path, monkeypatch):
    monkeypatch.setenv('DWS_CLIENT_ID', 'inherited-client')
    monkeypatch.setenv('DWS_CLIENT_SECRET', 'inherited-secret')
    runtime = LocalDingTalkCliRuntime(
        root=tmp_path, config_loader=lambda: _config(_app('employee-1', 'bot-app', 'bot-secret')),
        dws_path=Path('/tools/dws'),
    )
    scope = DingTalkCliScope('local', 'employee-1')
    oauth = runtime._oauth_environment(scope)
    assert (oauth['DWS_CLIENT_ID'], oauth['DWS_CLIENT_SECRET']) == ('bot-app', 'bot-secret')
    app = runtime.environment(scope)
    assert (app['DWS_CLIENT_ID'], app['DWS_CLIENT_SECRET']) == ('bot-app', 'bot-secret')
    assert oauth['DWS_CONFIG_DIR'] != app['DWS_CONFIG_DIR']


@pytest.mark.asyncio
async def test_device_authorization_reports_rejected_client_without_exposing_cli_output(tmp_path, monkeypatch):
    def runner(args, *, env, input_text=None, timeout=None, cwd):
        return subprocess.CompletedProcess(args, 0, '{"authenticated":false}', '')

    def start_process(args, *, stdin, stdout, stderr, env, cwd, start_new_session):
        stdout.write(json.dumps({'error': {'category': 'auth', 'code': 'DEVICE_AUTH_FAILED',
            'message': '[900103] 应用不存在 secret-value'}}).encode())
        stdout.flush()
        return type('FailedProcess', (), {'pid': 987654, 'returncode': 2, 'poll': lambda self: 2})()

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path, config_loader=lambda: _config(_app('employee-1', 'app', 'secret-value')),
        dws_path=Path('/tools/dws'), runner=runner,
    )
    monkeypatch.setattr(subprocess, 'Popen', start_process)
    with pytest.raises(PersistentAgentError) as failure:
        await runtime.dingtalk_auth_begin(DingTalkCliScope('local', 'employee-1'))
    assert failure.value.code == 'DINGTALK_CLI_AUTH_LOGIN_FAILED'
    assert '900103' in str(failure.value)
    assert 'secret-value' not in str(failure.value)


@pytest.mark.asyncio
async def test_disconnect_stops_pending_device_authorization_before_deleting_profile(tmp_path, monkeypatch):
    runtime = LocalDingTalkCliRuntime(
        root=tmp_path, config_loader=lambda: _config(_app('employee-1', 'app', 'secret')),
        dws_path=Path('/tools/dws'),
    )
    scope = DingTalkCliScope('local', 'employee-1')
    operation = runtime._scope_digest(scope) + '.' + 'a' * 32
    log, pid, _ = runtime._oauth_paths(scope, operation)
    log.parent.mkdir(parents=True)
    log.write_text('pending', encoding='utf-8')
    pid.write_text('987654', encoding='utf-8')
    runtime._oauth_current(scope).write_text(operation, encoding='utf-8')
    stopped = []
    monkeypatch.setattr(os, 'kill', lambda target, sig: stopped.append(target))
    monkeypatch.setattr(runtime, '_oauth_process_running', lambda path: True)
    root = runtime._oauth_root(scope)
    await runtime.dingtalk_auth_disconnect(scope)
    assert stopped == [987654]
    assert not root.exists()


def test_cancel_rejects_an_operation_from_another_employee(tmp_path):
    runtime = LocalDingTalkCliRuntime(
        root=tmp_path, config_loader=lambda: _config(_app('employee-1', 'app', 'secret', is_default=True)),
        dws_path=Path('/tools/dws'),
    )
    scope = DingTalkCliScope('local', 'employee-1')
    foreign = DingTalkCliScope('local', 'employee-2')
    operation = runtime._scope_digest(foreign) + '.' + 'a' * 32
    runtime._oauth_current(scope).parent.mkdir(parents=True)
    with pytest.raises(PersistentAgentError) as exc:
        runtime._cancel_dingtalk_auth(scope, operation)
    assert exc.value.code == 'BAD_REQUEST'


@pytest.mark.parametrize('mode', ['missing', 'id_as_name', 'mismatched_user', 'lookup_failed', 'invalid_response', 'timeout'])
def test_connected_oauth_profile_resolves_missing_display_identity_from_current_user(tmp_path, mode):
    def runner(args, **options):
        if args[1:3] == ['auth', 'status']:
            body = {'authenticated': True}
        elif args[1:3] == ['profile', 'list']:
            profile = {'profile': 'corp', 'corpId': 'corp', 'corpName': 'corp', 'isCurrent': True}
            if mode in {'id_as_name', 'mismatched_user'}:
                profile.update({'userId': 'profile-user'})
            body = {'profiles': [profile]}
        else:
            assert '--profile' in args and args[args.index('--profile') + 1] == 'corp'
            assert 'contact' in args and '+me' in args
            if mode == 'invalid_response':
                return subprocess.CompletedProcess(args, 0, 'not-json', '')
            if mode == 'timeout':
                raise subprocess.TimeoutExpired(args, 5)
            if mode == 'lookup_failed':
                return subprocess.CompletedProcess(args, 1, '{"ok":false}', '')
            body = {'ok': True, 'outcome': 'success', 'data': {
                'userId': 'another-user' if mode == 'mismatched_user' else 'profile-user',
                'name': 'Authorized user', 'org': 'Authorized organization',
                'mobile': 'not-for-the-page', 'email': 'not-for-the-page',
            }}
        return subprocess.CompletedProcess(args, 0, json.dumps(body), '')
    runtime = LocalDingTalkCliRuntime(
        root=tmp_path, config_loader=lambda: _config(_app('employee-1', 'app', 'secret')),
        dws_path=Path('/tools/dws'), runner=runner,
    )
    result = runtime._dingtalk_auth_status(DingTalkCliScope('local', 'employee-1'))
    assert result['status'] == 'connected'
    if mode in {'missing', 'id_as_name'}:
        assert result['account'] == 'Authorized user'
        assert result['organization'] == 'Authorized organization'
    else:
        assert result.get('account') != 'Authorized user'
        assert result.get('organization') != 'Authorized organization'
    assert 'mobile' not in result and 'email' not in result


@pytest.mark.parametrize('mode', ['ambiguous', 'invalid_status'])
def test_oauth_status_does_not_invent_an_account_or_logout_on_cli_failure(tmp_path, mode):
    def runner(args, **options):
        if args[1:3] == ['auth', 'status']:
            return subprocess.CompletedProcess(args, 0, 'broken' if mode == 'invalid_status' else json.dumps({'authenticated': True}), '')
        return subprocess.CompletedProcess(args, 0, json.dumps({'profiles': [{'userId': 'one'}, {'userId': 'two'}]}), '')
    runtime = LocalDingTalkCliRuntime(
        root=tmp_path, config_loader=lambda: _config(_app('employee-1', 'app', 'secret')),
        dws_path=Path('/tools/dws'), runner=runner,
    )
    assert runtime._dingtalk_auth_status(DingTalkCliScope('local', 'employee-1'))['status'] == 'error'


@pytest.mark.asyncio
@pytest.mark.parametrize('command', [['drive', 'info', '--node', 'node'], ['chat', 'group', 'members', '--id', 'group']])
async def test_user_cli_response_limit_applies_to_document_and_calendar_paths(tmp_path, command):
    def runner(args, *, env, input_text=None, timeout=None, cwd):
        return subprocess.CompletedProcess(args, 0, json.dumps({'success': True, 'payload': 'x' * (241 * 1024)}), '')
    runtime = LocalDingTalkCliRuntime(
        root=tmp_path, config_loader=lambda: _config(_app('employee-1', 'app', 'secret')),
        dws_path=Path('/tools/dws'), runner=runner,
    )
    with pytest.raises(PersistentAgentError):
        await runtime._execute_oauth_command(DingTalkCliScope('local', 'employee-1'), command)


@pytest.mark.asyncio
async def test_concurrent_device_authorization_reuses_one_pending_process(tmp_path, monkeypatch):
    started = []
    def runner(args, *, env, input_text=None, timeout=None, cwd):
        time.sleep(0.01)
        return subprocess.CompletedProcess(args, 0, json.dumps({'authenticated': False}), '')
    def start_process(args, *, stdin, stdout, stderr, env, cwd, start_new_session):
        started.append(args)
        stdout.write(b'https://login.dingtalk.com/oauth2/device/verify.htm?user_code=TEST-CODE')
        stdout.flush()
        class Pending:
            pid = 987654
            def poll(self):
                return None
        return Pending()
    monkeypatch.setattr(subprocess, 'Popen', start_process)
    runtime = LocalDingTalkCliRuntime(
        root=tmp_path, config_loader=lambda: _config(_app('employee-1', 'app', 'secret')),
        dws_path=Path('/tools/dws'), runner=runner,
    )
    scope = DingTalkCliScope('local', 'employee-1')
    monkeypatch.setattr(runtime, '_oauth_process_running', lambda path: True, raising=False)
    first, second = await asyncio.gather(runtime.dingtalk_auth_begin(scope), runtime.dingtalk_auth_begin(scope))
    assert len(started) == 1
    assert first['operation_id'] == second['operation_id']
    assert first['user_code'] == 'TEST-CODE'


@pytest.mark.parametrize('kind,expected', [
    ('org_denied', 'org_access_denied'), ('dead', 'authorization_failed'), ('expired', 'authorization_expired'),
])
def test_finished_device_authorization_never_reuses_consumed_device_code(tmp_path, monkeypatch, kind, expected):
    runtime = LocalDingTalkCliRuntime(
        root=tmp_path, workspace_root=tmp_path / 'workspaces',
        config_loader=lambda: _config(_app('employee-1', 'app', 'secret')), dws_path=Path('/tools/dws'),
        runner=lambda args, **kw: subprocess.CompletedProcess(args, 0, '{"authenticated":false}', ''),
    )
    scope = DingTalkCliScope('local', 'employee-1')
    operation = runtime._scope_digest(scope) + '.' + 'a' * 32
    log, pid, _ = runtime._oauth_paths(scope, operation)
    log.parent.mkdir(parents=True)
    text = 'https://login.dingtalk.com/oauth2/device/verify.htm?user_code=USED-CODE\n'
    if kind == 'org_denied':
        text += '授权成功!\n' + json.dumps({'error': {'message': '您暂无 CLI 数据访问权限，请联系管理员开启'}})
    log.write_text(text)
    pid.write_text('987654')
    runtime._oauth_current(scope).write_text(operation)
    if kind == 'expired':
        os.utime(pid, (time.time() - 700, time.time() - 700))
    monkeypatch.setattr(runtime, '_oauth_process_running', lambda path: kind == 'expired', raising=False)
    monkeypatch.setattr(os, 'kill', lambda *args: None)
    for _ in range(2):
        status = runtime._dingtalk_auth_status(scope)
        assert status['status'] == 'error'
        assert status['error_code'] == expected
        assert not status.get('auth_url') and not status.get('user_code') and not status.get('operation_id')


def test_device_authorization_process_liveness_rejects_exit_and_reused_pid_metadata(tmp_path):
    import sys

    pid_file = tmp_path / 'auth.pid'
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        pid_file.write_text(str(process.pid))
        assert LocalDingTalkCliRuntime._oauth_process_running(pid_file)
        os.utime(pid_file, (time.time() - 60, time.time() - 60))
        assert not LocalDingTalkCliRuntime._oauth_process_running(pid_file)
        pid_file.write_text(str(process.pid))
        process.terminate()
        process.wait(timeout=5)
        assert not LocalDingTalkCliRuntime._oauth_process_running(pid_file)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_cancel_failed_authorization_clears_metadata_without_signalling_reused_pid(tmp_path, monkeypatch):
    runtime = LocalDingTalkCliRuntime(
        root=tmp_path, workspace_root=tmp_path / 'workspaces',
        config_loader=lambda: _config(_app('employee-1', 'app', 'secret')), dws_path=Path('/tools/dws'),
    )
    scope = DingTalkCliScope('local', 'employee-1')
    operation = runtime._scope_digest(scope) + '.' + 'a' * 32
    log, pid, _ = runtime._oauth_paths(scope, operation)
    log.parent.mkdir(parents=True)
    log.write_text('failed')
    pid.write_text('987654')
    runtime._oauth_current(scope).write_text(operation)
    signals = []
    monkeypatch.setattr(runtime, '_oauth_process_running', lambda path: False)
    monkeypatch.setattr(os, 'kill', lambda *args: signals.append(args))
    runtime._cancel_dingtalk_auth(scope, operation)
    assert signals == []
    assert not pid.exists() and not log.exists() and not runtime._oauth_current(scope).exists()


@pytest.mark.parametrize('finished_marker', ['dead_process', 'exit_file'])
def test_authorization_finishing_during_status_check_rechecks_persisted_login(tmp_path, monkeypatch, finished_marker):
    checks = []
    def runner(args, **options):
        if args[1:3] == ['auth', 'status']:
            checks.append(True)
            payload = {'authenticated': len(checks) > 1}
        else:
            payload = {'profiles': [{'userId': 'verified-user', 'userName': 'Test account',
                                    'corpName': 'Test organization', 'isCurrent': True}]}
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), '')
    runtime = LocalDingTalkCliRuntime(
        root=tmp_path, config_loader=lambda: _config(_app('employee-1', 'app', 'secret')),
        dws_path=Path('/tools/dws'), runner=runner,
    )
    scope = DingTalkCliScope('local', 'employee-1')
    operation = runtime._scope_digest(scope) + '.' + 'a' * 32
    log, pid, exit_path = runtime._oauth_paths(scope, operation)
    log.parent.mkdir(parents=True)
    log.write_text('authorization finished')
    pid.write_text('987654')
    runtime._oauth_current(scope).write_text(operation)
    if finished_marker == 'exit_file':
        exit_path.write_text('0')
    monkeypatch.setattr(runtime, '_oauth_process_running', lambda path: False)
    status = runtime._dingtalk_auth_status(scope)
    assert status['status'] == 'connected'
    assert status['account'] == 'Test account'
    assert status['organization'] == 'Test organization'
    assert not status.get('auth_url') and not status.get('user_code')
    assert not runtime._oauth_current(scope).exists()


def test_successful_authorization_clears_pending_pid_without_signalling_it(tmp_path, monkeypatch):
    def runner(args, *, env, input_text=None, timeout=None, cwd):
        body = {'authenticated': True} if args[1:3] == ['auth', 'status'] else {'profiles': [{'userId': 'user', 'isCurrent': True}]}
        return subprocess.CompletedProcess(args, 0, json.dumps(body), '')
    runtime = LocalDingTalkCliRuntime(
        root=tmp_path, config_loader=lambda: _config(_app('employee-1', 'app', 'secret')),
        dws_path=Path('/tools/dws'), runner=runner,
    )
    scope = DingTalkCliScope('local', 'employee-1')
    operation = runtime._scope_digest(scope) + '.' + 'a' * 32
    log, pid, _ = runtime._oauth_paths(scope, operation)
    log.parent.mkdir(parents=True)
    log.write_text('finished', encoding='utf-8')
    pid.write_text('987654', encoding='utf-8')
    runtime._oauth_current(scope).write_text(operation, encoding='utf-8')
    def must_not_signal(*args):
        raise AssertionError('A finished device-login PID must not be signalled')
    monkeypatch.setattr(os, 'kill', must_not_signal)
    assert runtime._dingtalk_auth_status(scope)['status'] == 'connected'
    assert not pid.exists()
    assert not runtime._oauth_current(scope).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [None, 'read', 'update', 'verify', 'wrong_event', 'changed_members'])
async def test_update_calendar_title_preserves_existing_event(tmp_path, monkeypatch, failure):
    import copy
    original = {
        'id': 'existing-event', 'summary': '会议',
        'start': {'dateTime': '2026-10-31T16:00:00+08:00'},
        'end': {'dateTime': '2026-10-31T17:00:00+08:00'},
        'attendees': [{'id': 'member-one'}, {'id': 'member-two'}],
        'reminders': [{'method': 'dingtalk', 'minutes': 15}],
    }
    calls = []
    def runner(args, *, env, input_text=None, timeout=None, cwd):
        calls.append(args)
        assert 'user-oauth' in env['DWS_CONFIG_DIR']
        assert args[args.index('--profile') + 1] == 'corp:owner'
        if args[1:4] == ['calendar', 'event', 'get']:
            if failure == 'read':
                return subprocess.CompletedProcess(args, 1, '', 'not found')
            event = copy.deepcopy(original)
            if len(calls) > 1:
                event['summary'] = '会议' if failure == 'verify' else 'AI同事讨论'
                if failure == 'changed_members':
                    event['attendees'] = []
            if failure == 'wrong_event':
                event['id'] = 'another-event'
            body = {'success': True, 'result': event}
        elif args[1:4] == ['calendar', 'event', 'update']:
            assert args[4:] == ['--id', 'existing-event', '--title', 'AI同事讨论', '--format', 'json', '--profile', 'corp:owner']
            if failure == 'update':
                return subprocess.CompletedProcess(args, 1, '', 'interrupted')
            body = {'success': True, 'result': {'id': 'existing-event'}}
        else:
            raise AssertionError('must never recreate or delete an event')
        return subprocess.CompletedProcess(args, 0, json.dumps(body), '')
    runtime = LocalDingTalkCliRuntime(
        root=tmp_path, config_loader=lambda: _config(_app('employee-1', 'app', 'secret')),
        dws_path=Path('/tools/dws'), runner=runner,
    )
    async def status(scope):
        return {'status': 'connected', '_profile': 'corp:owner'}
    monkeypatch.setattr(runtime, 'dingtalk_auth_status', status)
    if failure:
        with pytest.raises(PersistentAgentError):
            await runtime.update_calendar_event(DingTalkCliScope('local', 'employee-1'), event_id='existing-event', title='AI同事讨论')
    else:
        result = await runtime.update_calendar_event(DingTalkCliScope('local', 'employee-1'), event_id='existing-event', title='AI同事讨论')
        assert result['updated'] is True
        assert result['eventId'] == 'existing-event'
        assert result['summary'] == 'AI同事讨论'
    writes = [args for args in calls if args[1:4] == ['calendar', 'event', 'update']]
    assert len(writes) == (0 if failure in {'read', 'wrong_event'} else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('authorized', [True, False])
async def test_calendar_rename_does_not_write_when_already_named_or_not_authorized(tmp_path, monkeypatch, authorized):
    calls = []
    def runner(args, *, env, input_text=None, timeout=None, cwd):
        calls.append(args)
        assert args[1:4] == ['calendar', 'event', 'get']
        return subprocess.CompletedProcess(args, 0, json.dumps({'success': True, 'result': {'id': 'existing-event', 'summary': 'AI同事讨论'}}), '')
    runtime = LocalDingTalkCliRuntime(
        root=tmp_path, config_loader=lambda: _config(_app('employee-1', 'app', 'secret')),
        dws_path=Path('/tools/dws'), runner=runner,
    )
    async def status(scope):
        return {'status': 'connected' if authorized else 'not_connected', '_profile': 'corp:owner'}
    monkeypatch.setattr(runtime, 'dingtalk_auth_status', status)
    if authorized:
        result = await runtime.update_calendar_event(DingTalkCliScope('local', 'employee-1'), event_id='existing-event', title='AI同事讨论')
        assert result['updated'] is True and result['changed'] is False
        assert len(calls) == 1
    else:
        with pytest.raises(PersistentAgentError) as exc:
            await runtime.update_calendar_event(DingTalkCliScope('local', 'employee-1'), event_id='existing-event', title='AI同事讨论')
        assert exc.value.code == 'DINGTALK_CLI_USER_AUTH_REQUIRED'
        assert calls == []
