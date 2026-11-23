"""Cross-group candidates stay host-private until the owner authorizes their destination."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.harness import persistent_agent as core
from jiuwenswarm.server.jiuwenmate.execution_models import ConversationRef, RequestRouteSnapshot
from jiuwenswarm.server.jiuwenmate.group_share_repository import GroupShareRepository
from jiuwenswarm.server.jiuwenmate.group_sharing_service import GroupSharingService, GuardedGroupChatPort

NOW = datetime(2026, 10, 27, 8, tzinfo=timezone.utc)


@pytest.fixture
def sharing(tmp_path):
    execution = core.ExecutionContext(
        namespace_id="local", agent_id="agent-1", session_id="session-1", request_id="request-1",
        execution_id="execution-1", source_ref="source-1", config_version="v1", config_fingerprint="a" * 64,
        principal_ref="feishu:user:someone", startup_scope=frozenset({"chat"}), source_scope=frozenset({"chat"}),
        policy_revision="policy-1", capability_snapshot=frozenset({"workspace_read"}),
    )
    message = core.GroupMessage(message_id="m-1", sender_id="user-1", sender_name="Alice",
                                sender_kind="human", received_at=NOW, text="private launch plan")
    hit = core.GroupMessageSearchHit(group_id="source-group", group_name="Source group",
                                    message_ref="message-ref", message=message)
    result = core.GroupMessageSearchResult(**core.InvocationRef.from_execution(execution).model_dump(),
                                          status="found", hits=(hit,), query_complete=True)
    record = SimpleNamespace(
        namespace_id="local", agent_id="agent-1", request_id="request-1", session_id="session-1",
        access=SimpleNamespace(principal_ref=execution.principal_ref),
        request_input=SimpleNamespace(kind="group_message", source=SimpleNamespace(sender_name="Bob")),
        route_snapshot=RequestRouteSnapshot(channel="feishu", app_id="app-1", target_id="chat-target",
                                            source_message_id="message-current"),
    )
    sessions = SimpleNamespace(
        get_request=lambda ns, rid: SimpleNamespace(record=record),
        get_session=lambda ns, sid: SimpleNamespace(conversation=ConversationRef.group("target-group")),
    )
    groups = SimpleNamespace(known_name_of=lambda ns, gid: "Destination group")
    archive = SimpleNamespace(current_messages=lambda ns, gid, ids: (message,))
    service = SimpleNamespace(connection_of=lambda ns, aid: SimpleNamespace(config_id="config-1"))
    repo = GroupShareRepository(tmp_path / "agents.sqlite3")
    gate = GroupSharingService(service=service, sessions=sessions, groups=groups, archive=archive,
                               repository=repo, clock=lambda: NOW)
    delegate = SimpleNamespace(
        search_messages=AsyncMock(return_value=result),
        sharing_sources=AsyncMock(return_value=({"group_id": "source-group", "name": "Source group"},)),
    )
    return SimpleNamespace(execution=execution, query=core.GroupMessageQuery(query="launch"), result=result,
                           record=record, sessions=sessions, groups=groups, archive=archive, repo=repo,
                           gate=gate, delegate=delegate, port=GuardedGroupChatPort(delegate, gate))




@pytest.mark.parametrize("kind", ["file", "online_doc"])
async def test_artifact_tool_releases_content_only_after_owner_approval(sharing, tmp_path, kind):
    from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
    from openjiuwen.harness.persistent_agent.tools.group_history import PersistentGroupMessageSearchTool

    row = dict(
        artifact_id="artifact-1", source_group_id="source-group", artifact_kind=kind,
        title="launch report", snippet="private launch result", created_at=NOW.isoformat(),
    )
    imports = []
    if kind == "file":
        source = tmp_path / "report.txt"
        source.write_text("launch", encoding="utf-8")
        row.update(session_id="source-session", file_name="report.txt", file_size=6,
                   file_ref="sha256:" + "a" * 64, file_path="sessions/source-session/output/report.txt")

        def import_file(*args):
            imports.append(args)
            return "sessions/session-1/imports/report.txt"

        sharing.gate._files = SimpleNamespace(
            resolve=lambda *args, **kwargs: source,
            search_text=lambda *args: source.read_text(encoding="utf-8"),
            import_approved=import_file,
        )
    else:
        row["url"] = "https://example.feishu.cn/docx/launch"
    sharing.sessions.search_session_artifacts = lambda *args, **kwargs: [row]
    sharing.delegate.sharing_sources.return_value = ({
        "group_id": "source-group", "name": "Source group", "visible_from": (NOW - timedelta(days=1)).isoformat(),
    },)
    tool = PersistentGroupMessageSearchTool(execution=sharing.execution, group_chat=sharing.port)
    args = {"query": "launch", "content_types": ["files" if kind == "file" else "online_docs"]}
    with pytest.raises(ToolInterruptException) as caught:
        await tool.invoke(args)
    assert caught.value.request.metadata["kind"] == "group_share"
    assert not imports
    pending, = sharing.repo.pending_for_request("local", "request-1")
    sharing.repo.bind_native("local", "request-1", "native-1")
    sharing.repo.decide(pending["approval_id"], namespace_id="local", config_id="config-1",
                        owner_principal="local", decision="once", now=NOW)
    result = await tool.invoke(args)
    assert result.success and result.data["artifacts"][0]["artifact_kind"] == kind
    assert "source_file_path" not in result.data["artifacts"][0]
    assert bool(imports) == (kind == "file")
    sharing.delegate.search_messages.assert_not_called()


async def approve(sharing, decision="once"):
    with pytest.raises(core.GroupContentApprovalRequired):
        await sharing.port.search_messages(sharing.execution, sharing.query)
    row, = sharing.repo.pending_for_request("local", "request-1")
    sharing.repo.bind_native("local", "request-1", "native-1")
    sharing.repo.decide(row["approval_id"], namespace_id="local", config_id="config-1", owner_principal="local",
                        decision=decision, now=NOW)
    return row












async def test_current_group_search_does_not_ask_owner(sharing):
    sharing.sessions.get_session = lambda *args: SimpleNamespace(conversation=ConversationRef.group("source-group"))
    result = await sharing.port.search_messages(sharing.execution, sharing.query)
    assert result == sharing.result
    assert not sharing.repo.pending_for_request("local", "request-1")




async def test_no_search_match_returns_no_candidates_or_share_request(sharing):
    sharing.delegate.search_messages.return_value = sharing.result.model_copy(update={"status": "empty", "hits": ()})
    result = await sharing.port.search_messages(sharing.execution, sharing.query)
    assert result.status == "empty" and not result.hits and not result.artifacts
    assert not sharing.repo.pending_for_request("local", "request-1")








def delivery_job():
    return {"namespace_id": "local", "agent_id": "agent-1", "request_id": "request-1",
            "channel": "feishu", "app_id": "app-1", "target_id": "chat-target",
            "request_kind": "group_message", "is_direct_message": False}










@pytest.mark.parametrize("remaining", [(), ({"group_id": "target-group", "name": "Current group"},)])
async def test_source_access_removed_after_search_never_releases_cross_group_hits(sharing, remaining):
    sharing.delegate.sharing_sources.side_effect = [
        ({"group_id": "source-group", "name": "Source group"},), remaining,
    ]
    result = await sharing.port.search_messages(sharing.execution, sharing.query)
    assert result.status == "unavailable" and not result.hits
    assert not sharing.repo.pending_for_request("local", "request-1")


async def test_unlisted_result_source_is_refused_even_when_no_cross_scope_remains(sharing):
    sharing.delegate.sharing_sources.return_value = ()
    result = await sharing.port.search_messages(sharing.execution, sharing.query)
    assert result.status == "unavailable" and not result.hits




@pytest.mark.parametrize("fail", [False, True])
async def test_delivery_guard_strips_content_and_file_upload_on_revoke_or_check_error(fail):
    from jiuwenswarm.server.jiuwenmate.delivery_service import ResultDeliveryService

    jobs = [{"delivery_id": "d-1", "request_id": "r-1", "channel": "feishu", "app_id": "app-1",
             "target_id": "chat-1", "text": "private body", "file": {"path": "private.txt", "file_key": "uploaded"}}]
    repository = SimpleNamespace(
        claim_pending_result_deliveries=lambda *args, **kwargs: [jobs.pop()] if jobs else [],
        replace_result_delivery_with_notice=lambda *args, **kwargs: None,
        update_result_delivery=lambda *args, **kwargs: None,
    )
    send = AsyncMock(return_value={"outcome": "delivered", "external_message_id": "message-sent"})
    deliveries = ResultDeliveryService(repository, namespace_id="local", send_delivery=send)
    deliveries.reconcile_archives = AsyncMock()
    deliveries.set_sharing_guard(AsyncMock(side_effect=RuntimeError("unavailable")) if fail else AsyncMock(return_value=False))
    assert await deliveries.dispatch_pending() == 1
    sent = send.await_args.args[0]
    assert "private body" not in str(sent) and "file" not in sent and "uploaded" not in str(sent)

