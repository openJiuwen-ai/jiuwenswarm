from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import subprocess
import tarfile
import threading
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from jiuwenswarm.server.jiuwenmate import dingtalk_cli_local_provider
from jiuwenswarm.server.jiuwenmate.dingtalk_cli_runtime import DingTalkCliRuntimeService, DingTalkCliScope
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
async def test_group_discovery_only_returns_groups_containing_the_employees_exact_robot(tmp_path, monkeypatch):
    runtime, calls = _online_document_runtime(tmp_path, monkeypatch, [
        {'success': True, 'result': {'groups': [
            {'openConversationId': 'cid-other', 'title': 'Unrelated'},
            {'openConversationId': 'cid-employee', 'title': 'Employee group'},
        ], 'hasMore': True, 'nextCursor': 5}},
        {'success': True, 'result': {'bots': [{'robotCode': 'other-robot'}]}},
        {'success': True, 'result': {'bots': [{'robotCode': 'employee-robot'}]}},
    ])
    monkeypatch.setattr(runtime, '_config', lambda: _config(
        {**_app('employee-1', 'ding-app', 'secret', is_default=True), 'robot_code': 'employee-robot'},
    ))
    result = await runtime.discover_groups(DingTalkCliScope('local', 'employee-1', app_id='ding-app'), cursor=0)

    assert result['groups'] == [{'conversation_id': 'cid-employee', 'name': 'Employee group'}]
    assert result['hasMore'] is True and result['nextCursor'] == 5
    assert result['failed'] is False
    assert len(result['readerKey']) == 64
    assert calls[0][0][1:6] == ['chat', 'group', 'list-all', '--limit', '5']
    assert calls[0][0][6:8] == ['--cursor', '0']
    assert all(call[0][1:4] != ['chat', 'message', 'list'] for call in calls)


@pytest.mark.asyncio
async def test_group_discovery_without_employee_authorization_never_lists_account_groups(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    runtime, calls = _online_document_runtime(tmp_path, monkeypatch, [])
    monkeypatch.setattr(runtime, 'dingtalk_auth_status', AsyncMock(return_value={'status': 'not_connected'}))
    with pytest.raises(PersistentAgentError) as failure:
        await runtime.discover_groups(DingTalkCliScope('local', 'employee-1', app_id='ding-app'))
    assert failure.value.code == 'DINGTALK_CLI_USER_AUTH_REQUIRED'
    assert not calls


@pytest.mark.asyncio
async def test_group_discovery_keeps_verified_groups_when_another_group_cannot_be_inspected(tmp_path, monkeypatch):
    runtime, calls = _online_document_runtime(tmp_path, monkeypatch, [
        {'success': True, 'result': {'groups': [
            {'openConversationId': 'cid-denied', 'title': 'Denied'},
            {'openConversationId': 'cid-employee', 'title': 'Employee group'},
        ], 'hasMore': True, 'nextCursor': 100}},
        {'success': False, 'errorCode': 'denied'},
        {'success': True, 'result': {'bots': [{'robotCode': 'ding-app'}]}},
    ])
    result = await runtime.discover_groups(DingTalkCliScope('local', 'employee-1', app_id='ding-app'))
    assert result['groups'] == [{'conversation_id': 'cid-employee', 'name': 'Employee group'}]
    assert result['failed'] is True
    assert result['hasMore'] is True and result['nextCursor'] == 100


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['missing_groups', 'invalid_id', 'repeated_cursor', 'oversized_page'])
async def test_group_discovery_rejects_invalid_page_before_reading_bots(tmp_path, monkeypatch, invalid):
    page = {'groups': [{'openConversationId': 'cid-ok', 'title': 'Group'}], 'hasMore': False, 'nextCursor': 0}
    if invalid == 'missing_groups':
        page.pop('groups')
    elif invalid == 'invalid_id':
        page['groups'][0]['openConversationId'] = 'bad id'
    elif invalid == 'repeated_cursor':
        page.update(hasMore=True, nextCursor=0)
    else:
        page['groups'] *= 6
    runtime, calls = _online_document_runtime(tmp_path, monkeypatch, [{'success': True, 'result': page}])
    with pytest.raises(PersistentAgentError) as failure:
        await runtime.discover_groups(DingTalkCliScope('local', 'employee-1', app_id='ding-app'), cursor=0)
    assert failure.value.code == 'DINGTALK_CLI_GROUP_HISTORY_INVALID'
    assert len(calls) == 1


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
async def test_group_history_does_not_treat_a_verified_bot_welcome_as_a_human_task(tmp_path, monkeypatch):
    runtime, calls = _online_document_runtime(tmp_path, monkeypatch, [
        {'success': True, 'result': {'bots': [{'robotCode': 'ding-app', 'botOpenDingTalkId': 'open-bot'}]}},
        {'contractVersion': '1.0', 'complete': True, 'hasMore': False, 'messages': [
            {'messageId': 'welcome', 'openConversationId': 'cid-known', 'senderOpenDingTalkId': 'open-bot',
             'sendType': 'user', 'text': 'any bot text', 'createTime': '2026-11-05 14:13:50'},
            {'messageId': 'human', 'openConversationId': 'cid-known', 'senderOpenDingTalkId': 'open-person',
             'sendType': 'user', 'text': 'please introduce yourselves', 'createTime': '2026-11-05 14:14:00'},
        ]},
    ])
    result = await runtime.read_group_history(DingTalkCliScope('local', 'employee-1'),
                                            conversation_id='cid-known', since='2026-11-05T06:13:00+00:00')
    assert [(message['messageId'], message['sendType']) for message in result['messages']] == [
        ('welcome', 'robot'), ('human', 'user'),
    ]


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


def _connected_status_runtime(tmp_path, calls, apps):
    def runner(args, *, env, input_text=None, timeout=None, cwd):
        calls.append(args[1:3])
        if args[1:3] == ["auth", "status"]:
            payload = {"success": True, "authenticated": True}
        else:
            payload = {"success": True, "profiles": [{"current": True, "userName": "tester"}]}
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    return LocalDingTalkCliRuntime(
        root=tmp_path, config_loader=lambda: _config(*apps), dws_path=Path("/tools/dws"), runner=runner,
    )


@pytest.mark.asyncio
async def test_group_polling_reuses_recent_authorization_until_it_changes(tmp_path):
    calls = []
    apps = [_app("employee-1", "ding-app", "secret")]
    runtime = _connected_status_runtime(tmp_path, calls, apps)
    scope = DingTalkCliScope("local", "employee-1", app_id="ding-app")

    first = await runtime.dingtalk_auth_status(scope)
    second = await runtime.dingtalk_auth_status(scope)

    assert first["status"] == second["status"] == "connected"
    assert calls.count(["auth", "status"]) == 1
    apps[0] = _app("employee-1", "ding-app", "rotated-secret")
    await runtime.dingtalk_auth_status(scope)
    assert calls.count(["auth", "status"]) == 2
    assert (await runtime.dingtalk_auth_disconnect(scope))["status"] == "not_connected"
    await runtime.dingtalk_auth_status(scope)
    assert calls.count(["auth", "status"]) == 3


@pytest.mark.asyncio
async def test_authorizing_status_is_always_checked_again(tmp_path, monkeypatch):
    runtime = _connected_status_runtime(tmp_path, [], [_app("employee-1", "ding-app", "secret")])
    checks = []
    monkeypatch.setattr(runtime, "_dingtalk_auth_status", lambda scope: checks.append(1) or {"status": "authorizing"})
    scope = DingTalkCliScope("local", "employee-1", app_id="ding-app")

    await runtime.dingtalk_auth_status(scope)
    await runtime.dingtalk_auth_status(scope)

    assert len(checks) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("known_status", [True, False])
async def test_busy_status_check_returns_last_known_authorization(tmp_path, monkeypatch, known_status):
    monkeypatch.setattr(dingtalk_cli_local_provider, "_AUTH_STATUS_BUSY_WAIT_SECONDS", 0.1)
    monkeypatch.setattr(dingtalk_cli_local_provider, "_OAUTH_LOCK_TIMEOUT_SECONDS", 0.1)
    runtime = _connected_status_runtime(tmp_path, [], [_app("employee-1", "ding-app", "secret")])
    scope = DingTalkCliScope("local", "employee-1", app_id="ding-app")
    if known_status:
        await runtime.dingtalk_auth_status(scope)
        key = runtime._auth_status_key(scope)
        runtime._auth_status_cache[key] = (time.monotonic() - 30, runtime._auth_status_cache[key][1])
    lock = runtime._oauth_locks.setdefault(runtime._scope_digest(scope), threading.RLock())
    lock.acquire()
    try:
        if known_status:
            assert (await runtime.dingtalk_auth_status(scope))["status"] == "connected"
        else:
            with pytest.raises(PersistentAgentError) as error:
                await runtime.dingtalk_auth_status(scope)
            assert error.value.code == "DINGTALK_CLI_AUTH_BUSY"
    finally:
        lock.release()


@pytest.mark.parametrize("has_saved_state", [False, True])
def test_uninitialized_dingtalk_auth_status_does_not_install_cli(tmp_path, monkeypatch, has_saved_state):
    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        npx_path=Path("/tools/npx"),
    )
    scope = DingTalkCliScope("local", "employee-1", app_id="ding-app")
    root = runtime._oauth_root(scope)
    if has_saved_state:
        (root / "saved-cli-profile").write_text("existing private state", encoding="utf-8")
    monkeypatch.setattr(runtime, "_cached_dws_binary", lambda: None)
    calls = []

    def run(argv, *, env, cwd, timeout):
        calls.append(argv)
        payload = ({"success": True, "authenticated": True} if argv[:2] == ["auth", "status"] else
                   {"success": True, "profiles": [{"current": True, "userName": "tester"}]})
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    monkeypatch.setattr(runtime, "_run_cli", run)
    result = runtime._dingtalk_auth_status(scope)
    assert result["status"] == ("connected" if has_saved_state else "not_connected")
    assert len(calls) == (2 if has_saved_state else 0)


def test_dingtalk_program_cache_survives_project_reset_without_restoring_oauth(tmp_path):
    import shutil
    project = tmp_path / 'project'
    programs = tmp_path / 'programs'
    package = programs / 'managed' / 'dingtalk-workspace-cli'
    (package / 'vendor').mkdir(parents=True)
    (package / 'package.json').write_text(json.dumps({'version': '1.0.65'}), encoding='utf-8')
    binary = package / 'vendor' / ('dws.exe' if os.name == 'nt' else 'dws')
    binary.write_bytes(b'program')
    binary.chmod(0o700)
    factory = lambda: LocalDingTalkCliRuntime(
        root=project, cache_root=programs,
        config_loader=lambda: _config(_app('employee-1', 'ding-app', 'secret')),
    )
    runtime = factory()
    scope = DingTalkCliScope('local', 'employee-1')
    oauth = runtime._oauth_root(scope)
    (oauth / 'private-login').write_text('old authorization', encoding='utf-8')
    assert runtime._cached_dws_binary() == binary
    assert runtime._npm_cache_dir().is_relative_to(programs)
    assert oauth.is_relative_to(project)
    shutil.rmtree(project)
    reopened = factory()
    assert reopened._cached_dws_binary() == binary
    assert not (reopened._oauth_root(scope) / 'private-login').exists()


def test_legacy_dingtalk_program_is_migrated_without_copying_private_account_data(tmp_path):
    import shutil
    project, programs = tmp_path / 'project', tmp_path / 'programs'
    package = project / 'npm-cache' / '1.0.65' / '_npx' / 'old' / 'node_modules' / 'dingtalk-workspace-cli'
    (package / 'vendor').mkdir(parents=True)
    (package / 'package.json').write_text(json.dumps({'version': '1.0.65'}), encoding='utf-8')
    binary = package / 'vendor' / ('dws.exe' if os.name == 'nt' else 'dws')
    binary.write_bytes(b'legacy-program')
    binary.chmod(0o700)
    (package / 'private-account-data').write_text('must remain private', encoding='utf-8')
    runtime = LocalDingTalkCliRuntime(root=project, cache_root=programs)
    migrated = runtime._cached_dws_binary()
    assert migrated.is_relative_to(programs)
    assert migrated.read_bytes() == b'legacy-program'
    assert not list(programs.rglob('private-account-data'))
    shutil.rmtree(project)
    reopened = LocalDingTalkCliRuntime(root=project, cache_root=programs)
    assert reopened._cached_dws_binary() == migrated


@pytest.mark.parametrize('platform,relative', [
    ('darwin', 'Library/Caches/WorkSwarm/dingtalk-cli'),
    ('linux', '.cache/workswarm/dingtalk-cli'),
    ('win32', 'AppData/Local/WorkSwarm/Cache/dingtalk-cli'),
])
def test_default_dingtalk_program_cache_is_outside_project_data(tmp_path, monkeypatch, platform, relative):
    import sys
    from jiuwenswarm.common import utils
    from jiuwenswarm.server.jiuwenmate import service
    from jiuwenswarm.server.jiuwenmate import dingtalk_cli_local_provider
    from jiuwenswarm.server.jiuwenmate.dingtalk_cli_local_provider import create_provider
    monkeypatch.setattr(dingtalk_cli_local_provider, '_find_npx', lambda: tmp_path / 'npx')
    monkeypatch.setattr(sys, 'platform', platform)
    monkeypatch.setattr(utils, 'get_user_home', lambda: tmp_path)
    monkeypatch.setattr(service, 'default_data_dir', lambda: tmp_path / '.jiuwenswarm' / 'jiuwenmate')
    for name in ('LOCALAPPDATA', 'XDG_CACHE_HOME', 'JIUWENMATE_DINGTALK_CLI_HOME', 'JIUWENMATE_DWS_BIN'):
        monkeypatch.delenv(name, raising=False)
    runtime = create_provider()
    assert runtime._npm_cache_dir() == tmp_path / relative / 'npm-cache' / '1.0.65'



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
async def test_execute_uses_a_cached_pinned_cli_binary_without_npx(tmp_path, monkeypatch):
    monkeypatch.setattr(dingtalk_cli_local_provider, "_find_npx", lambda: None)
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
        runner=runner,
    )

    await runtime.execute(DingTalkCliScope("local", "employee-1"), ["api", "GET", "/v1.0/test"])

    assert calls == [[str(binary), "api", "GET", "/v1.0/test"]]


@pytest.mark.asyncio
async def test_dingtalk_auth_status_on_clean_install_without_npx(tmp_path, monkeypatch):
    monkeypatch.setattr(dingtalk_cli_local_provider, "_find_npx", lambda: None)
    runtime = LocalDingTalkCliRuntime(
        root=tmp_path / "profiles", cache_root=tmp_path / "programs",
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret")),
        runner=lambda *args, **kwargs: pytest.fail("status must not start the CLI on first use"),
    )
    service = DingTalkCliRuntimeService(runtime=runtime)

    result = await service.dingtalk_auth_status(DingTalkCliScope("local", "employee-1", app_id="ding-app"))

    assert result == {"status": "not_connected", "cli_version": "1.0.65"}
    assert not (tmp_path / "programs").exists()
    with pytest.raises(PersistentAgentError) as error:
        await service.dingtalk_auth_begin(DingTalkCliScope("local", "another-employee", app_id="ding-app"))
    assert error.value.code == "DINGTALK_CLI_APP_NOT_CONFIGURED"


@pytest.mark.asyncio
@pytest.mark.parametrize(("system", "machine", "platform_tag", "binary_name", "suffix"), [
    ("Darwin", "arm64", "darwin-arm64", "dws", ".tar.gz"),
    ("Windows", "AMD64", "windows-amd64", "dws.exe", ".zip"),
])
async def test_clean_dingtalk_authorization_verifies_and_reuses_native_cli(
    tmp_path, monkeypatch, system, machine, platform_tag, binary_name, suffix,
):
    import httpx
    from jiuwenswarm.server.jiuwenmate import dingtalk_cli_distribution as distribution

    monkeypatch.setattr(dingtalk_cli_local_provider, "_find_npx", lambda: None)
    monkeypatch.setattr(distribution.platform, "system", lambda: system)
    monkeypatch.setattr(distribution.platform, "machine", lambda: machine)
    binary_bytes = b"pinned dws executable"
    archive = io.BytesIO()
    if suffix == ".zip":
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr(binary_name, binary_bytes)
    else:
        with tarfile.open(fileobj=archive, mode="w:gz") as package:
            member = tarfile.TarInfo(f"./{binary_name}")
            member.size = len(binary_bytes)
            package.addfile(member, io.BytesIO(binary_bytes))
    monkeypatch.setitem(distribution._RELEASE_SHA256, platform_tag, hashlib.sha256(archive.getvalue()).hexdigest())
    downloads = []
    calls = []
    valid_release = False

    @contextmanager
    def stream(method, url, **kwargs):
        assert method == "GET"
        downloads.append(url)
        content = archive.getvalue() if valid_release and "github.com" in url else b"wrong checksum"
        yield httpx.Response(200, content=content, request=httpx.Request(method, url))

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        assert Path(args[0]).read_bytes() == binary_bytes
        return subprocess.CompletedProcess(args, 0, '{"authenticated":false}', "")

    def login(args, *, stdin, stdout, stderr, env, cwd, start_new_session):
        calls.append(args)
        assert Path(args[0]).read_bytes() == binary_bytes
        assert args[1:] == ["auth", "login", "--device", "--no-browser"]
        stdout.write(b"https://login.dingtalk.com/oauth2/device/verify.htm?user_code=TEST-CODE")
        stdout.flush()
        return type("Pending", (), {"pid": 987654, "poll": lambda self: None})()

    monkeypatch.setattr(distribution.httpx, "stream", stream)
    monkeypatch.setattr(subprocess, "Popen", login)
    programs = tmp_path / "programs"
    runtime = LocalDingTalkCliRuntime(
        root=tmp_path / "profiles", cache_root=programs,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret")),
        runner=runner,
    )
    service = DingTalkCliRuntimeService(runtime=runtime)
    scope = DingTalkCliScope("local", "employee-1", app_id="ding-app")
    (runtime._oauth_root(scope) / "existing-profile").write_text("private state", encoding="utf-8")
    assert (await service.dingtalk_auth_status(scope))["status"] == "error"
    assert not downloads

    with pytest.raises(PersistentAgentError) as error:
        await service.dingtalk_auth_begin(scope)
    assert error.value.code == "DINGTALK_CLI_PREPARE_FAILED"
    assert not list(programs.rglob(binary_name))
    assert not calls

    valid_release = True
    result = await service.dingtalk_auth_begin(scope)
    assert result["status"] == "authorizing"
    assert result["user_code"] == "TEST-CODE"
    assert len(calls) == 1
    assert len(downloads) == 4
    assert all(url.endswith(f"/v1.0.65/dws-{platform_tag}{suffix}") for url in downloads)
    assert ["gitee.com" in url for url in downloads] == [True, False, True, False]

    another_runtime = LocalDingTalkCliRuntime(root=tmp_path / "another-profile", cache_root=programs)
    assert another_runtime._cached_dws_binary() == Path(calls[0][0])
    assert len(downloads) == 4


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


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['permission', 'missing_child', 'incomplete'])
async def test_disconnect_reports_private_profile_cleanup_failure(tmp_path, monkeypatch, failure):
    import shutil
    runtime = LocalDingTalkCliRuntime(
        root=tmp_path, config_loader=lambda: _config(_app('employee-1', 'app', 'secret')),
    )
    scope = DingTalkCliScope('local', 'employee-1', app_id='app')
    private = runtime._oauth_root(scope) / 'private-login'
    private.write_text('fake-login', encoding='utf-8')
    def refuse(path, ignore_errors=False):
        if not ignore_errors:
            if failure == 'permission':
                raise PermissionError('profile is locked')
            if failure == 'missing_child':
                raise FileNotFoundError('a concurrent child disappeared')
    monkeypatch.setattr(shutil, 'rmtree', refuse)
    with pytest.raises(PersistentAgentError) as error:
        await runtime.dingtalk_auth_disconnect(scope)
    assert error.value.code == 'DINGTALK_CLI_AUTH_CLEANUP_FAILED'
    assert private.exists()


@pytest.mark.skipif(os.name == 'nt', reason='A POSIX signal handler exposes a late credential write')
@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['pending', 'connected', 'retry'])
async def test_disconnect_waits_for_the_login_process_before_deleting_credentials(tmp_path, monkeypatch, state):
    import sys
    def runner(args, **options):
        payload = {'authenticated': True} if args[1:3] == ['auth', 'status'] else {
            'profiles': [{'current': True, 'userName': 'Test account', 'corpName': 'Test organization'}],
        }
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), '')
    runtime = LocalDingTalkCliRuntime(
        root=tmp_path, config_loader=lambda: _config(_app('employee-1', 'app', 'secret')),
        dws_path=Path('/tools/dws'), runner=runner,
    )
    scope = DingTalkCliScope('local', 'employee-1', app_id='app')
    root = runtime._oauth_root(scope)
    operation = runtime._scope_digest(scope) + '.' + 'a' * 32
    log, pid, _ = runtime._oauth_paths(scope, operation)
    log.parent.mkdir(parents=True)
    script = (
        'import signal,sys,time\nfrom pathlib import Path\n'
        'def stop(*args):\n'
        ' time.sleep(0.2)\n p=Path(sys.argv[1]);p.mkdir(parents=True,exist_ok=True)\n'
        ' (p/"late-login").write_text("fake-login")\n sys.exit(0)\n'
        'signal.signal(signal.SIGTERM,stop)\nprint("ready",flush=True)\ntime.sleep(30)\n'
    )
    process = subprocess.Popen([sys.executable, '-c', script, str(root)], stdout=subprocess.PIPE, text=True)
    try:
        assert await asyncio.to_thread(process.stdout.readline) == 'ready\n'
        pid.write_text(str(process.pid), encoding='utf-8')
        runtime._oauth_current(scope).write_text(operation, encoding='utf-8')
        if state == 'connected':
            assert (await runtime.dingtalk_auth_status(scope))['status'] == 'connected'
            assert pid.exists(), 'A live login process must remain tracked after credentials are visible'
        if state == 'retry':
            monkeypatch.setattr(runtime, '_dingtalk_auth_status', lambda scope: {
                'status': 'error', 'error_code': 'org_access_denied', 'cli_version': 'test',
            })
            def cannot_start(*args, **options):
                raise OSError('new login process cannot start')
            monkeypatch.setattr(subprocess, 'Popen', cannot_start)
            with pytest.raises(PersistentAgentError) as error:
                await runtime.dingtalk_auth_begin(scope)
            assert error.value.code == 'DINGTALK_CLI_AUTH_START_FAILED'
            assert process.poll() is not None, 'Retrying must stop the old flow before dropping its PID'
        await runtime.dingtalk_auth_disconnect(scope)
        assert process.poll() is not None
        assert not root.exists()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stdout.close()


@pytest.mark.asyncio
async def test_disconnect_can_clean_private_state_without_a_cli_binary(tmp_path, monkeypatch):
    from jiuwenswarm.server.jiuwenmate.dingtalk_cli_runtime import DingTalkCliRuntimeService
    runtime = LocalDingTalkCliRuntime(
        root=tmp_path, config_loader=lambda: _config(_app('employee-1', 'app', 'secret')),
    )
    monkeypatch.setattr(runtime, '_npx_path', None)
    scope = DingTalkCliScope('local', 'employee-1', app_id='app')
    private = runtime._oauth_root(scope)
    (private / 'private-login').write_text('fake-login', encoding='utf-8')
    result = await DingTalkCliRuntimeService(runtime=runtime).dingtalk_auth_disconnect(scope)
    assert result['status'] == 'not_connected'
    assert not private.exists()


@pytest.mark.asyncio
async def test_disconnect_clears_old_credential_revisions_without_touching_other_employee_or_program_cache(tmp_path):
    config = _config(_app('employee-1', 'app', 'old-secret'))
    runtime = LocalDingTalkCliRuntime(root=tmp_path, config_loader=lambda: config)
    scope = DingTalkCliScope('local', 'employee-1', app_id='app')
    previous = runtime._oauth_root(scope)
    (previous / 'private-login').write_text('old-login', encoding='utf-8')
    config['channels']['dingtalk']['apps'][0]['client_secret'] = 'new-secret'
    current = runtime._oauth_root(scope)
    (current / 'private-login').write_text('new-login', encoding='utf-8')
    other = runtime._scope_root(DingTalkCliScope('local', 'employee-2')) / 'user-oauth' / 'other-login'
    other.parent.mkdir(parents=True)
    other.write_text('other-employee', encoding='utf-8')
    program = runtime._npm_cache_dir() / 'program'
    program.parent.mkdir(parents=True)
    program.write_text('pinned-program', encoding='utf-8')
    await runtime.dingtalk_auth_disconnect(scope)
    assert not previous.exists() and not current.exists()
    assert other.read_text(encoding='utf-8') == 'other-employee'
    assert program.read_text(encoding='utf-8') == 'pinned-program'


@pytest.mark.asyncio
@pytest.mark.parametrize('online_document', [False, True])
@pytest.mark.parametrize('separate_runtime', [False, True])
async def test_disconnect_waits_for_oauth_commands_before_removing_their_late_writes(tmp_path, online_document, separate_runtime):
    entered, release = threading.Event(), threading.Event()
    def runner(args, *, env, **options):
        entered.set()
        assert release.wait(timeout=5)
        profile = Path(env['DWS_CONFIG_DIR'])
        profile.mkdir(parents=True, exist_ok=True)
        (profile / 'refreshed-private-login').write_text('fake-login', encoding='utf-8')
        payload = {'status': 'success'} if online_document else {'success': True}
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), '')
    runtime = LocalDingTalkCliRuntime(
        root=tmp_path, config_loader=lambda: _config(_app('employee-1', 'app', 'secret')),
        dws_path=Path('/tools/dws'), runner=runner,
    )
    cleanup_runtime = LocalDingTalkCliRuntime(
        root=tmp_path, config_loader=lambda: _config(_app('employee-1', 'app', 'secret')),
    ) if separate_runtime else runtime
    scope = DingTalkCliScope('local', 'employee-1', app_id='app')
    root = runtime._oauth_root(scope)
    command = (runtime._execute_online_document(scope, ['doc', '+fetch', 'node'], profile='test-profile', error_code='TEST')
               if online_document else runtime._execute_oauth_command(scope, ['drive', 'info', '--node', 'node'], profile='test-profile'))
    running = asyncio.create_task(command)
    cleanup = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        cleanup = asyncio.create_task(cleanup_runtime.dingtalk_auth_disconnect(scope))
        await asyncio.sleep(0.05)
        assert not cleanup.done(), 'Cleanup must wait until the process can no longer refresh private credentials'
        release.set()
        await running
        assert (await cleanup)['status'] == 'not_connected'
        assert not root.exists()
    finally:
        release.set()
        await asyncio.gather(running, *([cleanup] if cleanup is not None else []), return_exceptions=True)


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


@pytest.mark.parametrize('has_profile_user_id', [False, True])
def test_oauth_display_lookup_has_a_short_process_budget_without_losing_verified_login(tmp_path, has_profile_user_id):
    budgets = []
    def runner(args, *, timeout, **options):
        budgets.append(timeout)
        if args[1:3] == ['auth', 'status']:
            body = {'authenticated': True}
        elif args[1:3] == ['profile', 'list']:
            profile = {'profile': 'corp', 'corpId': 'corp', 'corpName': 'Resolved organization', 'isCurrent': True}
            if has_profile_user_id:
                profile['userId'] = 'opaque-user-id'
            body = {'profiles': [profile]}
        else:
            assert 'contact' in args and '+me' in args
            assert int(args[args.index('--timeout') + 1]) <= 3
            raise subprocess.TimeoutExpired(args, timeout)
        return subprocess.CompletedProcess(args, 0, json.dumps(body), '')
    runtime = LocalDingTalkCliRuntime(
        root=tmp_path, config_loader=lambda: _config(_app('employee-1', 'app', 'secret')),
        dws_path=Path('/tools/dws'), runner=runner,
    )
    status = runtime._dingtalk_auth_status(DingTalkCliScope('local', 'employee-1'))
    assert status['status'] == 'connected'
    assert len(budgets) == 3 and max(budgets[:2]) <= 5 and budgets[2] <= 4
    assert 'account' not in status


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


@pytest.mark.asyncio
@pytest.mark.parametrize('prepare_ok', [True, False])
async def test_device_authorization_prepares_cli_before_starting_login(tmp_path, monkeypatch, prepare_ok):
    runtime = LocalDingTalkCliRuntime(
        root=tmp_path, config_loader=lambda: _config(_app('employee-1', 'app', 'secret')),
        npx_path=Path('/tools/npx'),
    )
    cached = []
    events = []
    binary = tmp_path / 'prepared-dws'
    monkeypatch.setattr(runtime, '_cached_dws_binary', lambda: cached[0] if cached else None)
    monkeypatch.setattr(runtime, '_dingtalk_auth_status', lambda scope: {'status': 'not_connected'})

    def prepare(argv, *, env, cwd, timeout=None):
        events.append('prepare')
        assert argv == ['--version'] and timeout is not None
        if prepare_ok:
            binary.write_text('prepared', encoding='utf-8')
            cached.append(binary)
        return subprocess.CompletedProcess(argv, 0 if prepare_ok else 1, '1.0.65', '')

    def login(argv, *, stdin, stdout, stderr, env, cwd, start_new_session):
        events.append('login')
        assert argv[0] == str(binary)
        assert argv[1:] == ['auth', 'login', '--device', '--no-browser']
        stdout.write(b'https://login.dingtalk.com/oauth2/device/verify.htm?user_code=TEST-CODE')
        stdout.flush()
        return type('Pending', (), {'pid': 987654, 'poll': lambda self: None})()

    monkeypatch.setattr(runtime, '_run_cli', prepare)
    monkeypatch.setattr(subprocess, 'Popen', login)
    if prepare_ok:
        result = await runtime.dingtalk_auth_begin(DingTalkCliScope('local', 'employee-1'))
        assert result['status'] == 'authorizing' and result['user_code'] == 'TEST-CODE'
        assert events == ['prepare', 'login']
    else:
        with pytest.raises(PersistentAgentError) as error:
            await runtime.dingtalk_auth_begin(DingTalkCliScope('local', 'employee-1'))
        assert error.value.code == 'DINGTALK_CLI_PREPARE_FAILED'
        assert events == ['prepare']


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
