"""JM-09 / PA-08: retained file candidates, paused handoff and file deliveries."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager
from jiuwenswarm.gateway.channel_manager.im_platforms.feishu.feishu_connect import FeishuChannel, FeishuConfig
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
from jiuwenswarm.server.jiuwenmate.delivery_service import ResultDeliveryService
from jiuwenswarm.server.jiuwenmate.execution_models import (
    AccessSnapshot,
    ConversationRef,
    RequestRouteSnapshot,
    build_execution_context,
)
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.output_files import (
    OUTPUTS_DIR,
    OutputFileStore,
    output_file_key,
    resolve_output_file,
)
from jiuwenswarm.server.jiuwenmate.ports.commit import CommitQueue, HostCommitPort
from jiuwenswarm.server.jiuwenmate.ports.workspace_access import WORKSPACE_READ, WorkspaceStore
from jiuwenswarm.server.jiuwenmate.spec_provider import publish_spec

from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
from openjiuwen.harness import persistent_agent as core
from openjiuwen.harness.schema.deep_agent_spec import TeamModelConfig

NS = "workspace-1"
AGENT = "mate_dev"
NOW = datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc)
CONTENT = b"weekly report\n"
FILE_REF = "sha256:" + hashlib.sha256(CONTENT).hexdigest()
SNAPSHOT = publish_spec(
    AGENT,
    "v1",
    {
        "card": {"id": AGENT, "name": "研发员工", "description": "研发"},
        "system_prompt": "你是研发员工。",
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


def _access(*features: str) -> AccessSnapshot:
    scope = frozenset({"chat", *features})
    return AccessSnapshot(
        principal_ref="user-1", startup_scope=scope, source_scope=scope, policy_revision="policy-1"
    )


def _accept(repository, source_ref, *, access=None, route=None):
    session = repository.resolve_session(NS, AGENT, ConversationRef.web("conv-1"), now=NOW)
    state, _ = repository.accept_request(
        session,
        snapshot=SNAPSHOT,
        source_ref=source_ref,
        access=access or _access(WORKSPACE_READ),
        request_input=core.RequestInput(kind="scheduled", text="整理周报", reply_target="reply-1"),
        received_at=NOW,
        route_snapshot=route or RequestRouteSnapshot(channel="web", target_id="conv-1", source_message_id="m-1"),
    )
    return state


def _start(repository, state):
    execution = repository.start_execution(NS, state.record.request_id, now=NOW)
    return build_execution_context(state.record, execution, capability_snapshot=SNAPSHOT.enabled_features)


def _file_event(execution, seq, *, output_id="out-file", file_ref=FILE_REF, size=len(CONTENT), caption="本周周报"):
    payload = {
        "output_id": output_id,
        "target_ref": "reply-1",
        "share_mode": "attachment",
        "file_ref": file_ref,
        "file_path": "reports/周报.md",
        "file_size": size,
    }
    if caption is not None:
        payload["text"] = caption
    return core.AgentEvent.create(
        execution,
        event_type=core.EventType.OUTPUT_PROPOSED,
        local_seq=seq,
        occurred_at=NOW,
        visibility="candidate",
        payload=payload,
    )


def _final(execution, seq, refs=("out-file",)):
    return core.AgentEvent.create(
        execution,
        event_type=core.EventType.EXECUTION_FINAL,
        local_seq=seq,
        occurred_at=NOW,
        payload={"status": "completed", "output_refs": refs},
    )


def _paused(execution, seq, refs=("out-file",)):
    return core.AgentEvent.create(
        execution,
        event_type=core.EventType.EXECUTION_PAUSED,
        local_seq=seq,
        occurred_at=NOW,
        payload={"reason": "budget_denied", "detail": "额度不足", "output_refs": refs},
    )


@pytest.fixture
async def host(tmp_path):
    repository = ExecutionRepository(tmp_path / "agents.sqlite3")
    workspaces = WorkspaceStore(tmp_path / "workspaces")
    office = workspaces.ensure(NS, AGENT)
    (office / "reports").mkdir()
    (office / "reports" / "周报.md").write_bytes(CONTENT)
    files = OutputFileStore(tmp_path / OUTPUTS_DIR, workspaces)
    queue = CommitQueue(repository)
    await queue.start()
    yield SimpleNamespace(
        repository=repository,
        queue=queue,
        port=HostCommitPort(repository, queue, files=files),
        files=files,
        office=office,
    )
    await queue.aclose()


async def test_final_retains_a_verified_copy_and_makes_the_file_available_on_web(host):
    state = _accept(host.repository, "web:m-1")
    execution = _start(host.repository, state)

    await host.port.record(execution, _file_event(execution, 1))
    await host.port.record(execution, _final(execution, 2))

    copy = host.files.root / output_file_key(NS, FILE_REF, "reports/周报.md")
    assert copy.read_bytes() == CONTENT
    (host.office / "reports" / "周报.md").write_bytes(b"edited later")
    assert copy.read_bytes() == CONTENT
    deliveries = host.repository.result_deliveries(NS, state.record.request_id)
    assert [(item["output_id"], item["channel"], item["status"]) for item in deliveries] == [
        ("out-file", "web", "available")
    ]


async def test_file_changed_after_handoff_keeps_the_ending_unrecorded(host):
    state = _accept(host.repository, "web:m-1")
    execution = _start(host.repository, state)
    (host.office / "reports" / "周报.md").write_bytes(b"changed before saving")

    await host.port.record(execution, _file_event(execution, 1))
    await host.queue.drain()
    with pytest.raises(PersistentAgentError) as caught:
        await host.port.record(execution, _final(execution, 2))

    assert caught.value.code == "OUTPUT_FILE_MISMATCH"
    log = await host.port.read_execution_log(execution, execution.execution_id)
    assert log == ()
    assert host.repository.result_deliveries(NS, state.record.request_id) == []


async def test_file_outside_the_read_scope_is_not_saved(host):
    state = _accept(host.repository, "web:m-1", access=_access())
    execution = _start(host.repository, state)

    await host.port.record(execution, _file_event(execution, 1))
    with pytest.raises(PersistentAgentError) as caught:
        await host.port.record(execution, _final(execution, 2))

    assert caught.value.code == "OUTPUT_FILE_FORBIDDEN"


async def test_a_later_execution_may_propose_the_same_output_only_with_the_same_content(host):
    state = _accept(host.repository, "web:m-1")
    first = _start(host.repository, state)
    await host.port.record(first, _file_event(first, 1))
    await host.port.record(first, _paused(first, 2, refs=()))
    # Continuation is not opened yet (JM-13); requeue the Request to start its next Execution.
    with host.repository._transaction() as conn:
        conn.execute("UPDATE requests SET status = 'queued' WHERE request_id = ?", (state.record.request_id,))
    second = _start(host.repository, state)

    await host.port.record(second, _file_event(second, 1))
    await host.port.record(second, _final(second, 2))
    assert len(host.repository.result_deliveries(NS, state.record.request_id)) == 1

    other = _file_event(second, 3, caption="另一份说明")
    with pytest.raises(PersistentAgentError) as caught:
        host.repository.record_events([other], now=NOW)
    assert caught.value.code == "CONFLICT"


async def test_paused_hands_off_files_and_feishu_claims_the_retained_copy(host):
    route = RequestRouteSnapshot(channel="feishu", target_id="oc_group", app_id="cli_bound", source_message_id="om_1")
    state = _accept(host.repository, "feishu:om_1", route=route)
    execution = _start(host.repository, state)

    await host.port.record(execution, _file_event(execution, 1))
    await host.port.record(execution, _paused(execution, 2))

    jobs = host.repository.claim_pending_result_deliveries(NS, now=NOW)
    assert len(jobs) == 1
    file = jobs[0]["file"]
    assert file["name"] == "周报.md"
    assert file["size"] == len(CONTENT)
    assert file["file_key"] is None
    assert jobs[0]["text"] == "本周周报"
    assert resolve_output_file(host.files.root, file["key"]).read_bytes() == CONTENT

    row = host.repository.update_result_delivery(
        NS, jobs[0]["delivery_id"], outcome="failed", external_message_id=None, error_code="FEISHU_1", now=NOW,
        file_key="fk_1",
    )
    assert row["file_key"] == "fk_1"


async def test_delivery_service_wakes_on_paused_and_keeps_the_uploaded_file_key(host):
    route = RequestRouteSnapshot(channel="feishu", target_id="oc_group", app_id="cli_bound", source_message_id="om_1")
    state = _accept(host.repository, "feishu:om_1", route=route)
    execution = _start(host.repository, state)
    await host.port.record(execution, _file_event(execution, 1, caption=None))
    await host.port.record(execution, _paused(execution, 2))
    sent = []

    async def send(job):
        sent.append(job)
        return {"delivery_id": job["delivery_id"], "stage": "send", "outcome": "pending", "file_key": "fk_up"}

    service = ResultDeliveryService(host.repository, namespace_id=NS, send_delivery=send)
    assert await service.dispatch_pending() == 1
    assert sent[0]["file"]["file_key"] is None
    assert host.repository.claim_pending_result_deliveries(NS, now=NOW)[0]["file"]["file_key"] == "fk_up"


def test_resolve_output_file_rejects_malformed_keys(tmp_path):
    assert resolve_output_file(tmp_path, "../etc/passwd") is None
    assert resolve_output_file(tmp_path, f"{NS}/{'0' * 64}/..") is None
    assert resolve_output_file(tmp_path, f"{NS}/{'0' * 64}/missing.md") is None


async def test_channel_manager_sends_the_retained_copy_with_its_upload_key(tmp_path, monkeypatch):
    key = output_file_key(NS, FILE_REF, "reports/周报.md")
    copy = tmp_path / OUTPUTS_DIR / key
    copy.parent.mkdir(parents=True)
    copy.write_bytes(CONTENT)
    monkeypatch.setattr("jiuwenswarm.server.jiuwenmate.service.default_data_dir", lambda: tmp_path)
    manager = ChannelManager(object.__new__(MessageHandler))
    channel = FeishuChannel(FeishuConfig(enabled=True, app_id="cli_bound"), MagicMock())
    channel.send_persistent_result = AsyncMock(return_value={"outcome": "delivered", "external_message_id": "om_f"})
    manager.register_channel(channel)
    payload = {"app_id": "cli_bound", "chat_id": "oc_group", "idempotency_key": "k", "text": None}

    result = await manager.deliver_persistent_result(
        {**payload, "file": {"key": key, "size": len(CONTENT), "file_key": "fk_1"}}
    )
    assert result["outcome"] == "delivered"
    kwargs = channel.send_persistent_result.await_args.kwargs
    assert kwargs["file_path"] == str(copy)
    assert kwargs["file_key"] == "fk_1"
    assert kwargs["text"] is None

    missing = await manager.deliver_persistent_result({**payload, "file": {"key": key, "size": 1}})
    assert missing == {"outcome": "failed", "error_code": "OUTPUT_FILE_MISSING"}


async def test_feishu_file_result_sends_caption_then_uploads_once_and_reports_file_key(tmp_path):
    channel = FeishuChannel(FeishuConfig(enabled=True, app_id="cli_bound"), MagicMock())
    channel._api_client = object()
    channel._upload_file = MagicMock(return_value="fk_new")
    channel._send_persistent_message = AsyncMock(
        side_effect=[
            {"outcome": "delivered", "external_message_id": "om_caption"},
            {"outcome": "delivered", "external_message_id": "om_file"},
        ]
    )

    result = await channel.send_persistent_result(
        chat_id="oc_group", text="本周周报", source_message_id="om_1", idempotency_key="key-1",
        file_path=str(tmp_path / "周报.md"),
    )

    assert result == {"outcome": "delivered", "external_message_id": "om_file", "file_key": "fk_new"}
    caption_call, file_call = channel._send_persistent_message.await_args_list
    assert caption_call.args[1] == "interactive" and caption_call.args[4] != "key-1"
    assert file_call.args[1] == "file" and file_call.args[4] == "key-1"
    assert '"fk_new"' in file_call.args[2]
    channel._upload_file.assert_called_once()


async def test_feishu_file_retry_reuses_the_uploaded_file_key(tmp_path):
    channel = FeishuChannel(FeishuConfig(enabled=True, app_id="cli_bound"), MagicMock())
    channel._api_client = object()
    channel._upload_file = MagicMock()
    channel._send_persistent_message = AsyncMock(return_value={"outcome": "failed", "error_code": "FEISHU_9"})

    result = await channel.send_persistent_result(
        chat_id="oc_group", text=None, source_message_id=None, idempotency_key="key-1",
        file_path=str(tmp_path / "周报.md"), file_key="fk_old",
    )

    channel._upload_file.assert_not_called()
    assert result == {"outcome": "failed", "error_code": "FEISHU_9", "file_key": "fk_old"}
