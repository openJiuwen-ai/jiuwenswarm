"""File resources survive Core delegation, status recovery and visible history."""
# pylint: disable=protected-access

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.extensions.video_duplex.backend import video_live, video_search
from jiuwenswarm.extensions.video_duplex.backend.video_files import normalize_file_items
from jiuwenswarm.server.runtime.session import session_history, session_metadata
from jiuwenswarm.agents.harness.common.tools.file_delivery_policy import is_send_file_enabled
from jiuwenswarm.agents.harness.common.tools import send_file_to_user, web_file_download


FILE = {
    "name": "report.py", "path": "C:/work/report.py", "size": 12,
    "mime_type": "text/x-python", "download_url": "/file-api/download?token=test-token",
    "download_token": "test-token",
}


def test_duplex_file_tool_inherits_web_policy_without_overriding_explicit_settings():
    assert is_send_file_enabled(None, "video_tool")
    assert is_send_file_enabled(None, "web")
    assert not is_send_file_enabled(None, "feishu")
    assert not is_send_file_enabled({"channels": {"web": {"send_file_allowed": False}}}, "video_tool")
    assert not is_send_file_enabled({"channels": {"video_tool": {"send_file_allowed": False}}}, "video_tool")
    assert is_send_file_enabled({"channels": {
        "web": {"send_file_allowed": False}, "video_tool": {"send_file_allowed": True},
    }}, "video_tool")


def test_file_bridge_preserves_resources_and_ignores_malformed_entries():
    assert normalize_file_items([FILE, None, {}, {"name": "text-only"}, {"path": "unnamed"}]) == [FILE]
    assert normalize_file_items("C:/work/report.py") == []
    assert video_search.core_agent_progress({"event_type": "chat.file", "files": [FILE]})["files"] == [FILE]
    assert video_search.core_agent_progress({"event_type": "chat.file", "files": []}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("file_in_final", [False, True])
async def test_streamed_files_remain_in_job_status_after_missed_delivery(file_in_final):
    async def stream(_request):
        if not file_in_final:
            yield SimpleNamespace(payload={"event_type": "chat.file", "files": [FILE]})
        yield SimpleNamespace(payload={
            "event_type": "chat.final", "content": "文件已准备好",
            **({"files": [FILE]} if file_in_final else {}),
        })

    channel = SimpleNamespace(send_event=AsyncMock(side_effect=ConnectionError("client unavailable")),
                              send_response=AsyncMock())
    manager = video_search.VideoSearchManager(
        channel, SimpleNamespace(send_request_stream=stream), log_event=lambda _: None, qwen_active=lambda: True,
    )
    job = manager.start(None, question="生成文件", query="生成文件", search_session_id="task-duplex:visible")
    await asyncio.wait_for(asyncio.gather(*list(manager._tasks)), 5)
    await manager.handle_status(None, "status", {
        "job_id": job["id"], "search_session_id": "task-duplex:visible",
    }, None)
    recovered = channel.send_response.call_args.kwargs["payload"]
    assert recovered["status"] == "completed"
    files = [entry for entry in recovered["progress_history"] if entry["stage"] == "file"]
    assert len(files) == 1
    assert files[0]["files"] == [FILE]
    assert files[0]["sequence"] > 0


@pytest.mark.asyncio
async def test_nonstreaming_file_only_response_is_delivered():
    client = SimpleNamespace(send_request=AsyncMock(return_value=SimpleNamespace(ok=True, payload={"files": [FILE]})))
    progress = AsyncMock()
    result = await video_search.execute_core_agent(
        client, question="生成文件", query="生成文件", visual_context="", search_session_id="scope", on_progress=progress,
    )
    assert result["answer"]
    assert progress.call_args.args[0]["files"] == [FILE]


@pytest.mark.asyncio
async def test_file_history_uses_native_chat_file_record_in_visible_conversation(monkeypatch):
    handlers = {}
    channel = SimpleNamespace(
        register_method=lambda name, handler, **_: handlers.update({name: handler}), send_response=AsyncMock(),
    )
    records = []
    monkeypatch.setattr(session_history, "_enqueue_history_item", lambda sid, item, **_: records.append((sid, item)))
    monkeypatch.setattr(session_metadata, "update_session_metadata", lambda **_: None)
    video_live.register_video_live_handler(channel, agent_client=None)
    append_handler = handlers.get("video.conversation.append")
    assert append_handler is not None
    await append_handler(None, "append", {
        "session_id": "visible-session", "event_id": "file-event", "kind": "file", "files": [FILE],
        "timestamp": 1_800_000_000,
    }, "transport-session")
    assert channel.send_response.call_args.kwargs["ok"] is True
    assert records[0][0] == "visible-session"
    assert records[0][1]["event_type"] == "chat.file"
    assert records[0][1]["files"] == [FILE]
    assert records[0][1]["channel_id"] == "video_duplex"
    await append_handler(None, "bad", {
        "session_id": "visible-session", "event_id": "invalid", "kind": "file", "files": [{"name": "no-resource"}],
    }, None)
    assert channel.send_response.call_args.kwargs["code"] == "INVALID_FILES"
    assert len(records) == 1


@pytest.mark.asyncio
async def test_native_file_tool_resources_keep_valid_download_credentials_through_bridge(tmp_path, monkeypatch):
    output = tmp_path / "generated.py"
    output.write_text("print(42)\n", encoding="utf-8")
    pushes = []

    async def push(message):
        pushes.append(message)
        return True

    monkeypatch.setattr(send_file_to_user, "send_runtime_push", push)
    monkeypatch.setattr(session_history, "append_history_record", lambda **_: None)
    monkeypatch.setattr(web_file_download.WebFileDownloadManager, "_instance",
                        web_file_download.WebFileDownloadManager(secret="test-only-secret-with-at-least-32-chars"))
    toolkit = send_file_to_user.SendFileToolkit(
        request_id="file-delegation", session_id="video-tool-file-test", channel_id="video_tool",
        project_dir=str(tmp_path), team_workspace_root=str(tmp_path),
    )
    await toolkit.send_file([str(output)])
    assert len(pushes) == 1
    assert pushes[0]["request_id"] == "file-delegation"
    bridged = video_search.core_agent_progress(pushes[0]["payload"])["files"][0]
    verified = web_file_download.validate_file_download_token(bridged["download_token"])
    assert verified is not None
    assert bridged["path"] == str(output)
    assert bridged["size"] == output.stat().st_size
    assert bridged["download_url"] == pushes[0]["payload"]["files"][0]["download_url"]
