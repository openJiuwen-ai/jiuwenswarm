"""Confirmed receipts reconcile with platform history without resending or bypassing consent."""

import asyncio
from datetime import timedelta
import json
from unittest.mock import AsyncMock

import pytest

from openjiuwen.harness import persistent_agent as core
from jiuwenswarm.server.jiuwenmate.conversation_resources import ConversationResources
from jiuwenswarm.server.jiuwenmate.delivery_service import ResultDeliveryService
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from tests.unit_tests.server.jiuwenmate.test_delivery_sharing_redaction import (
    NOTICE, PRIVATE, prepare, stored_delivery,
)
from tests.unit_tests.server.jiuwenmate.test_commit_port import NOW, NS


DELIVERED_NOTICE = "该消息此前已送达；跨群分享授权现已失效，后续访问和分享仍需授权。"


def confirm(repository):
    job, = repository.claim_pending_result_deliveries(NS, channels=frozenset({"feishu", "dingtalk"}), now=NOW)
    repository.update_result_delivery(NS, job["delivery_id"], outcome="delivered",
                                      external_message_id="platform-receipt", error_code=None, now=NOW)
    return job


@pytest.mark.parametrize("channel", ["feishu", "dingtalk"])
@pytest.mark.parametrize("attachment", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("guard_raises", [False, True])
async def test_revoked_confirmed_echo_reconciles_once_without_overwriting_or_resending(
    tmp_path, channel, attachment, legacy, guard_raises,
):
    repository, execution, archive, group = prepare(tmp_path, attachment=attachment, channel=channel)
    job = confirm(repository)
    actual = core.GroupMessage(message_id="platform-receipt", sender_id="platform-bot-id", sender_name="Bot",
                               sender_kind="agent", received_at=NOW, text="Actual platform body with resolved mentions",
                               attachment_refs=(f"{channel}:attachment:platform-receipt:key",) if attachment else ())
    archive.append(NS, group.group_chat_id, actual, source_ref=f"{channel}:message:platform-receipt")
    if legacy:
        repository.replace_result_delivery_with_notice(NS, job["delivery_id"], notice_text=NOTICE, now=NOW)
    reopened = ExecutionRepository(repository.db_path)
    resources = ConversationResources(archive, reopened)
    send, handoff = AsyncMock(), AsyncMock()

    async def save(job):
        await asyncio.to_thread(resources.archive_delivery, job)

    service = ResultDeliveryService(reopened, namespace_id=NS, channels=frozenset({channel}),
                                    send_delivery=send, archive_delivery=save, handoff_handler=handoff,
                                    clock=lambda: NOW + timedelta(seconds=1))
    service.set_sharing_guard(AsyncMock(side_effect=OSError("authorization lookup unavailable"))
                              if guard_raises else AsyncMock(return_value=False))
    assert await service.dispatch_pending() == 0
    assert reopened.pending_delivery_archives(NS) == []
    await service.reconcile_archives()
    saved, = archive.current_messages(NS, group.group_chat_id, (actual.message_id,))
    assert saved == actual
    row = stored_delivery(reopened)
    assert row["status"] == "delivered" and row["external_message_id"] == actual.message_id
    assert row["notice_text"] == DELIVERED_NOTICE and row["file_key"] is None and row["handoff_done"] == 1
    assert reopened.candidate_output(NS, "out-1").payload.text == PRIVATE
    send.assert_not_awaited()
    handoff.assert_not_awaited()


def test_repeated_notice_replacement_does_not_keep_advancing_receipt_timestamp(tmp_path):
    repository, _, _, _ = prepare(tmp_path, attachment=True)
    job = confirm(repository)
    repository.replace_result_delivery_with_notice(NS, job["delivery_id"], notice_text=NOTICE, now=NOW)
    before = stored_delivery(repository)
    repository.replace_result_delivery_with_notice(NS, job["delivery_id"], notice_text=NOTICE,
                                                   now=NOW + timedelta(minutes=1))
    assert stored_delivery(repository) == before


async def test_conflicting_human_message_is_not_silently_accepted_as_bot_delivery(tmp_path, caplog):
    repository, _, archive, group = prepare(tmp_path, attachment=False)
    confirm(repository)
    actual = core.GroupMessage(message_id="platform-receipt", sender_id="human", sender_name="User",
                               sender_kind="human", received_at=NOW, text="Unrelated message")
    archive.append(NS, group.group_chat_id, actual, source_ref="feishu:message:platform-receipt")
    resources = ConversationResources(archive, repository)

    async def save(job):
        await asyncio.to_thread(resources.archive_delivery, job)

    send = AsyncMock()
    service = ResultDeliveryService(repository, namespace_id=NS, send_delivery=send, archive_delivery=save)
    await service.reconcile_archives()
    assert len(repository.pending_delivery_archives(NS)) == 1
    assert "error_code=CONFLICT" in caplog.text
    assert archive.current_messages(NS, group.group_chat_id, (actual.message_id,)) == (actual,)
    send.assert_not_awaited()


def test_confirmed_archive_checks_platform_as_well_as_chat_id(tmp_path):
    repository, _, archive, _ = prepare(tmp_path, attachment=False)
    confirm(repository)
    job, = repository.pending_delivery_archives(NS)
    record = json.loads(job["record_json"])
    record["route_snapshot"]["channel"] = "dingtalk"
    job["record_json"] = json.dumps(record)
    with pytest.raises(ValueError):
        ConversationResources(archive, repository).archive_delivery(job)


async def test_archive_failure_keeps_retry_but_logs_only_safe_diagnostic_type(tmp_path, caplog):
    repository, _, _, _ = prepare(tmp_path, attachment=False)
    confirm(repository)
    save = AsyncMock(side_effect=OSError("private path and credential"))
    service = ResultDeliveryService(repository, namespace_id=NS, send_delivery=AsyncMock(), archive_delivery=save)
    await service.reconcile_archives()
    assert len(repository.pending_delivery_archives(NS)) == 1
    assert "error_type=OSError" in caplog.text
    assert "private path and credential" not in caplog.text


@pytest.mark.parametrize("channel", ["feishu", "dingtalk"])
async def test_delayed_candidate_does_not_overwrite_an_existing_platform_edit(tmp_path, channel):
    repository, _, archive, group = prepare(tmp_path, attachment=False, channel=channel)
    confirm(repository)
    actual = core.GroupMessage(message_id="platform-receipt", sender_id="platform-bot-id", sender_name="Bot",
                               sender_kind="agent", received_at=NOW, text="A newer platform edit")
    archive.append(NS, group.group_chat_id, actual, source_ref=f"{channel}:message:platform-receipt",
                   changed_at=NOW + timedelta(seconds=1))
    resources = ConversationResources(archive, repository)

    async def save(job):
        await asyncio.to_thread(resources.archive_delivery, job)

    service = ResultDeliveryService(repository, namespace_id=NS, channels=frozenset({channel}),
                                    send_delivery=AsyncMock(), archive_delivery=save)
    service.set_sharing_guard(AsyncMock(return_value=True))
    await service.reconcile_archives()
    assert repository.pending_delivery_archives(NS) == []
    assert archive.current_messages(NS, group.group_chat_id, (actual.message_id,)) == (actual,)
    assert stored_delivery(repository)["notice_text"] is None
