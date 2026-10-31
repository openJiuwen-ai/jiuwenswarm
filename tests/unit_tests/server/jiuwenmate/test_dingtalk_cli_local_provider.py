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


@pytest.mark.asyncio
async def test_create_group_document_creates_group_docx_when_root_uuid_is_unavailable(tmp_path):
    calls = []
    uploaded = []

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
            body = {"dentry": {"id": "dentry-7", "uuid": "doc-uuid-7", "name": "test.docx"}}
        elif path == "/v1.0/storage/spaces/group-space-9/dentries/dentry-7/openInfos/query":
            body = {"url": "https://alidocs.dingtalk.com/i/nodes/doc-uuid-7"}
        else:
            raise AssertionError(f"unexpected API request: {args}")
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

    async def upload(url, headers, data):
        uploaded.append((url, headers, data))

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        dws_path=Path("/tools/dws"),
        runner=runner,
        signed_upload=upload,
    )

    result = await runtime.create_group_document(
        DingTalkCliScope("local", "employee-1"),
        conversation_id="cid-current-group",
        sender_staff_id="staff-42",
        title="test",
        content="",
    )

    assert result["url"] == "https://alidocs.dingtalk.com/i/nodes/doc-uuid-7"
    assert result["documentType"] == "docx_file"
    assert result["dentryId"] == "dentry-7"
    assert result["dentryUuid"] == "doc-uuid-7"
    assert len(uploaded) == 1
    assert uploaded[0][0] == "https://storage.aliyuncs.com/signed-upload?secret=private"
    assert uploaded[0][1] == {"x-oss-signature": "signed-header"}
    assert uploaded[0][2].startswith(b"PK")
    assert all("secret=private" not in " ".join(call) for call in calls)
    assert len(calls) == 5
    commit_args = calls[3]
    commit_payload = json.loads(commit_args[commit_args.index("--data") + 1])
    assert calls[3][3] == "/v1.0/storage/spaces/group-space-9/files/commit"
    assert "convertToOnlineDoc" not in commit_payload["option"]
    assert commit_payload["parentId"] == "0"


@pytest.mark.asyncio
async def test_group_calendar_reads_members_with_app_token_without_user_login(tmp_path):
    calls = []

    def runner(args, *, env, input_text=None, timeout=None, cwd):
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
async def test_group_calendar_falls_back_to_existing_shared_dws_user_session_on_app_token_forbidden(
    tmp_path, monkeypatch
):
    calls = []
    monkeypatch.setenv("DWS_CONFIG_DIR", "/tmp/private-app-token-cache")
    monkeypatch.setenv("DWS_CACHE_DIR", "/tmp/private-app-token-cache/cache")

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        del input_text, timeout, cwd
        calls.append(args)
        if args[1:4] == ["api", "POST", "/v1.0/im/innerGroups/members/query"]:
            error = {"error": {"message": "requiredScopes=[Chat.OrgInnerGroup.Read]"}}
            return subprocess.CompletedProcess(args, 1, "", json.dumps(error))
        if args[1:4] == ["chat", "group", "members"]:
            assert args[4:8] == ["--id", "cid-current-group", "--cursor", "0"]
            assert env.get("DWS_CLIENT_ID") is None
            assert env.get("DWS_CLIENT_SECRET") is None
            assert env.get("DWS_CONFIG_DIR") is None
            assert env.get("DWS_CACHE_DIR") is None
            assert "DWS_DISABLE_KEYCHAIN" not in env
            assert env["HOME"] == str(Path.home())
            body = {
                "success": True,
                "members": [{"userId": "staff-42"}, {"userId": "staff-7"}],
                "hasMore": False,
                "nextCursor": "0",
            }
        else:
            endpoint = args[3]
            request = json.loads(args[args.index("--data") + 1])
            if endpoint == "/topapi/v2/user/get":
                body = {"result": {"unionid": f"union-{request['userid']}"}}
            elif endpoint == "/v1.0/calendar/users/union-staff-42/calendars/primary/events":
                assert [item["id"] for item in request["attendees"]] == ["union-staff-7"]
                body = {"id": "event-shared-dws-1"}
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

    assert result["eventId"] == "event-shared-dws-1"
    assert result["attendeeCount"] == 1
    assert any(call[1:4] == ["chat", "group", "members"] for call in calls)
    assert len([call for call in calls if len(call) > 3 and call[3] == "/topapi/v2/user/get"]) == 2


@pytest.mark.asyncio
async def test_group_calendar_stops_when_shared_dws_user_session_is_unavailable(tmp_path):
    calls = []

    def runner(args, *, env, input_text=None, timeout=None, cwd):
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
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_create_group_calendar_event_does_not_create_when_member_lookup_fails(tmp_path):
    calls = []

    def runner(args, *, env, input_text=None, timeout=None, cwd):
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
async def test_create_group_document_uses_v2_online_doc_conversion_when_root_uuid_is_available(tmp_path):
    calls = []

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        calls.append(args)
        path = args[3]
        if path == "/topapi/v2/user/get":
            body = {"result": {"unionid": "union-42"}}
        elif path == "/v1.0/convFile/conversations/spaces/query":
            body = {"space": {"spaceId": "group-space-9", "rootDentryUuid": "root-uuid-9"}}
        elif path == "/v2.0/storage/spaces/files/root-uuid-9/uploadInfos/query":
            body = {
                "uploadKey": "upload-key",
                "headerSignatureInfo": {
                    "resourceUrls": ["https://storage.aliyuncs.com/signed-upload?secret=private"],
                    "headers": {"x-oss-signature": "signed-header"},
                },
            }
        elif path == "/v2.0/storage/spaces/files/root-uuid-9/commit":
            body = {"dentry": {"id": "dentry-7", "uuid": "doc-uuid-7", "name": "test.docx"}}
        elif path == "/v1.0/storage/spaces/group-space-9/dentries/dentry-7/openInfos/query":
            body = {"url": "https://alidocs.dingtalk.com/i/nodes/doc-uuid-7"}
        else:
            raise AssertionError(f"unexpected API request: {args}")
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

    result = await runtime.create_group_document(
        DingTalkCliScope("local", "employee-1"),
        conversation_id="cid-current-group",
        sender_staff_id="staff-42",
        title="test",
    )

    assert result["documentType"] == "online_doc"
    assert result["url"] == "https://alidocs.dingtalk.com/i/nodes/doc-uuid-7"
    commit = calls[3]
    payload = json.loads(commit[commit.index("--data") + 1])
    assert commit[3] == "/v2.0/storage/spaces/files/root-uuid-9/commit"
    assert payload["option"]["convertToOnlineDoc"] is True
    assert payload["option"]["convertToOnlineDocTargetDocumentType"] == "DOC"


@pytest.mark.asyncio
async def test_group_document_creation_remains_successful_when_preview_link_lookup_fails(tmp_path):
    calls = []

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        calls.append(args)
        path = args[3]
        if path == "/topapi/v2/user/get":
            body = {"result": {"unionid": "union-42"}}
            return subprocess.CompletedProcess(args, 0, json.dumps(body), "")
        if path == "/v1.0/convFile/conversations/spaces/query":
            body = {"space": {"spaceId": "group-space-9"}}
            return subprocess.CompletedProcess(args, 0, json.dumps(body), "")
        if path == "/v1.0/storage/spaces/group-space-9/files/uploadInfos/query":
            body = {
                "uploadKey": "upload-key",
                "headerSignatureInfo": {
                    "resourceUrls": ["https://storage.aliyuncs.com/signed-upload?secret=private"],
                    "headers": {"x-oss-signature": "signed-header"},
                },
            }
            return subprocess.CompletedProcess(args, 0, json.dumps(body), "")
        if path == "/v1.0/storage/spaces/group-space-9/files/commit":
            body = {"dentry": {"id": "dentry-7", "uuid": "doc-uuid-7"}}
            return subprocess.CompletedProcess(args, 0, json.dumps(body), "")
        if path.endswith("/dentries/dentry-7/openInfos/query"):
            return subprocess.CompletedProcess(args, 1, "", "HTTP 403 Forbidden")
        raise AssertionError(f"unexpected API request: {args}")

    async def upload(url, headers, data):
        return None

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path,
        config_loader=lambda: _config(_app("employee-1", "ding-app", "secret", is_default=True)),
        dws_path=Path("/tools/dws"),
        runner=runner,
        signed_upload=upload,
    )

    result = await runtime.create_group_document(
        DingTalkCliScope("local", "employee-1"),
        conversation_id="cid-current-group",
        sender_staff_id="staff-42",
        title="test",
    )

    assert result["created"] is True
    assert result["documentType"] == "docx_file"
    assert result["linkUnavailable"] is True
    assert "url" not in result
    assert len(calls) == 5


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
