"""JM-09 / PA-08: shared workspace outputs, paused handoff and file deliveries."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import pytest
from jiuwenswarm.common.file_download_token import WebFileDownloadManager, validate_file_download_token
from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager
from jiuwenswarm.gateway.channel_manager.im_platforms.feishu.feishu_connect import FeishuChannel, FeishuConfig
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
from jiuwenswarm.server.jiuwenmate.agent_manager import AgentManager
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
    WorkspaceOutputFiles,
    workspace_output_key,
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


def _file_event(
    execution, seq, *, output_id="out-file", file_ref=FILE_REF, size=len(CONTENT),
    caption="本周周报", file_path="output/周报.md",
):
    payload = {
        "output_id": output_id,
        "target_ref": "reply-1",
        "share_mode": "attachment",
        "file_ref": file_ref,
        "file_path": file_path,
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
    (office / "output" / "周报.md").write_bytes(CONTENT)
    files = WorkspaceOutputFiles(workspaces)
    queue = CommitQueue(repository)
    await queue.start()
    yield SimpleNamespace(
        repository=repository,
        queue=queue,
        port=HostCommitPort(repository, queue, files=files),
        files=files,
        office=office,
        data_dir=tmp_path,
    )
    await queue.aclose()


async def test_final_uses_the_verified_workspace_file_and_makes_it_available_on_web(host):
    state = _accept(host.repository, "web:m-1")
    execution = _start(host.repository, state)

    await host.port.record(execution, _file_event(execution, 1))
    await host.port.record(execution, _final(execution, 2))

    source = host.office / "output" / "周报.md"
    assert source.read_bytes() == CONTENT
    assert host.files.resolve(NS, AGENT, "output/周报.md", FILE_REF, len(CONTENT)) == source
    assert not (host.data_dir / "outputs").exists()
    source.write_bytes(b"edited later")
    assert host.files.resolve(NS, AGENT, "output/周报.md", FILE_REF, len(CONTENT)) is None
    deliveries = host.repository.result_deliveries(NS, state.record.request_id)
    assert [(item["output_id"], item["channel"], item["status"]) for item in deliveries] == [
        ("out-file", "web", "available")
    ]


async def test_web_file_view_signs_the_agent_output_without_copying_it(host, monkeypatch):
    state = _accept(host.repository, "web:web-file-view")
    execution = _start(host.repository, state)
    output = _file_event(execution, 1).payload
    manager = object.__new__(AgentManager)
    manager._files = host.files
    monkeypatch.setattr(WebFileDownloadManager, "_instance", WebFileDownloadManager(secret="s" * 32))

    view = manager._file_view(
        SimpleNamespace(namespace_id=NS, agent_id=AGENT, session_id=state.record.session_id), output
    )

    token = parse_qs(urlparse(view["download_url"]).query)["token"][0]
    assert validate_file_download_token(token)["path"] == str(host.office / "output" / "周报.md")
    assert not (host.data_dir / "outputs").exists()


async def test_shared_workspace_output_is_not_copied_into_a_second_persistent_directory(host):
    (host.office / "output" / "最终报告.md").write_bytes(CONTENT)
    state = _accept(host.repository, "web:output-1")
    execution = _start(host.repository, state)

    await host.port.record(execution, _file_event(execution, 1, file_path="output/最终报告.md"))
    await host.port.record(execution, _final(execution, 2))

    assert host.files.resolve(NS, AGENT, "output/最终报告.md", FILE_REF, len(CONTENT)) == (
        host.office / "output" / "最终报告.md"
    )
    assert not (host.data_dir / "outputs").exists()


async def test_file_changed_after_handoff_keeps_the_ending_unrecorded(host):
    state = _accept(host.repository, "web:m-1")
    execution = _start(host.repository, state)
    (host.office / "output" / "周报.md").write_bytes(b"changed before saving")

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


async def test_file_candidate_outside_shared_output_is_not_handed_off(host):
    state = _accept(host.repository, "web:non-output-file")
    execution = _start(host.repository, state)

    await host.port.record(execution, _file_event(execution, 1, file_path="reports/周报.md"))
    await host.queue.drain()

    with pytest.raises(PersistentAgentError) as caught:
        await host.port.record(execution, _final(execution, 2))

    assert caught.value.code == "OUTPUT_FILE_INVALID"
    assert await host.port.read_execution_log(execution, execution.execution_id) == ()


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


async def test_paused_hands_off_files_and_feishu_claims_the_shared_workspace_output(host):
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
    assert file["key"] == f"{NS}/{AGENT}/output/周报.md"
    assert host.files.resolve_key(file["key"], file["file_ref"], file["size"]) == (
        host.office / "output" / "周报.md"
    )

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


def test_workspace_output_key_rejects_paths_outside_output():
    with pytest.raises(PersistentAgentError) as caught:
        workspace_output_key(NS, AGENT, "reports/周报.md", FILE_REF, len(CONTENT))
    assert caught.value.code == "OUTPUT_FILE_INVALID"


async def test_channel_manager_reads_the_agent_output_directly_and_rejects_changed_content(tmp_path, monkeypatch):
    source = tmp_path / "workspaces" / NS / AGENT / "output" / "周报.md"
    source.parent.mkdir(parents=True)
    source.write_bytes(CONTENT)
    monkeypatch.setattr("jiuwenswarm.server.jiuwenmate.service.default_data_dir", lambda: tmp_path)
    manager = ChannelManager(object.__new__(MessageHandler))
    channel = FeishuChannel(FeishuConfig(enabled=True, app_id="cli_bound"), MagicMock())
    channel.send_persistent_result = AsyncMock(return_value={"outcome": "delivered", "external_message_id": "om_f"})
    manager.register_channel(channel)
    payload = {
        "app_id": "cli_bound",
        "namespace_id": NS,
        "agent_id": AGENT,
        "chat_id": "oc_group",
        "idempotency_key": "k",
        "text": None,
        "file": {
            "key": f"{NS}/{AGENT}/output/周报.md",
            "name": "周报.md",
            "size": len(CONTENT),
            "file_ref": FILE_REF,
            "file_key": None,
        },
    }

    result = await manager.deliver_persistent_result(payload)

    assert result["outcome"] == "delivered"
    assert channel.send_persistent_result.await_args.kwargs["file_path"] == str(source)
    assert channel.send_persistent_result.await_args.kwargs["file_key"] is None
    assert not (tmp_path / "outputs").exists()

    already_uploaded = {
        **payload,
        "file": {**payload["file"], "file_key": "fk_existing"},
    }
    result = await manager.deliver_persistent_result(already_uploaded)
    assert result["outcome"] == "delivered"
    kwargs = channel.send_persistent_result.await_args.kwargs
    assert kwargs["file_path"] is None
    assert kwargs["file_key"] == "fk_existing"

    source.write_bytes(b"changed after candidate acceptance")
    result = await manager.deliver_persistent_result(payload)

    assert result == {"outcome": "failed", "error_code": "OUTPUT_FILE_MISSING"}
    assert channel.send_persistent_result.await_count == 2


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


@pytest.mark.parametrize("source_message_id", ["om_source", None])
async def test_feishu_qr_result_is_an_inline_image_with_caption(tmp_path, source_message_id):
    channel = FeishuChannel(FeishuConfig(enabled=True, app_id="cli_bound"), MagicMock())
    channel._api_client = MagicMock()
    api = channel._api_client.im.v1
    api.image.create.return_value = SimpleNamespace(
        success=lambda: True, data=SimpleNamespace(image_key="img_v3_qr"),
    )
    api.file.create.return_value = SimpleNamespace(
        success=lambda: True, data=SimpleNamespace(file_key="file_v3_qr"),
    )
    response = SimpleNamespace(success=lambda: True, data=SimpleNamespace(message=SimpleNamespace(message_id="om_qr")))
    api.message.reply.return_value = response
    api.message.create.return_value = response
    source = tmp_path / "qr_calendar_auth.PNG"
    source.write_bytes(b"\x89PNG\r\n\x1a\nqr-test")

    result = await channel.send_persistent_result(
        chat_id="oc_group", text="飞书日历授权二维码", source_message_id=source_message_id,
        idempotency_key="qr-key", file_path=str(source),
    )

    assert result == {"outcome": "delivered", "external_message_id": "om_qr", "file_key": "img_v3_qr"}
    send = api.message.reply if source_message_id else api.message.create
    caption, image = [call.args[0] for call in send.call_args_list]
    assert caption.request_body.msg_type == "interactive"
    assert caption.request_body.uuid != "qr-key"
    assert image.request_body.msg_type == "image"
    assert json.loads(image.request_body.content) == {"image_key": "img_v3_qr"}
    assert image.request_body.uuid == "qr-key"
    if source_message_id:
        assert image.message_id == source_message_id
    else:
        assert image.request_body.receive_id == "oc_group"
    assert api.image.create.call_count == 1
    api.file.create.assert_not_called()


async def test_feishu_image_retry_reuses_key_without_workspace_file(tmp_path):
    channel = FeishuChannel(FeishuConfig(enabled=True, app_id="cli_bound"), MagicMock())
    channel._api_client = MagicMock()
    api = channel._api_client.im.v1
    api.image.create.return_value = SimpleNamespace(
        success=lambda: True, data=SimpleNamespace(image_key="img_v3_qr"),
    )
    api.file.create.return_value = SimpleNamespace(
        success=lambda: True, data=SimpleNamespace(file_key="file_v3_qr"),
    )
    api.message.create.side_effect = [
        TimeoutError("send receipt lost"),
        SimpleNamespace(success=lambda: True, data=SimpleNamespace(message=SimpleNamespace(message_id="om_qr"))),
    ]
    source = tmp_path / "qr.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nqr-test")
    first = await channel.send_persistent_result(
        chat_id="oc_group", text=None, source_message_id=None, idempotency_key="qr-key", file_path=str(source),
    )
    assert first["outcome"] == "unknown"

    result = await channel.send_persistent_result(
        chat_id="oc_group", text=None, source_message_id=None, idempotency_key="qr-key", file_key=first["file_key"],
    )

    assert result == {"outcome": "delivered", "external_message_id": "om_qr", "file_key": "img_v3_qr"}
    assert api.image.create.call_count == 1
    api.file.create.assert_not_called()
    for call in api.message.create.call_args_list:
        assert call.args[0].request_body.msg_type == "image"
        assert call.args[0].request_body.uuid == "qr-key"
        assert json.loads(call.args[0].request_body.content) == {"image_key": "img_v3_qr"}


async def test_feishu_image_upload_failure_does_not_send_another_file(tmp_path):
    channel = FeishuChannel(FeishuConfig(enabled=True, app_id="cli_bound"), MagicMock())
    channel._api_client = MagicMock()
    api = channel._api_client.im.v1
    api.image.create.return_value = SimpleNamespace(success=lambda: False, code=999, msg="image rejected")
    api.file.create.return_value = SimpleNamespace(
        success=lambda: True, data=SimpleNamespace(file_key="file_v3_qr"),
    )
    api.message.create.return_value = SimpleNamespace(
        success=lambda: True, data=SimpleNamespace(message=SimpleNamespace(message_id="om_wrong_file")),
    )
    source = tmp_path / "qr.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nqr-test")

    result = await channel.send_persistent_result(
        chat_id="oc_group", text=None, source_message_id=None, idempotency_key="qr-key", file_path=str(source),
    )

    assert result == {"outcome": "failed", "error_code": "FEISHU_UPLOAD_FAILED"}
    api.file.create.assert_not_called()
    api.message.create.assert_not_called()


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


async def test_feishu_file_retry_with_uploaded_key_does_not_need_workspace_file():
    channel = FeishuChannel(FeishuConfig(enabled=True, app_id="cli_bound"), MagicMock())
    channel._api_client = object()
    channel._upload_file = MagicMock()
    channel._send_persistent_message = AsyncMock(
        return_value={"outcome": "delivered", "external_message_id": "om_f"}
    )

    result = await channel.send_persistent_result(
        chat_id="oc_group", text=None, source_message_id=None, idempotency_key="key-1",
        file_path=None, file_key="fk_existing",
    )

    assert result == {"outcome": "delivered", "external_message_id": "om_f", "file_key": "fk_existing"}
    channel._upload_file.assert_not_called()
    assert channel._send_persistent_message.await_args.args[1] == "file"
