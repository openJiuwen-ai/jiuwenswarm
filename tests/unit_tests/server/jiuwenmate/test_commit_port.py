"""SW-04: CommitPort event log, critical durability and candidate outputs."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from datetime import datetime, timedelta, timezone

import pytest
from jiuwenswarm.server.jiuwenmate.execution_models import (
    AccessSnapshot,
    ConversationRef,
    RequestRecord,
    RequestRouteSnapshot,
    build_execution_context,
)
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.ports.commit import CommitQueue, HostCommitPort
from jiuwenswarm.server.jiuwenmate.spec_provider import publish_spec

from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
from openjiuwen.core.foundation.store import InMemoryKVStore
from openjiuwen.harness import persistent_agent as core
from openjiuwen.harness.schema.deep_agent_spec import TeamModelConfig

NS = "workspace-1"
AGENT = "mate_dev"
NOW = datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc)
ACCESS = AccessSnapshot(
    principal_ref="user-1",
    startup_scope=frozenset({"chat"}),
    source_scope=frozenset({"chat"}),
    policy_revision="policy-1",
)
SNAPSHOT = publish_spec(
    AGENT,
    "v1",
    {
        "card": {"id": AGENT, "name": "研发同事", "description": "研发"},
        "system_prompt": "你是研发同事。",
        "model": TeamModelConfig(
            model_client_config=ModelClientConfig(
                client_id=f"jiuwenmate:{AGENT}",
                client_provider="OpenAI",
                api_key="sk-test",
                api_base="https://example.invalid/v1",
                model_name="deepseek",
            ),
            model_request_config=ModelRequestConfig(model="deepseek"),
        ),
    },
)


def accept_and_start(
    repository: ExecutionRepository,
    conversation_key: str,
    source_ref: str,
    *,
    route_snapshot: RequestRouteSnapshot | None = None,
    request_kind: str = "scheduled",
):
    session = repository.resolve_session(NS, AGENT, ConversationRef.web(conversation_key), now=NOW)
    source = (
        core.DirectMessageSource(
            message_id=source_ref,
            sender_id="user-1",
            sender_name="用户",
            received_at=NOW,
        )
        if request_kind == "direct_message"
        else None
    )
    state, _ = repository.accept_request(
        session,
        snapshot=SNAPSHOT,
        source_ref=source_ref,
        access=ACCESS,
        request_input=core.RequestInput(
            kind=request_kind,
            text="整理进展",
            reply_target="reply-1",
            source=source,
        ),
        received_at=NOW,
        route_snapshot=route_snapshot,
    )
    execution = repository.start_execution(NS, state.record.request_id, now=NOW)
    return build_execution_context(state.record, execution, capability_snapshot=SNAPSHOT.enabled_features)


def progress(execution, seq, text="正在整理"):
    return core.AgentEvent.create(
        execution,
        event_type=core.EventType.PROGRESS_UPDATED,
        local_seq=seq,
        occurred_at=NOW,
        visibility="progress",
        payload={"text": text},
    )


def candidate(execution, seq, output_id="out-1", text="进展如下"):
    return core.AgentEvent.create(
        execution,
        event_type=core.EventType.OUTPUT_PROPOSED,
        local_seq=seq,
        occurred_at=NOW,
        visibility="candidate",
        payload={"output_id": output_id, "target_ref": "reply-1", "share_mode": "message", "text": text},
    )


def final(execution, seq, output_refs=("out-1",)):
    return core.AgentEvent.create(
        execution,
        event_type=core.EventType.EXECUTION_FINAL,
        local_seq=seq,
        occurred_at=NOW,
        payload={"status": "completed", "output_refs": output_refs},
    )


def paused(execution, seq):
    return core.AgentEvent.create(
        execution,
        event_type=core.EventType.EXECUTION_PAUSED,
        local_seq=seq,
        occurred_at=NOW,
        payload={"reason": "stopped"},
    )


def tool_intent(execution, seq):
    return core.AgentEvent.create(
        execution,
        event_type=core.EventType.TOOL_INTENT,
        local_seq=seq,
        occurred_at=NOW,
        payload={
            "operation_id": "op-1",
            "tool_call_id": "call-1",
            "tool_name": "write_file",
            "params_digest": "a" * 64,
            "args_summary": "写入 report.md",
        },
    )


class FlakyRepository(ExecutionRepository):
    """Fails writes of the listed event types."""

    def __init__(self, db_path, failing=()):
        super().__init__(db_path)
        self.failing = set(failing)

    def record_events(self, events, *, now):
        if any(event.event_type in self.failing for event in events):
            raise OSError("disk unavailable")
        super().record_events(events, now=now)


@pytest.fixture
async def host(tmp_path):
    repository = ExecutionRepository(tmp_path / "agents.sqlite3")
    queue = CommitQueue(repository)
    await queue.start()
    yield repository, queue, HostCommitPort(repository, queue)
    await queue.aclose()


async def test_ordinary_events_are_queued_and_read_back_in_order(host):
    repository, queue, port = host
    execution = accept_and_start(repository, "conv-1", "msg-1")

    await port.record(execution, progress(execution, 2, "第二步"))
    await port.record(execution, progress(execution, 1, "第一步"))
    await queue.drain()

    log = await port.read_execution_log(execution, execution.execution_id)
    assert [event.local_seq for event in log] == [1, 2]
    assert log[0].payload.text == "第一步"


async def test_resubmitted_event_id_is_stored_once(host):
    repository, queue, port = host
    execution = accept_and_start(repository, "conv-1", "msg-1")
    event = progress(execution, 1)

    await port.record(execution, event)
    await port.record(execution, event)
    await queue.drain()
    await port.record(execution, tool_intent(execution, 2))
    await port.record(execution, tool_intent(execution, 2))

    log = await port.read_execution_log(execution, execution.execution_id)
    assert [item.event_id for item in log] == [event.event_id, tool_intent(execution, 2).event_id]


async def test_same_event_id_with_other_content_is_rejected(host):
    repository, _, port = host
    execution = accept_and_start(repository, "conv-1", "msg-1")
    await port.record(execution, paused(execution, 1))

    other = core.AgentEvent.create(
        execution,
        event_type=core.EventType.EXECUTION_PAUSED,
        local_seq=1,
        occurred_at=NOW,
        payload={"reason": "error", "detail": "模型异常"},
    )
    with pytest.raises(PersistentAgentError) as caught:
        await port.record(execution, other)
    assert caught.value.code == "CONFLICT"


async def test_failed_ordinary_write_does_not_block_a_later_critical_event(tmp_path):
    repository = FlakyRepository(tmp_path / "agents.sqlite3", failing={core.EventType.PROGRESS_UPDATED})
    queue = CommitQueue(repository)
    await queue.start()
    port = HostCommitPort(repository, queue)
    execution = accept_and_start(repository, "conv-1", "msg-1")

    await port.record(execution, progress(execution, 1))
    await queue.drain()
    await port.record(execution, tool_intent(execution, 2))
    await queue.aclose()

    log = await port.read_execution_log(execution, execution.execution_id)
    assert [event.event_type for event in log] == [core.EventType.TOOL_INTENT]


async def test_critical_write_failure_is_raised(tmp_path):
    repository = FlakyRepository(tmp_path / "agents.sqlite3", failing={core.EventType.TOOL_INTENT})
    queue = CommitQueue(repository)
    await queue.start()
    port = HostCommitPort(repository, queue)
    execution = accept_and_start(repository, "conv-1", "msg-1")

    with pytest.raises(OSError):
        await port.record(execution, tool_intent(execution, 1))
    await queue.aclose()


async def test_final_without_saved_candidate_fails_and_is_not_stored(host):
    repository, _, port = host
    execution = accept_and_start(repository, "conv-1", "msg-1")

    with pytest.raises(PersistentAgentError) as caught:
        await port.record(execution, final(execution, 1, output_refs=("out-missing",)))

    assert caught.value.code == "OUTPUT_MISSING"
    assert await port.read_execution_log(execution, execution.execution_id) == ()


async def test_final_stores_a_candidate_whose_queued_write_failed(tmp_path):
    repository = FlakyRepository(tmp_path / "agents.sqlite3", failing={core.EventType.OUTPUT_PROPOSED})
    queue = CommitQueue(repository)
    await queue.start()
    port = HostCommitPort(repository, queue)
    execution = accept_and_start(repository, "conv-1", "msg-1")
    proposed = candidate(execution, 1)

    await port.record(execution, proposed)
    await queue.drain()
    repository.failing.clear()
    await port.record(execution, final(execution, 2))
    await queue.aclose()

    stored = repository.candidate_output(NS, "out-1")
    assert stored is not None and stored.event_id == proposed.event_id
    assert stored.payload.text == "进展如下"
    log = await port.read_execution_log(execution, execution.execution_id)
    assert [event.event_type for event in log] == [core.EventType.OUTPUT_PROPOSED, core.EventType.EXECUTION_FINAL]


async def test_final_stores_a_candidate_still_in_the_queue(tmp_path):
    repository = ExecutionRepository(tmp_path / "agents.sqlite3")
    queue = CommitQueue(repository)
    await queue.start()
    port = HostCommitPort(repository, queue)
    execution = accept_and_start(repository, "conv-1", "msg-1")

    await port.record(execution, candidate(execution, 1))
    await port.record(execution, final(execution, 2))

    assert repository.candidate_output(NS, "out-1") is not None
    await queue.aclose()


async def test_final_and_feishu_delivery_are_recorded_atomically_and_idempotently(host):
    repository, _, port = host
    route = RequestRouteSnapshot(
        channel="feishu",
        app_id="cli_bound",
        target_id="oc_group",
        source_message_id="om_source",
        source_reply_to="om_parent",
    )
    execution = accept_and_start(repository, "conv-1", "msg-1", route_snapshot=route)
    proposed, finished = candidate(execution, 1), final(execution, 2)

    await port.record(execution, proposed)
    await port.record(execution, finished)
    first = repository.result_deliveries(NS, execution.request_id)
    await port.record(execution, finished)
    again = repository.result_deliveries(NS, execution.request_id)

    assert len(first) == len(again) == 1
    assert first[0]["status"] == "pending"
    assert first[0]["channel"] == "feishu" and first[0]["target_id"] == "oc_group"
    assert first[0]["output_id"] == "out-1" and first[0]["execution_id"] == execution.execution_id
    assert first[0]["delivery_id"] == again[0]["delivery_id"]
    assert first[0]["idempotency_key"] == again[0]["idempotency_key"]


async def test_web_delivery_is_available_only_for_final_adopted_message_outputs(host):
    repository, _, port = host
    route = RequestRouteSnapshot(channel="web", target_id="conv-1", source_message_id="web-msg")
    execution = accept_and_start(repository, "conv-1", "msg-1", route_snapshot=route)

    await port.record(execution, candidate(execution, 1, output_id="unadopted"))
    await port.record(execution, final(execution, 2, output_refs=()))

    assert repository.result_deliveries(NS, execution.request_id) == []


async def test_claimed_feishu_delivery_carries_the_accepted_route_and_can_be_acknowledged(host):
    repository, _, port = host
    route = RequestRouteSnapshot(
        channel="feishu",
        app_id="cli_bound",
        target_id="oc_group",
        source_message_id="om_source",
        source_reply_to="om_parent",
    )
    execution = accept_and_start(repository, "conv-1", "msg-1", route_snapshot=route)
    await port.record(execution, candidate(execution, 1))
    await port.record(execution, final(execution, 2))

    jobs = repository.claim_pending_result_deliveries(NS, limit=10, now=NOW)

    assert len(jobs) == 1
    assert jobs[0]["app_id"] == "cli_bound" and jobs[0]["target_id"] == "oc_group"
    assert jobs[0]["source_message_id"] == "om_source" and jobs[0]["source_reply_to"] == "om_parent"
    assert jobs[0]["is_direct_message"] is False
    assert jobs[0]["text"] == "进展如下" and jobs[0]["attempt_count"] == 1
    assert "mention_ids" not in jobs[0]
    delivery_id = jobs[0]["delivery_id"]
    repository.update_result_delivery(
        NS,
        delivery_id,
        outcome="delivered",
        external_message_id="om_reply",
        error_code=None,
        now=NOW,
    )
    repository.update_result_delivery(
        NS,
        delivery_id,
        outcome="delivered",
        external_message_id="om_reply",
        error_code=None,
        now=NOW,
    )

    row = repository.result_deliveries(NS, execution.request_id)[0]
    assert row["status"] == "delivered" and row["external_message_id"] == "om_reply"


async def test_claimed_feishu_direct_message_delivery_is_not_a_reply(host):
    repository, _, port = host
    route = RequestRouteSnapshot(
        channel="feishu", app_id="cli_bound", target_id="oc_dm", source_message_id="om_source"
    )
    execution = accept_and_start(
        repository, "conv-dm", "msg-dm", route_snapshot=route, request_kind="direct_message"
    )
    await port.record(execution, candidate(execution, 1))
    await port.record(execution, final(execution, 2))

    job = repository.claim_pending_result_deliveries(NS, now=NOW)[0]

    assert job["is_direct_message"] is True
    assert job["source_message_id"] is None


async def test_claimed_group_delivery_does_not_copy_inbound_mentions(host):
    repository, _, port = host
    route = RequestRouteSnapshot(channel="feishu", app_id="cli_a", target_id="oc_group", source_message_id="om_1")
    session = repository.resolve_session(NS, AGENT, ConversationRef.group("group-1"), now=NOW)
    state, _ = repository.accept_request(
        session,
        snapshot=SNAPSHOT,
        source_ref="feishu:message:om_1",
        access=ACCESS,
        request_input=core.RequestInput(
            kind="group_message",
            text="请交给同事",
            reply_target="oc_group",
            source=core.GroupMessage(
                message_id="om_1",
                sender_id="ou_author",
                sender_name="发起人",
                sender_kind="human",
                received_at=NOW,
                text="请交给同事",
                mention_ids=("ou_author", "ou_b", "ou_b", "ou.b"),
            ),
        ),
        received_at=NOW,
        route_snapshot=route,
    )
    record = repository.start_execution(NS, state.record.request_id, now=NOW)
    execution = build_execution_context(state.record, record, capability_snapshot=SNAPSHOT.enabled_features)
    await port.record(execution, candidate(execution, 1))
    await port.record(execution, final(execution, 2))

    job = repository.claim_pending_result_deliveries(NS, limit=10, now=NOW)[0]

    assert "mention_ids" not in job
    assert state.record.request_input.source.mention_ids == ("ou_author", "ou_b", "ou_b", "ou.b")


async def test_sending_delivery_after_restart_becomes_unknown_not_retriable(host):
    repository, _, port = host
    route = RequestRouteSnapshot(channel="feishu", app_id="cli_bound", target_id="oc_group", source_message_id="om_1")
    execution = accept_and_start(repository, "conv-1", "msg-1", route_snapshot=route)
    await port.record(execution, candidate(execution, 1))
    await port.record(execution, final(execution, 2))
    repository.claim_pending_result_deliveries(NS, limit=10, now=NOW)

    repository.recover_sending_result_deliveries(NS, now=NOW + timedelta(minutes=1))
    jobs = repository.claim_pending_result_deliveries(NS, limit=10, now=NOW + timedelta(minutes=1))

    row = repository.result_deliveries(NS, execution.request_id)[0]
    assert row["status"] == "unknown" and jobs == []


async def test_unknown_delivery_cannot_be_reset_to_pending(host):
    repository, _, port = host
    route = RequestRouteSnapshot(channel="feishu", app_id="cli_bound", target_id="oc_group", source_message_id="om_1")
    execution = accept_and_start(repository, "conv-1", "msg-1", route_snapshot=route)
    await port.record(execution, candidate(execution, 1))
    await port.record(execution, final(execution, 2))
    job = repository.claim_pending_result_deliveries(NS, limit=10, now=NOW)[0]
    repository.update_result_delivery(
        NS,
        job["delivery_id"],
        outcome="unknown",
        external_message_id=None,
        error_code="ACK_TIMEOUT",
        now=NOW,
    )

    with pytest.raises(PersistentAgentError) as caught:
        repository.update_result_delivery(
            NS,
            job["delivery_id"],
            outcome="pending",
            external_message_id=None,
            error_code="RETRY",
            now=NOW,
        )

    assert caught.value.code == "CONFLICT"
    assert repository.result_deliveries(NS, execution.request_id)[0]["status"] == "unknown"


async def test_delivery_service_persists_the_gateway_receipt(host):
    from jiuwenswarm.server.jiuwenmate.delivery_service import ResultDeliveryService

    repository, _, port = host
    route = RequestRouteSnapshot(channel="feishu", app_id="cli_bound", target_id="oc_group", source_message_id="om_1")
    execution = accept_and_start(repository, "conv-1", "msg-1", route_snapshot=route)
    await port.record(execution, candidate(execution, 1))
    await port.record(execution, final(execution, 2))
    sent = []

    async def send(job):
        sent.append(job)
        return {
            "delivery_id": job["delivery_id"],
            "stage": "send",
            "outcome": "delivered",
            "external_message_id": "om_reply",
        }

    service = ResultDeliveryService(repository, namespace_id=NS, send_delivery=send, clock=lambda: NOW)

    assert await service.dispatch_pending() == 1

    row = repository.result_deliveries(NS, execution.request_id)[0]
    assert sent[0]["app_id"] == "cli_bound" and sent[0]["text"] == "进展如下"
    assert row["status"] == "delivered" and row["external_message_id"] == "om_reply"


async def test_delivery_service_attaches_only_current_group_handoff_targets(host):
    from jiuwenswarm.server.jiuwenmate.group_chat_repository import GroupChatRepository
    from jiuwenswarm.server.jiuwenmate.delivery_service import ResultDeliveryService

    repository, _, port = host
    groups = GroupChatRepository(repository.db_path)
    group = groups.resolve(NS, "feishu", "oc_group", now=NOW)
    groups.observe_access(NS, "agent-peer", group.group_chat_id, "app-peer", observed_at=NOW, source_ref="joined:peer")
    session = repository.resolve_session(NS, AGENT, ConversationRef.group(group.group_chat_id), now=NOW)
    route = RequestRouteSnapshot(
        channel="feishu", app_id="app-sender", target_id="oc_group", source_message_id="om_source"
    )
    state, _ = repository.accept_request(
        session,
        snapshot=SNAPSHOT,
        source_ref="feishu:om_source",
        access=ACCESS,
        request_input=core.RequestInput(
            kind="group_message",
            text="请交接",
            reply_target="oc_group",
            source=core.GroupMessage(
                message_id="om_source", sender_id="human", sender_name="用户", sender_kind="human",
                received_at=NOW, text="请交接",
            ),
        ),
        received_at=NOW,
        route_snapshot=route,
    )
    execution = repository.start_execution(NS, state.record.request_id, now=NOW)
    context = build_execution_context(state.record, execution, capability_snapshot=SNAPSHOT.enabled_features)
    await port.record(context, candidate(context, 1, text="请继续 [[agent:agent-peer]]"))
    await port.record(context, final(context, 2))
    sent = []

    async def send(job):
        sent.append(job)
        return {"delivery_id": job["delivery_id"], "stage": "send", "outcome": "delivered"}

    service = ResultDeliveryService(
        repository,
        namespace_id=NS,
        send_delivery=send,
        clock=lambda: NOW,
        mention_roster=lambda job: [
            {"agent_id": AGENT, "name": "发送同事"},
            {"agent_id": "agent-peer", "name": "执行同事"},
        ],
    )

    assert await service.dispatch_pending() == 1
    assert sent[0]["text"] == "请继续 [[agent:agent-peer]]"
    assert sent[0]["mention_targets"] == [{"agent_id": "agent-peer", "name": "执行同事"}]
    assert not {"namespace_id", "agent_id", "session_id", "request_kind"}.intersection(sent[0])


async def test_delivery_service_strips_handoff_tokens_from_private_messages(host):
    from jiuwenswarm.server.jiuwenmate.delivery_service import ResultDeliveryService

    repository, _, port = host
    route = RequestRouteSnapshot(
        channel="feishu", app_id="cli_bound", target_id="oc_dm", source_message_id="om_source"
    )
    execution = accept_and_start(repository, "conv-1", "msg-1", route_snapshot=route, request_kind="direct_message")
    await port.record(execution, candidate(execution, 1, text="不应接力 [[agent:agent-peer]]"))
    await port.record(execution, final(execution, 2))
    sent = []

    async def send(job):
        sent.append(job)
        return {"delivery_id": job["delivery_id"], "stage": "send", "outcome": "delivered"}

    service = ResultDeliveryService(
        repository,
        namespace_id=NS,
        send_delivery=send,
        clock=lambda: NOW,
        mention_roster=lambda job: [{"agent_id": "agent-peer", "name": "执行同事"}],
    )

    assert await service.dispatch_pending() == 1
    assert sent[0]["text"] == "不应接力 "
    assert "mention_targets" not in sent[0]


async def test_group_confirmed_reply_archive_retries_without_resending(host, tmp_path):
    from jiuwenswarm.server.jiuwenmate.conversation_archive import ConversationArchive
    from jiuwenswarm.server.jiuwenmate.conversation_resources import ConversationResources
    from jiuwenswarm.server.jiuwenmate.group_chat_repository import GroupChatRepository
    from jiuwenswarm.server.jiuwenmate.delivery_service import ResultDeliveryService

    repository, _, port = host
    groups = GroupChatRepository(repository.db_path)
    group = groups.resolve(NS, "feishu", "oc_group", now=NOW)
    session = repository.resolve_session(NS, AGENT, ConversationRef.group(group.group_chat_id), now=NOW)
    route = RequestRouteSnapshot(channel="feishu", app_id="cli_bound", target_id="oc_group", source_message_id="om_1")
    state, _ = repository.accept_request(session, snapshot=SNAPSHOT, source_ref="feishu:message:om_1", access=ACCESS,
        request_input=core.RequestInput(kind="group_message", text="hello", reply_target="oc_group",
            source=core.GroupMessage(message_id="om_1", sender_id="alice", sender_name="Alice", sender_kind="human",
                                     received_at=NOW, text="hello")), received_at=NOW, route_snapshot=route)
    record = repository.start_execution(NS, state.record.request_id, now=NOW)
    execution = build_execution_context(state.record, record, capability_snapshot=SNAPSHOT.enabled_features)
    await port.record(execution, candidate(execution, 1))
    await port.record(execution, final(execution, 2))
    archive = ConversationArchive(tmp_path / "group_messages", groups)
    resources = ConversationResources(archive, repository)
    sends = []
    attempts = []

    async def send(job):
        sends.append(job)
        return {"delivery_id": job["delivery_id"], "stage": "send", "outcome": "delivered", "external_message_id": "sent-1"}

    async def save(job):
        attempts.append(job)
        assert repository.result_deliveries(NS, state.record.request_id)[0]["status"] == "delivered"
        if len(attempts) == 1:
            raise OSError("archive temporarily offline")
        await asyncio.to_thread(resources.archive_delivery, job)

    service = ResultDeliveryService(repository, namespace_id=NS, send_delivery=send, clock=lambda: NOW,
                                    archive_delivery=save)
    await service.dispatch_pending()
    await service.reconcile_archives()
    assert len(sends) == 1
    page = archive.recent(NS, group.group_chat_id, cursor=None, limit=20, visible_from=NOW)
    assert [(item.message_id, item.text) for item in page.messages] == [("sent-1", "进展如下")]
    assert repository.pending_delivery_archives(NS) == []


async def test_unsent_batch_deliveries_remain_pending_when_worker_is_cancelled(host):
    from jiuwenswarm.server.jiuwenmate.delivery_service import ResultDeliveryService

    repository, _, port = host
    route = RequestRouteSnapshot(channel="feishu", app_id="cli_bound", target_id="oc_group", source_message_id="om_1")
    execution = accept_and_start(repository, "conv-1", "msg-1", route_snapshot=route)
    await port.record(execution, candidate(execution, 1, output_id="out-1"))
    await port.record(execution, candidate(execution, 2, output_id="out-2", text="第二条"))
    await port.record(execution, final(execution, 3, output_refs=("out-1", "out-2")))
    started = asyncio.Event()
    sent_ids = []

    async def send(job):
        sent_ids.append(job["delivery_id"])
        if len(sent_ids) == 1:
            started.set()
            await asyncio.Event().wait()
        return {
            "delivery_id": job["delivery_id"],
            "stage": "send",
            "outcome": "delivered",
            "external_message_id": f"om_{job['output_id']}",
        }

    service = ResultDeliveryService(repository, namespace_id=NS, send_delivery=send, clock=lambda: NOW)
    dispatch = asyncio.create_task(service.dispatch_pending())
    await started.wait()
    dispatch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await dispatch

    repository.recover_sending_result_deliveries(NS, now=NOW + timedelta(minutes=1))
    rows = repository.result_deliveries(NS, execution.request_id)
    assert [row["status"] for row in rows] == ["unknown", "pending"]
    assert await service.dispatch_pending() == 1
    assert len(sent_ids) == 2
    assert [row["status"] for row in repository.result_deliveries(NS, execution.request_id)] == [
        "unknown",
        "delivered",
    ]


async def test_pending_gateway_ack_is_persisted_and_idempotent(host):
    from jiuwenswarm.server.jiuwenmate.delivery_service import ResultDeliveryService

    repository, _, port = host
    route = RequestRouteSnapshot(channel="feishu", app_id="cli_bound", target_id="oc_group", source_message_id="om_1")
    execution = accept_and_start(repository, "conv-1", "msg-1", route_snapshot=route)
    await port.record(execution, candidate(execution, 1, output_id="out-1"))
    await port.record(execution, candidate(execution, 2, output_id="out-2", text="第二条"))
    await port.record(execution, final(execution, 3, output_refs=("out-1", "out-2")))
    calls = 0
    service = None

    async def send(job):
        nonlocal calls
        calls += 1
        if job["output_id"] == "out-2":
            return {
                "delivery_id": job["delivery_id"],
                "stage": "send",
                "outcome": "delivered",
                "external_message_id": "om_reply",
            }
        ack = {
            "delivery_id": job["delivery_id"],
            "stage": "send",
            "outcome": "pending",
            "error_code": "FEISHU_APP_NOT_CONNECTED",
        }
        await service.apply_ack(ack)
        return ack

    service = ResultDeliveryService(repository, namespace_id=NS, send_delivery=send, clock=lambda: NOW)

    assert await service.dispatch_pending() == 2
    rows = repository.result_deliveries(NS, execution.request_id)
    assert [row["status"] for row in rows] == ["pending", "delivered"]
    assert rows[0]["error_code"] == "FEISHU_APP_NOT_CONNECTED"
    assert all(row["attempt_count"] == 1 for row in rows)
    assert calls == 2


async def test_final_commit_wakes_delivery_worker_after_ledger_is_durable(tmp_path):
    from jiuwenswarm.server.jiuwenmate.delivery_service import ResultDeliveryService

    repository = ExecutionRepository(tmp_path / "agents.sqlite3")
    queue = CommitQueue(repository)
    sent = []
    route = RequestRouteSnapshot(
        channel="feishu", app_id="cli_bound", target_id="oc_group", source_message_id="om_source"
    )
    execution = accept_and_start(repository, "conv-1", "msg-1", route_snapshot=route)

    async def send(job):
        sent.append(job)
        assert repository.result_deliveries(NS, execution.request_id)[0]["status"] == "sending"
        return {
            "delivery_id": job["delivery_id"],
            "stage": "send",
            "outcome": "delivered",
            "external_message_id": "om_reply",
        }

    service = ResultDeliveryService(repository, namespace_id=NS, send_delivery=send)
    port = HostCommitPort(repository, queue, observer=service.observe)
    await queue.start()
    await service.start()
    try:
        await port.record(execution, candidate(execution, 1))
        await port.record(execution, final(execution, 2))
        for _ in range(100):
            rows = repository.result_deliveries(NS, execution.request_id)
            if rows and rows[0]["status"] == "delivered":
                break
            await asyncio.sleep(0.01)
        assert len(sent) == 1
        assert repository.result_deliveries(NS, execution.request_id)[0]["external_message_id"] == "om_reply"
    finally:
        await service.aclose()
        await queue.aclose()


async def test_delivery_service_does_not_blindly_retry_unknown_platform_results(host):
    from jiuwenswarm.server.jiuwenmate.delivery_service import ResultDeliveryService

    repository, _, port = host
    route = RequestRouteSnapshot(channel="feishu", app_id="cli_bound", target_id="oc_group", source_message_id="om_1")
    execution = accept_and_start(repository, "conv-1", "msg-1", route_snapshot=route)
    await port.record(execution, candidate(execution, 1))
    await port.record(execution, final(execution, 2))
    calls = 0

    async def send(job):
        nonlocal calls
        calls += 1
        return {"delivery_id": job["delivery_id"], "stage": "send", "outcome": "unknown"}

    service = ResultDeliveryService(repository, namespace_id=NS, send_delivery=send, clock=lambda: NOW)

    assert await service.dispatch_pending() == 1
    assert await service.dispatch_pending() == 0
    assert calls == 1
    assert repository.result_deliveries(NS, execution.request_id)[0]["status"] == "unknown"


async def test_resubmitted_candidate_and_final_keep_their_ids(host):
    repository, queue, port = host
    execution = accept_and_start(repository, "conv-1", "msg-1")
    proposed, finished = candidate(execution, 1), final(execution, 2)

    for _ in range(2):
        await port.record(execution, proposed)
        await port.record(execution, finished)
    await queue.drain()

    log = await port.read_execution_log(execution, execution.execution_id)
    assert [event.event_id for event in log] == [proposed.event_id, finished.event_id]
    assert repository.candidate_output(NS, "out-1").event_id == proposed.event_id


async def test_output_id_cannot_be_reused_by_another_event(host):
    repository, _, port = host
    execution = accept_and_start(repository, "conv-1", "msg-1")
    await port.record(execution, candidate(execution, 1))
    await port.record(execution, final(execution, 2))

    with pytest.raises(PersistentAgentError) as caught:
        repository.record_events([candidate(execution, 3, text="另一份内容")], now=NOW)
    assert caught.value.code == "CONFLICT"


async def test_execution_has_at_most_one_terminal_event(host):
    repository, _, port = host
    execution = accept_and_start(repository, "conv-1", "msg-1")
    await port.record(execution, paused(execution, 1))

    with pytest.raises(PersistentAgentError) as caught:
        await port.record(execution, final(execution, 2, output_refs=()))
    assert caught.value.code == "CONFLICT"


async def test_event_of_another_execution_is_rejected(host):
    repository, _, port = host
    mine = accept_and_start(repository, "conv-1", "msg-1")
    other = accept_and_start(repository, "conv-2", "msg-2")

    with pytest.raises(BaseError):
        await port.record(mine, tool_intent(other, 1))


async def test_reading_a_log_of_another_request_is_rejected(host):
    repository, _, port = host
    mine = accept_and_start(repository, "conv-1", "msg-1")
    other = accept_and_start(repository, "conv-2", "msg-2")
    await port.record(other, tool_intent(other, 1))

    for execution_id in (other.execution_id, "exe_unknown"):
        with pytest.raises(PersistentAgentError) as caught:
            await port.read_execution_log(mine, execution_id)
        assert caught.value.code == "NOT_FOUND"


async def test_log_survives_restart(tmp_path):
    repository = ExecutionRepository(tmp_path / "agents.sqlite3")
    queue = CommitQueue(repository)
    await queue.start()
    port = HostCommitPort(repository, queue)
    execution = accept_and_start(repository, "conv-1", "msg-1")
    await port.record(execution, progress(execution, 1))
    await port.record(execution, candidate(execution, 2))
    await port.record(execution, final(execution, 3))
    await queue.aclose()

    restarted = ExecutionRepository(tmp_path / "agents.sqlite3")
    log = restarted.read_execution_log(NS, execution.session_id, execution.request_id, execution.execution_id)
    assert [event.local_seq for event in log] == [1, 2, 3]
    assert log[2].payload.output_refs == ("out-1",)


async def test_closed_queue_rejects_ordinary_events_but_writes_what_was_queued(tmp_path):
    repository = ExecutionRepository(tmp_path / "agents.sqlite3")
    queue = CommitQueue(repository)
    await queue.start()
    port = HostCommitPort(repository, queue)
    execution = accept_and_start(repository, "conv-1", "msg-1")
    await port.record(execution, progress(execution, 1))

    await queue.aclose()

    assert not queue.running
    with pytest.raises(PersistentAgentError) as caught:
        await port.record(execution, progress(execution, 2))
    assert caught.value.code == "QUEUE_CLOSED"
    log = await port.read_execution_log(execution, execution.execution_id)
    assert [event.local_seq for event in log] == [1]


async def test_group_failed_archive_batch_does_not_starve_later_groups():
    from jiuwenswarm.server.jiuwenmate.delivery_service import ResultDeliveryService

    jobs = [{"updated_at": NOW.isoformat(), "delivery_id": f"d{index:03}"} for index in range(101)]
    saved = set()

    def pending(namespace_id, *, limit=100, after=None):
        return [job for job in jobs if job["delivery_id"] not in saved
                and (after is None or (job["updated_at"], job["delivery_id"]) > after)][:limit]

    async def archive(job):
        if job["delivery_id"] != "d100":
            raise OSError("one group is unavailable")

    sender = AsyncMock()
    service = ResultDeliveryService(SimpleNamespace(pending_delivery_archives=pending,
        mark_delivery_archived=lambda namespace_id, delivery_id: saved.add(delivery_id)),
        namespace_id=NS, send_delivery=sender, archive_delivery=archive)
    await service.reconcile_archives()
    await service.reconcile_archives()
    assert saved == {"d100"}
    sender.assert_not_awaited()


class FakeBudget:
    async def admit(self, execution, model_call_id):
        raise AssertionError("not called")

    async def report(self, execution, model_call_id, usage):
        raise AssertionError("not called")


async def test_port_satisfies_the_core_contract(host):
    _, _, port = host

    core.PersistentPorts(commit=port, budget=FakeBudget(), window_backend=InMemoryKVStore()).validate()


async def test_request_record_without_route_snapshot_remains_readable(host):
    repository, _, _ = host
    execution = accept_and_start(repository, "conv-1", "msg-1")
    stored = repository.get_request(NS, execution.request_id).record.to_dict()
    stored.pop("route_snapshot", None)

    restored = RequestRecord.from_dict(stored)

    assert restored.route_snapshot is None


async def test_observer_sees_recorded_events_and_cannot_break_recording(tmp_path):
    repository = ExecutionRepository(tmp_path / "agents.sqlite3")
    queue = CommitQueue(repository)
    await queue.start()
    seen = []

    def observer(event):
        seen.append(event.local_seq)
        raise RuntimeError("display failed")

    port = HostCommitPort(repository, queue, observer=observer)
    execution = accept_and_start(repository, "conv-1", "msg-1")

    await port.record(execution, progress(execution, 1))
    await port.record(execution, candidate(execution, 2))
    with pytest.raises(PersistentAgentError):
        await port.record(execution, final(execution, 3, output_refs=("out-missing",)))
    await port.record(execution, final(execution, 4))
    await queue.aclose()

    assert seen == [1, 2, 4]
    log = await port.read_execution_log(execution, execution.execution_id)
    assert [event.local_seq for event in log] == [1, 2, 4]
