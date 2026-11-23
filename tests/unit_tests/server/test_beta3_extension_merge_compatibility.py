# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Preserve develop runtime contracts while integrating beta3 extension behavior."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from jiuwenswarm.common import model_selection_store as shared_model_store
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.runtime.session_lifecycle import RuntimeParticipantRegistry, SessionRunEndReason
from jiuwenswarm.server.runtime.agent_adapter import interface_deep as deep
from jiuwenswarm.server.runtime.session import model_selection_store as model_store
from jiuwenswarm.server.runtime.session import session_history as history
from jiuwenswarm.server.wire_truncate import (
    _HISTORY_WIRE_RECORD_MAX_BYTES,
    _sanitize_history_record_for_wire,
    is_restorable_history_record,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_request", [False, True])
async def test_run_end_notifies_before_releasing_the_release_manager(with_request):
    calls = []

    async def ended(event):
        calls.append(("end", event.reason))

    async def clean_manager(**kwargs):
        calls.append(("manager", kwargs["session_id"]))
        return True

    manager = SimpleNamespace(
        cleanup_session_runtime=AsyncMock(side_effect=clean_manager),
        cancel_all_inflight_work=AsyncMock(), cleanup=AsyncMock(),
    )
    registry = RuntimeParticipantRegistry(
        host_run_end=(SimpleNamespace(session_run_ended=ended),),
    )
    runtime = AgentRuntime(
        agent_manager=manager, initializer=AsyncMock(), participant_registry=registry,
        plan_controller=SimpleNamespace(reset_session=Mock()),
    )
    await runtime.start()
    await runtime._register_session(session_id="merge-session", channel_id="web")
    request = AgentRequest(request_id="cleanup", session_id="merge-session", channel_id="web")
    try:
        assert await runtime.cleanup_session(
            channel_id="web", session_id="merge-session",
            request=request if with_request else None,
            end_reason=SessionRunEndReason.OTHER,
        )
        assert calls == [
            ("end", SessionRunEndReason.OTHER),
            ("manager", "merge-session"),
        ]
        manager.cleanup_session_runtime.assert_awaited_once_with(
            channel_id="web", session_id="merge-session",
        )
        runtime.plan_controller.reset_session.assert_called_once_with("merge-session")
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_cleanup_request_remains_compatible_with_single_manager_release():
    manager = SimpleNamespace(cleanup_session_runtime=AsyncMock(return_value=True))
    runtime = AgentRuntime(
        agent_manager=manager, initializer=AsyncMock(),
        plan_controller=SimpleNamespace(reset_session=Mock()),
    )
    request = AgentRequest(request_id="cleanup", session_id="offline-session", channel_id="web")
    assert await runtime.cleanup_session(
        channel_id="web", session_id="offline-session", request=request,
    )
    manager.cleanup_session_runtime.assert_awaited_once_with(
        channel_id="web", session_id="offline-session",
    )


def test_model_selection_uses_the_extracted_common_store():
    assert model_store.get_session_model_selection is shared_model_store.get_session_model_selection
    assert model_store.set_session_model_selection is shared_model_store.set_session_model_selection
    assert model_store._read_text_ride_out_sharing is shared_model_store._read_text_ride_out_sharing


def test_model_information_uses_session_path_and_sharing_retry_reader(tmp_path, monkeypatch):
    path = tmp_path / "merge-session" / "metadata.json"
    path.parent.mkdir()
    path.touch()
    reader = Mock(return_value=json.dumps({"model": "saved-model"}))
    monkeypatch.setattr(model_store, "get_agent_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(model_store, "_read_text_ride_out_sharing", reader)

    assert model_store.get_session_model_info("merge-session") == {
        "selection": None, "model_name": "saved-model", "provider": None,
        "source": "session", "activated": None,
    }
    reader.assert_called_once_with(path.resolve())


def test_model_information_read_failure_is_not_misreported_as_missing_selection(tmp_path, monkeypatch):
    path = tmp_path / "merge-session" / "metadata.json"
    path.parent.mkdir()
    path.touch()
    failure = PermissionError("metadata remains unavailable")
    monkeypatch.setattr(model_store, "get_agent_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(model_store, "_read_text_ride_out_sharing", Mock(side_effect=failure))

    with pytest.raises(model_store.ModelSelectionError) as raised:
        model_store.get_session_model_info("merge-session")
    assert raised.value.__cause__ is failure


def _completion(request_id, timestamp):
    return {
        "id": request_id + ":request_completed", "request_id": request_id,
        "event_type": "chat.request_completed", "role": "assistant",
        "feedback_only": True, "status": "success", "timestamp": timestamp,
    }


@pytest.mark.parametrize("role", ["user", "assistant"])
@pytest.mark.parametrize("allow_gaps", [False, True])
def test_transcript_only_notes_do_not_block_completed_native_requests(role, allow_gaps):
    records = [
        {"request_id": "r1", "role": "user", "content": "first request"},
        _completion("r1", 1.0),
        {
            "request_id": "ext-note", "role": role, "content": "extension note",
            "message_origin": "extension", "transcript_only": True,
        },
        {"request_id": "r2", "role": "user", "content": "second request"},
        _completion("r2", 2.0),
    ]
    prefix = history.project_completed_session_prefix("merge-session", records, allow_gaps=allow_gaps)
    assert prefix.completed_request_ids == ("r1", "r2")
    assert prefix.completed_user_ordinals == (0, 3)
    assert prefix.boundary.ordinal == 4
    assert records[2]["role"] == role


def test_native_unfinished_request_still_blocks_a_contiguous_completed_prefix():
    records = [
        {"request_id": "r1", "role": "user", "content": "first request"},
        _completion("r1", 1.0),
        {"request_id": "unfinished", "role": "user", "content": "still running"},
        {"request_id": "r2", "role": "user", "content": "second request"},
        _completion("r2", 2.0),
    ]
    prefix = history.project_completed_session_prefix("merge-session", records)
    assert prefix.completed_request_ids == ("r1",)


@pytest.mark.parametrize("falsey", [False, True])
def test_extension_append_uses_shared_writer_handles_even_when_falsey(tmp_path, monkeypatch, falsey):
    monkeypatch.setattr(history, "get_agent_sessions_dir", lambda: tmp_path)
    writer = MagicMock()
    writer.__bool__.return_value = not falsey
    shared_lock = threading.Lock()
    file_lock = MagicMock()
    file_lock.__enter__.side_effect = shared_lock.__enter__
    file_lock.__exit__.side_effect = shared_lock.__exit__
    file_lock.__bool__.return_value = not falsey
    monkeypatch.setattr(history, "WRITE_QUEUE", writer, raising=False)
    monkeypatch.setattr(history, "FILE_LOCK", file_lock, raising=False)
    prepared = history.prepare_history_record(
        session_id="shared-handles", request_id="ext-note", channel_id="extension",
        role="user", content="stored note", timestamp=1.0,
        extra={"transcript_only": True},
    )
    record, duplicate = history.commit_history_record(
        prepared, append_key="extension:test-note", fingerprint="original-content",
        update_metadata=False,
    )
    assert record["content"] == "stored note"
    assert duplicate is False
    writer.join.assert_called_once_with()
    file_lock.__enter__.assert_called_once_with()
    file_lock.__exit__.assert_called_once()
    assert not shared_lock.locked()


def test_committed_history_reader_uses_the_shared_writer_lock(tmp_path, monkeypatch):
    session_dir = tmp_path / "shared-reader"
    session_dir.mkdir()
    records = [{"role": "user", "content": "saved note", "transcript_only": True}]
    (session_dir / "history.jsonl").write_text(json.dumps(records[0]) + "\n", encoding="utf-8")
    lock = MagicMock()
    monkeypatch.setattr(history, "get_agent_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(history, "flush_pending_writes", Mock(return_value=True))
    monkeypatch.setattr(history, "FILE_LOCK", lock)

    assert history.read_committed_main_history("shared-reader") == records
    history.flush_pending_writes.assert_called_once_with()
    lock.__enter__.assert_called_once_with()
    lock.__exit__.assert_called_once()


def test_mcp_app_structure_survives_history_sanitizing_without_append_markers():
    block = {"structuredContent": {"rows": [{"values": list(range(120))}]}}
    record = {
        "role": "assistant", "event_type": "chat.tool_result", "content": "tool result",
        "raw_output": {"mcp_app": block},
        "_session_append": {"key": "private-identity", "fingerprint": "private-fingerprint"},
    }
    sanitized = _sanitize_history_record_for_wire(record)
    assert sanitized["raw_output"]["mcp_app"] == block
    assert "_session_append" not in sanitized
    assert is_restorable_history_record(sanitized)


def test_mcp_app_restoration_keeps_the_per_record_size_limit():
    record = {
        "role": "assistant", "event_type": "chat.tool_result", "content": "tool result",
        "raw_output": {"mcp_app": {"structuredContent": {"text": "x" * (_HISTORY_WIRE_RECORD_MAX_BYTES + 1)}}},
    }
    sanitized = _sanitize_history_record_for_wire(record)
    assert sanitized["truncated"] is True
    assert "mcp_app" not in sanitized.get("raw_output", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("project_binding", ["explicit", None, ""])
async def test_workspace_binding_keeps_history_facts_and_both_memory_agent_coordinates(
    tmp_path, monkeypatch, project_binding,
):
    from jiuwenswarm.server.runtime.session.history_context import bound_history_context, history_context_scope

    adapter = deep.JiuWenSwarmDeepAdapter()
    adapter._project_dir = str(tmp_path) if project_binding == "explicit" else project_binding
    adapter._instance = SimpleNamespace(_react_agent=SimpleNamespace())
    monkeypatch.setattr(deep, "init_cwd", Mock())
    monkeypatch.setattr(deep, "get_cwd", lambda: str(tmp_path))
    monkeypatch.setattr(adapter, "_resolve_runtime_language", lambda: "cn")

    class WorkspaceBound(Exception):
        pass

    def after_workspace(config, channel_id, session_id, *, request_id):
        assert channel_id == "web"
        assert session_id == "workspace-session"
        assert request_id == "workspace-request"
        raise WorkspaceBound()

    monkeypatch.setattr(adapter, "_sync_a4p_tools_for_runtime", after_workspace)
    with history_context_scope("workspace-session", request_id="workspace-request"):
        with pytest.raises(WorkspaceBound):
            await adapter._apply_runtime_config_stages(
                adapter._RuntimeConfig(
                    session_id="workspace-session", request_id="workspace-request",
                    mode="code", channel_id="web", workspace=str(tmp_path),
                ),
                SimpleNamespace(mark=Mock()), bind_request=True,
            )
        context = bound_history_context("workspace-session")
        assert context.cwd == str(tmp_path)
        if project_binding == "explicit":
            assert context.metadata["project_root"] == str(tmp_path)
        else:
            assert "project_root" not in context.metadata
        for agent in (adapter._instance, adapter._instance._react_agent):
            assert agent._jiuwenswarm_project_dir == str(tmp_path)
            assert agent._jiuwenswarm_is_projectless is False
    assert bound_history_context("workspace-session") is None
