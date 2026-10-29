"""Persist the guarded delivery body without publishing the audited candidate."""

import asyncio
import json
import sqlite3
import threading
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.server.jiuwenmate.conversation_archive import ConversationArchive
from jiuwenswarm.server.jiuwenmate.conversation_resources import ConversationResources
from jiuwenswarm.server.jiuwenmate.delivery_service import ResultDeliveryService
from jiuwenswarm.server.jiuwenmate.execution_models import (
    ConversationRef,
    RequestRouteSnapshot,
    build_execution_context,
)
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from jiuwenswarm.server.jiuwenmate.group_chat_repository import GroupChatRepository
from openjiuwen.harness import persistent_agent as core
from tests.unit_tests.server.jiuwenmate.test_commit_port import ACCESS, AGENT, NOW, NS, SNAPSHOT, final


NOTICE = "跨群分享授权已失效，本次内容未发送。"
PRIVATE = "PRIVATE-CROSS-GROUP-CANDIDATE-7f9c"


def prepare(tmp_path, *, attachment, channel="feishu", text=PRIVATE):
    repository = ExecutionRepository(tmp_path / "agents.sqlite3")
    groups = GroupChatRepository(repository.db_path)
    group = groups.resolve(NS, channel, "target-group", now=NOW)
    session = repository.resolve_session(NS, AGENT, ConversationRef.group(group.group_chat_id), now=NOW)
    state, _ = repository.accept_request(
        session,
        snapshot=SNAPSHOT,
        source_ref="feishu:message:source-1",
        access=ACCESS,
        request_input=core.RequestInput(
            kind="group_message", text="summarize", reply_target="target-group",
            source=core.GroupMessage(
                message_id="source-1", sender_id="user-1", sender_name="User", sender_kind="human",
                received_at=NOW, text="summarize",
            ),
        ),
        received_at=NOW,
        route_snapshot=RequestRouteSnapshot(
            channel=channel, app_id="app-1", target_id="target-group", source_message_id="source-1",
        ),
    )
    record = repository.start_execution(NS, state.record.request_id, now=NOW)
    execution = build_execution_context(state.record, record, capability_snapshot=SNAPSHOT.enabled_features)
    payload = {"output_id": "out-1", "target_ref": "target-group", "share_mode": "message", "text": text}
    if attachment:
        payload.update(
            share_mode="attachment", file_path="output/private.txt", file_ref="sha256:" + "a" * 64, file_size=7,
        )
    proposed = core.AgentEvent.create(
        execution, event_type=core.EventType.OUTPUT_PROPOSED, local_seq=1,
        occurred_at=NOW, visibility="candidate", payload=payload,
    )
    repository.record_events([proposed, final(execution, 2)], now=NOW)
    if attachment:
        # An earlier definite retry may already have uploaded the original file.
        job, = repository.claim_pending_result_deliveries(NS, channels=frozenset({channel}), now=NOW)
        repository.update_result_delivery(
            NS, job["delivery_id"], outcome="pending", external_message_id=None,
            error_code="RETRY", file_key="private-upload-key", now=NOW,
        )
    archive = ConversationArchive(tmp_path / "group_messages", groups)
    return repository, execution, archive, group


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["feishu", "dingtalk"])
@pytest.mark.parametrize("attachment", [False, True])
@pytest.mark.parametrize("transition", ["delivered", "archive_retry", "unknown", "pending", "crash"])
async def test_guarded_delivery_only_archives_persisted_notice_after_restart(tmp_path, attachment, transition, channel):
    repository, execution, archive, group = prepare(tmp_path, attachment=attachment, channel=channel)
    sends, archive_jobs = [], []
    resources = ConversationResources(archive, repository)

    async def save(job):
        archive_jobs.append(dict(job))
        if transition == "archive_retry" and len(archive_jobs) == 1:
            raise OSError("archive unavailable")
        await asyncio.to_thread(resources.archive_delivery, job)

    async def send(job):
        assert stored_delivery(repository)["notice_text"] == NOTICE
        assert stored_delivery(repository)["file_key"] is None
        sends.append(dict(job))
        if transition == "crash":
            raise asyncio.CancelledError()
        return {
            "delivery_id": job["delivery_id"], "stage": "send",
            "outcome": "delivered" if transition == "archive_retry" else transition,
            "external_message_id": "sent-notice" if transition in {"delivered", "archive_retry"} else None,
            "file_key": "private-upload-key",
        }

    service = ResultDeliveryService(
        repository, namespace_id=NS, send_delivery=send, archive_delivery=save, clock=lambda: NOW,
        channels=frozenset({channel}),
    )
    service.set_sharing_guard(AsyncMock(return_value=False))
    if transition == "crash":
        with pytest.raises(asyncio.CancelledError):
            await service.dispatch_pending()
    else:
        assert await service.dispatch_pending() == 1
    assert sends[0]["text"] == NOTICE and "file" not in sends[0]

    reopened = ExecutionRepository(repository.db_path)
    resources = ConversationResources(archive, reopened)

    async def retry_send(job):
        sends.append(dict(job))
        return {"delivery_id": job["delivery_id"], "stage": "send", "outcome": "delivered",
                "external_message_id": "sent-notice", "file_key": "private-upload-key"}

    # The new instance has no guard; the persisted substitution must remain authoritative.
    restarted = ResultDeliveryService(
        reopened, namespace_id=NS, send_delivery=retry_send, archive_delivery=save, clock=lambda: NOW,
        channels=frozenset({channel}),
    )
    row, = reopened.result_deliveries(NS, execution.request_id)
    if transition == "crash":
        assert reopened.recover_sending_result_deliveries(NS, channels=frozenset({channel}), now=NOW) == 1
    if transition in {"unknown", "crash"}:
        assert await restarted.dispatch_pending() == 0
        await restarted.apply_ack({
            "delivery_id": row["delivery_id"], "stage": "send", "outcome": "delivered",
            "external_message_id": "sent-notice", "file_key": "private-upload-key",
        })
    elif transition == "pending":
        assert await restarted.dispatch_pending() == 1
    else:
        await restarted.reconcile_archives()
    await restarted.reconcile_archives()

    page = archive.recent(NS, group.group_chat_id, cursor=None, limit=20, visible_from=NOW)
    assert [(item.message_id, item.text) for item in page.messages] == [("sent-notice", NOTICE)]
    assert all(job["text"] == NOTICE and "file" not in job for job in sends)
    assert all(json.loads(job["payload_json"]) == {"share_mode": "message", "text": NOTICE} for job in archive_jobs)
    assert all(job["file_key"] is None for job in archive_jobs)
    assert reopened.pending_delivery_archives(NS) == []
    assert reopened.result_deliveries(NS, execution.request_id)[0]["notice_text"] == NOTICE
    assert reopened.candidate_output(NS, "out-1") == repository.candidate_output(NS, "out-1")
    assert reopened.candidate_output(NS, "out-1").payload.text == PRIVATE
    assert len(sends) == (2 if transition == "pending" else 1)


def stored_delivery(repository):
    with sqlite3.connect(repository.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT * FROM result_deliveries").fetchone())


def seed_welink_snapshot(repository, *, outcome="pending"):
    job, = repository.claim_pending_result_deliveries(NS, channels=frozenset({"welink"}), now=NOW)
    job["text"] = PRIVATE
    job["mention_targets"] = [{"agent_id": "peer", "name": "Peer"}]
    job.update(file_path="output/private.txt", file_key="private-upload-key",
               file_ref="sha256:" + "a" * 64, file_size=7)
    repository.save_result_delivery_job(NS, job)
    repository.update_result_delivery(
        NS, job["delivery_id"], outcome=outcome,
        external_message_id="original-receipt" if outcome == "delivered" else None,
        error_code=None, file_key="private-upload-key", now=NOW,
    )
    return job


def assert_safe(job):
    assert job.get("text") == NOTICE
    assert not job.get("mention_targets")
    assert not {"file", "file_path", "file_key", "file_ref", "file_size"}.intersection(job)
    assert PRIVATE not in json.dumps(job)


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["delivered", "pending", "unknown", "crash"])
@pytest.mark.parametrize("guard_raises", [False, True])
async def test_welink_stale_snapshot_cannot_restore_redacted_payload(tmp_path, transition, guard_raises):
    repository, execution, _, _ = prepare(tmp_path, attachment=True, channel="welink")
    stale = seed_welink_snapshot(repository)
    sends, handoffs = [], []

    async def guard(job):
        assert job["request_id"] == execution.request_id
        if guard_raises:
            raise OSError("membership unavailable")
        return False

    async def send(job):
        assert_safe(job)
        row = stored_delivery(repository)
        assert row["notice_text"] == NOTICE and row["file_key"] is None
        assert row["handoff_done"] == 1
        assert_safe(json.loads(row["job_json"]))
        sends.append(dict(job))
        if transition == "crash" and len(sends) == 1:
            raise asyncio.CancelledError()
        outcome = transition if len(sends) == 1 else "delivered"
        return {"delivery_id": job["delivery_id"], "stage": "send", "outcome": outcome,
                "external_message_id": "notice-receipt" if outcome == "delivered" else None,
                "file_key": "private-upload-key"}

    async def handoff(*args):
        handoffs.append(args)

    service = ResultDeliveryService(repository, namespace_id=NS, channels=frozenset({"welink"}),
                                    send_delivery=send, handoff_handler=handoff, clock=lambda: NOW)
    service.set_sharing_guard(guard)
    if transition == "crash":
        with pytest.raises(asyncio.CancelledError):
            await service.dispatch_pending()
    else:
        assert await service.dispatch_pending() == 1
    reopened = ExecutionRepository(repository.db_path)
    restarted = ResultDeliveryService(reopened, namespace_id=NS, channels=frozenset({"welink"}),
                                      send_delivery=send, handoff_handler=handoff, clock=lambda: NOW)
    if transition == "crash":
        assert reopened.recover_sending_result_deliveries(NS, channels=frozenset({"welink"}), now=NOW) == 1
    if transition == "pending":
        assert await restarted.dispatch_pending() == 1
    else:
        assert await restarted.dispatch_pending() == 0
    if transition in {"unknown", "crash"}:
        reconcile, = reopened.unknown_welink_result_deliveries(NS)
        assert_safe(reconcile)
        assert reconcile["operation"] == "reconcile"
        await restarted.apply_ack({"delivery_id": stale["delivery_id"], "stage": "send",
                                   "outcome": "delivered", "external_message_id": "notice-receipt",
                                   "file_key": "private-upload-key"})
    await restarted.reconcile_welink_deliveries()
    assert not handoffs
    assert len(sends) == (2 if transition == "pending" else 1)
    row = stored_delivery(reopened)
    assert row["file_key"] is None and row["handoff_done"] == 1
    assert_safe(json.loads(row["job_json"]))
    assert reopened.candidate_output(NS, "out-1").payload.text == PRIVATE


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["unknown", "delivered"])
@pytest.mark.parametrize("guard_raises", [False, True])
async def test_revoked_welink_snapshot_cannot_reach_delayed_handoff(tmp_path, outcome, guard_raises):
    repository, _, _, _ = prepare(tmp_path, attachment=True, channel="welink")
    stale = seed_welink_snapshot(repository, outcome=outcome)
    reopened = ExecutionRepository(repository.db_path)
    operations, handoffs = [], []

    async def guard(job):
        if guard_raises:
            raise OSError("grant unavailable")
        return False

    async def send(job):
        assert job["operation"] == "reconcile"
        assert_safe(job)
        operations.append(job["operation"])
        return {"delivery_id": job["delivery_id"], "stage": "send", "outcome": "delivered",
                "external_message_id": "original-receipt"}

    async def handoff(*args):
        handoffs.append(args)

    service = ResultDeliveryService(reopened, namespace_id=NS, channels=frozenset({"welink"}),
                                    send_delivery=send, handoff_handler=handoff, clock=lambda: NOW)
    service.set_sharing_guard(guard)
    assert await service.dispatch_pending() == 0
    await service.reconcile_welink_deliveries()
    assert operations == (["reconcile"] if outcome == "unknown" else [])
    assert not handoffs
    row = stored_delivery(reopened)
    assert row["status"] == "delivered" and row["external_message_id"] == "original-receipt"
    assert row["notice_text"] == NOTICE and row["file_key"] is None and row["handoff_done"] == 1
    assert reopened.pending_welink_handoffs(NS) == []
    assert reopened.unknown_welink_result_deliveries(NS) == []
    assert_safe(json.loads(row["job_json"]))
    assert row["idempotency_key"] == stale["idempotency_key"]
    assert reopened.candidate_output(NS, "out-1").payload.text == PRIVATE


@pytest.mark.asyncio
async def test_revocation_before_public_archive_retry_redacts_candidate(tmp_path):
    repository, _, archive, group = prepare(tmp_path, attachment=True)
    job, = repository.claim_pending_result_deliveries(NS, now=NOW)
    repository.update_result_delivery(NS, job["delivery_id"], outcome="delivered",
                                      external_message_id="original-receipt", error_code=None, now=NOW)
    resources = ConversationResources(archive, repository)

    async def save(job):
        await asyncio.to_thread(resources.archive_delivery, job)

    service = ResultDeliveryService(repository, namespace_id=NS, send_delivery=AsyncMock(),
                                    archive_delivery=save, clock=lambda: NOW)
    service.set_sharing_guard(AsyncMock(return_value=False))
    await service.reconcile_archives()
    page = archive.recent(NS, group.group_chat_id, cursor=None, limit=20, visible_from=NOW)
    assert [(item.message_id, item.text) for item in page.messages] == [("original-receipt", NOTICE)]
    assert stored_delivery(repository)["status"] == "delivered"
    assert repository.candidate_output(NS, "out-1").payload.text == PRIVATE


@pytest.mark.parametrize("outcome", ["pending", "unknown", "delivered"])
def test_repository_notice_fences_stale_snapshot_writes_and_late_upload_keys(tmp_path, outcome):
    repository, _, _, _ = prepare(tmp_path, attachment=True, channel="welink")
    stale = seed_welink_snapshot(repository, outcome=outcome)
    repository.replace_result_delivery_with_notice(NS, stale["delivery_id"], notice_text=NOTICE, now=NOW)
    if outcome == "pending":
        claimed, = repository.claim_pending_result_deliveries(NS, channels=frozenset({"welink"}), now=NOW)
        assert_safe(claimed)
    repository.save_result_delivery_job(NS, stale)
    repository.update_result_delivery(NS, stale["delivery_id"], outcome="delivered",
                                      external_message_id="original-receipt", error_code=None,
                                      file_key="private-upload-key", now=NOW)
    row = stored_delivery(repository)
    assert row["file_key"] is None and row["handoff_done"] == 1
    assert row["status"] == "delivered"
    assert_safe(json.loads(row["job_json"]))


@pytest.mark.asyncio
async def test_guard_persistence_failure_prevents_gateway_side_effect(tmp_path, monkeypatch):
    repository, _, _, _ = prepare(tmp_path, attachment=True)
    sends = []

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("disk unavailable")

    async def send(job):
        sends.append(job)

    monkeypatch.setattr(repository, "replace_result_delivery_with_notice", unavailable)
    service = ResultDeliveryService(repository, namespace_id=NS, send_delivery=send)
    service.set_sharing_guard(AsyncMock(return_value=False))
    with pytest.raises(sqlite3.OperationalError):
        await service.dispatch_pending()
    assert sends == []


@pytest.mark.asyncio
async def test_sharing_guard_keeps_channel_claim_and_recovery_scope(tmp_path):
    repository, _, _, _ = prepare(tmp_path, attachment=True, channel="welink")
    service = ResultDeliveryService(repository, namespace_id=NS, channels=frozenset({"feishu", "dingtalk"}),
                                    send_delivery=AsyncMock())
    service.set_sharing_guard(AsyncMock(return_value=False))
    assert await service.dispatch_pending() == 0
    assert stored_delivery(repository)["status"] == "pending"
    repository.claim_pending_result_deliveries(NS, channels=frozenset({"welink"}), now=NOW)
    assert repository.recover_sending_result_deliveries(NS, channels=frozenset({"feishu", "dingtalk"}), now=NOW) == 0
    assert stored_delivery(repository)["status"] == "sending"
    assert repository.recover_sending_result_deliveries(NS, channels=frozenset({"welink"}), now=NOW) == 1
    assert stored_delivery(repository)["status"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("channel,blocked_stage", [
    ("feishu", "roster"), ("dingtalk", "roster"), ("welink", "roster"), ("welink", "save_job"),
])
@pytest.mark.parametrize("guard_raises", [False, True])
async def test_revoke_during_dispatch_preparation_redacts_before_gateway(
    tmp_path, monkeypatch, channel, blocked_stage, guard_raises,
):
    original_text = PRIVATE + " [[agent:peer]]"
    repository, _, _, _ = prepare(tmp_path, attachment=True, channel=channel, text=original_text)
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    allowed, checkpoints, sends, rows_at_send = [True], [], [], []
    save_job = repository.save_result_delivery_job
    replace_notice = repository.replace_result_delivery_with_notice

    def block(stage):
        checkpoints.append(stage + ":blocked")
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(10):
            raise TimeoutError("test did not release delivery preparation")
        checkpoints.append(stage + ":released")

    def roster(job):
        if blocked_stage == "roster":
            block("roster")
        return [{"agent_id": "peer", "name": "Peer", "app_ids": ["peer-app"]}]

    def save(namespace_id, job):
        save_job(namespace_id, job)
        checkpoints.append("job:saved")
        if blocked_stage == "save_job":
            block("save_job")

    def replace(*args, **kwargs):
        replace_notice(*args, **kwargs)
        checkpoints.append("notice:persisted")

    async def guard(job):
        checkpoints.append("guard:allowed" if allowed[0] else "guard:denied")
        if not allowed[0] and guard_raises:
            raise OSError("grant lookup failed after preparation")
        return allowed[0]

    async def send(job):
        checkpoints.append("gateway")
        sends.append(dict(job))
        rows_at_send.append(stored_delivery(repository))
        return {"delivery_id": job["delivery_id"], "stage": "send", "outcome": "delivered",
                "external_message_id": "notice-receipt", "file_key": "private-upload-key"}

    monkeypatch.setattr(repository, "save_result_delivery_job", save)
    monkeypatch.setattr(repository, "replace_result_delivery_with_notice", replace)
    service = ResultDeliveryService(repository, namespace_id=NS, channels=frozenset({channel}),
                                    send_delivery=send, mention_roster=roster, clock=lambda: NOW)
    service.set_sharing_guard(guard)
    dispatch = asyncio.create_task(service.dispatch_pending())
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        assert sends == []
        assert checkpoints[0] == "guard:allowed"
        allowed[0] = False
        checkpoints.append("revoke")
    finally:
        release.set()
        await asyncio.wait_for(dispatch, timeout=10)

    print(f"dispatch {channel}/{blocked_stage}/raises={guard_raises}: {' -> '.join(checkpoints)}")
    assert len(sends) == 1
    assert_safe(sends[0])
    assert checkpoints[-3:] == ["guard:denied", "notice:persisted", "gateway"]
    row, = rows_at_send
    assert row["status"] == "sending" and row["notice_text"] == NOTICE
    assert row["file_key"] is None and row["handoff_done"] == 1
    if channel == "welink":
        assert checkpoints.index("job:saved") < checkpoints.index("guard:denied")
        assert_safe(json.loads(row["job_json"]))
    reopened = ExecutionRepository(repository.db_path)
    assert stored_delivery(reopened)["file_key"] is None
    assert reopened.candidate_output(NS, "out-1").payload.text == original_text


@pytest.mark.asyncio
@pytest.mark.parametrize("guard_raises", [False, True])
async def test_revoke_during_welink_validation_blocks_delayed_handoff(tmp_path, guard_raises):
    repository, _, _, _ = prepare(tmp_path, attachment=True, channel="welink")
    seed_welink_snapshot(repository, outcome="delivered")
    entered, release = asyncio.Event(), asyncio.Event()
    allowed, checkpoints, handoffs = [True], [], []

    async def guard(job):
        checkpoints.append("guard:allowed" if allowed[0] else "guard:denied")
        if not allowed[0] and guard_raises:
            raise OSError("grant lookup failed during target validation")
        return allowed[0]

    async def validate(job):
        assert job["operation"] == "validate_handoff"
        checkpoints.append("validation:blocked")
        entered.set()
        await release.wait()
        checkpoints.append("validation:returned")
        return {"delivery_id": job["delivery_id"], "stage": "send", "handoff_target_valid": True}

    async def handoff(*args):
        handoffs.append(args)

    service = ResultDeliveryService(repository, namespace_id=NS, channels=frozenset({"welink"}),
                                    send_delivery=validate, handoff_handler=handoff, clock=lambda: NOW)
    service.set_sharing_guard(guard)
    reconcile = asyncio.create_task(service.reconcile_welink_deliveries())
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        allowed[0] = False
        checkpoints.append("revoke")
    finally:
        release.set()
        await asyncio.wait_for(reconcile, timeout=10)

    print(f"handoff raises={guard_raises}: {' -> '.join(checkpoints)}")
    assert checkpoints == ["guard:allowed", "validation:blocked", "revoke", "validation:returned", "guard:denied"]
    assert handoffs == []
    row = stored_delivery(repository)
    assert row["status"] == "delivered" and row["external_message_id"] == "original-receipt"
    assert row["notice_text"] == NOTICE and row["file_key"] is None and row["handoff_done"] == 1
    assert_safe(json.loads(row["job_json"]))
    assert repository.pending_welink_handoffs(NS) == []
