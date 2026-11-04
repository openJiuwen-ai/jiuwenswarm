"""Only the trusted local owner's Web destination may skip sharing consent."""

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.harness import persistent_agent as core
from jiuwenswarm.server.jiuwenmate.execution_models import ConversationRef, RequestRouteSnapshot
from jiuwenswarm.server.jiuwenmate.session_artifact_sharing import SessionArtifactSharing
from tests.unit_tests.server.jiuwenmate.test_group_sharing_service import sharing, NOW
from tests.unit_tests.server.jiuwenmate.test_cli_group_read import cli_host
from tests.unit_tests.server.jiuwenmate.test_session_artifact_sharing import artifact_host


def owner_request(host):
    execution = replace(host.execution, principal_ref="local", policy_revision="local", source_ref="web:message-1")
    host.record.access = SimpleNamespace(principal_ref="local", policy_revision="local")
    host.record.source_ref = execution.source_ref
    host.record.request_input.kind = "direct_message"
    host.record.request_input.source.sender_id = "local"
    host.record.route_snapshot = RequestRouteSnapshot(channel="web", target_id="owner-chat",
                                                     source_message_id="message-1")
    host.sessions.get_session = lambda *args: SimpleNamespace(
        agent_id=execution.agent_id, conversation=ConversationRef.web("owner-chat"),
    )
    return execution


@pytest.mark.parametrize("platform", ["feishu", "dingtalk", "welink"])
async def test_owner_web_search_returns_content_without_approval(sharing, platform):
    execution = owner_request(sharing)
    sharing.delegate.sharing_sources.return_value = ({
        "group_id": "source-group", "name": "Source group", "platform": platform,
    },)
    result = await sharing.port.search_messages(execution, sharing.query)
    assert result.hits[0].message.text == "private launch plan"
    assert not sharing.repo.pending_for_request("local", execution.request_id)
    assert sharing.repo.list_for_owner("local", "config-1") == {"approvals": [], "grants": []}
    receipt, = sharing.repo.approvals_for_session("local", execution.agent_id, execution.session_id)
    assert receipt["status"] == "owner_read"
    assert sharing.repo.window_generation("local", execution.agent_id, execution.session_id) == ""


@pytest.mark.parametrize("change", ["principal", "session_agent", "destination", "im_direct", "group", "source"])
async def test_non_owner_or_changed_destination_still_requires_approval(sharing, change):
    execution = owner_request(sharing)
    if change == "principal":
        execution = replace(execution, principal_ref="visitor")
        sharing.record.access.principal_ref = "visitor"
    elif change == "session_agent":
        sharing.sessions.get_session = lambda *args: SimpleNamespace(
            agent_id="another-agent", conversation=ConversationRef.web("owner-chat"),
        )
    elif change == "destination":
        sharing.record.route_snapshot = RequestRouteSnapshot(channel="web", target_id="another-chat")
    elif change == "im_direct":
        sharing.sessions.get_session = lambda *args: SimpleNamespace(
            agent_id=execution.agent_id, conversation=ConversationRef.im_direct("dingtalk", "owner-chat"),
        )
        sharing.record.route_snapshot = RequestRouteSnapshot(channel="dingtalk", app_id="ding-app",
                                                             target_id="owner-chat", source_message_id="message-1")
    elif change == "group":
        sharing.sessions.get_session = lambda *args: SimpleNamespace(
            agent_id=execution.agent_id, conversation=ConversationRef.group("target-group"),
        )
        sharing.record.request_input.kind = "group_message"
    else:
        sharing.record.source_ref = "feishu:untrusted-message"
    with pytest.raises(core.GroupContentApprovalRequired):
        await sharing.port.search_messages(execution, sharing.query)


async def test_owner_does_not_bypass_source_access_or_recall(sharing):
    execution = owner_request(sharing)
    sharing.delegate.sharing_sources.side_effect = ValueError("binding removed")
    result = await sharing.port.search_messages(execution, sharing.query)
    assert result.status == "unavailable" and not result.hits
    sharing.delegate.sharing_sources.side_effect = None
    sharing.archive.current_messages = lambda *args: (sharing.result.hits[0].message.model_copy(
        update={"text": None, "status": "recalled"}),)
    result = await sharing.port.search_messages(execution, sharing.query)
    assert not result.hits


async def test_owner_cli_read_keeps_private_receipt_without_approval(cli_host):
    from jiuwenswarm.server.jiuwenmate.cli_group_commands import parse_group_read

    execution = owner_request(cli_host)
    command = "lark-cli im +chat-messages-list --chat-id oc_source"
    result = await cli_host.handler.execute(execution, command, parse_group_read(command))
    assert result["ok"] and "PRIVATE-CLI-BODY" in json.dumps(result)
    assert not cli_host.repo.pending_for_request("local", execution.request_id)
    receipt, = cli_host.repo.approvals_for_session("local", execution.agent_id, execution.session_id)
    assert receipt["status"] == "owner_read"


async def test_owner_cli_attachment_is_imported_without_a_card(cli_host):
    from pathlib import Path
    from jiuwenswarm.server.jiuwenmate.cli_group_commands import parse_group_read

    execution = owner_request(cli_host)
    cli_host.raw.update(msg_type="file", content=json.dumps({"file_key": "file_a", "file_name": "report.txt"}))
    command = "lark-cli im +messages-resources-download --message-id om_source --file-key file_a --type file"
    result = await cli_host.handler.execute(execution, command, parse_group_read(command))
    file, = result["data"]["files"]
    assert (cli_host.workspace / "imports" / Path(file["file_path"]).name).read_bytes() == b"private attachment"
    assert cli_host.repo.list_for_owner("local", "config-1") == {"approvals": [], "grants": []}


async def test_owner_file_read_and_later_cache_read_do_not_ask_again(artifact_host):
    execution = owner_request(artifact_host)
    guard = SessionArtifactSharing(artifact_host.gate, artifact_host.delegate)
    result = await guard.prepare(execution, "read_file", {"file_path": artifact_host.source["file_path"]})
    assert result.success and "imports/" in str(result.data)
    receipt, = artifact_host.repo.approvals_for_session("local", execution.agent_id, execution.session_id)
    artifact_host.record.request_id = "request-2"
    next_execution = replace(execution, request_id="request-2", execution_id="execution-2")
    assert await guard.prepare(next_execution, "read_file", {"file_path": "imports/art_original_report.txt"}) is None
    assert not artifact_host.repo.pending_for_request("local", next_execution.request_id)
    assert receipt["status"] == "owner_read"


async def test_owner_delivery_cannot_be_redirected_to_a_group(sharing):
    execution = owner_request(sharing)
    await sharing.port.search_messages(execution, sharing.query)
    job = {"namespace_id": "local", "request_id": execution.request_id, "agent_id": execution.agent_id,
           "channel": "web", "target_id": "owner-chat", "request_kind": "direct_message"}
    assert await sharing.gate.can_deliver(job)
    assert not await sharing.gate.can_deliver({**job, "channel": "feishu", "target_id": "other-group",
                                              "request_kind": "group_message"})


async def test_owner_delivery_still_checks_current_source_membership(sharing):
    execution = owner_request(sharing)
    await sharing.port.search_messages(execution, sharing.query)
    sharing.groups.active_accesses = lambda *args: (SimpleNamespace(
        group_id="source-group", app_id="app-1", visible_from=NOW,
    ),)
    sharing.groups.get = lambda *args: SimpleNamespace(platform="dingtalk")
    sharing.gate._verify_membership = AsyncMock(return_value="left")
    assert not await sharing.gate.can_deliver({"namespace_id": "local", "request_id": execution.request_id})


@pytest.mark.parametrize("platform", ["feishu", "dingtalk", "welink"])
async def test_common_membership_checks_are_platform_specific(monkeypatch, platform):
    from jiuwenswarm.server.jiuwenmate.group_access import configure_group_access
    from jiuwenswarm.server.jiuwenmate import feishu_group_fanout, dingtalk_inbound, welink_inbound

    binding = SimpleNamespace(namespace_id="local", agent_id="agent-1", app_id="app-1")
    monkeypatch.setattr(feishu_group_fanout, "resolve_configured_feishu_bindings", lambda *args: (binding,))
    monkeypatch.setattr(dingtalk_inbound, "configured_dingtalk_bindings", lambda: (binding,))
    monkeypatch.setattr(welink_inbound, "welink_group_membership_status", lambda access: access.status)
    manager = SimpleNamespace(group_chats=SimpleNamespace(), group_archive=SimpleNamespace())
    lookup = AsyncMock(return_value={"status": "active"})
    configure_group_access(manager, lookup)
    access = SimpleNamespace(namespace_id="local", agent_id="agent-1", app_id="app-1", status="active")
    group = SimpleNamespace(platform=platform, platform_chat_id="chat-1")
    assert await manager.verify_group_membership(access, group) == "active"
    if platform == "feishu":
        lookup.assert_awaited_once_with("app-1", "chat-1")
    else:
        lookup.assert_not_awaited()
    access.agent_id = "unbound-agent"
    if platform != "welink":
        assert await manager.verify_group_membership(access, group) == "unknown"


async def test_platform_refresh_does_not_send_dingtalk_messages_to_feishu():
    from jiuwenswarm.server.jiuwenmate.group_access import configure_group_access

    manager = SimpleNamespace(group_chats=SimpleNamespace(), group_archive=SimpleNamespace())
    lookup = AsyncMock(side_effect=AssertionError("DingTalk must not call Feishu"))
    configure_group_access(manager, lookup)
    messages = (SimpleNamespace(message_id="ding-message"),)
    assert await manager.refresh_group_messages(SimpleNamespace(), SimpleNamespace(platform="dingtalk"), messages) == messages
    lookup.assert_not_awaited()


async def test_web_runtime_wires_access_before_start_and_recovery(monkeypatch):
    from jiuwenswarm.server.jiuwenmate import web_adapter

    manager = SimpleNamespace(start=AsyncMock(), recover=AsyncMock(), db_path="unused.sqlite3",
                              group_chats=SimpleNamespace(), group_archive=SimpleNamespace())
    configured = []
    monkeypatch.setattr(web_adapter, "WebConversationStore", lambda path: object())
    monkeypatch.setattr(web_adapter, "configure_group_access", lambda *args: configured.append(args))
    lookup = AsyncMock()
    adapter = web_adapter.JiuwenMateWebAdapter(service_factory=lambda: object(),
        manager_factory=lambda *args, **kwargs: manager, inspect_feishu_group=lookup)
    monkeypatch.setattr(adapter, "refresh_models", AsyncMock())
    async def start():
        assert configured == [(manager, lookup)]
    manager.start.side_effect = start
    assert await adapter._runtime() is manager
