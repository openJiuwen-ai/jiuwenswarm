import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.agents.harness.common.tools import send_file_to_user as sfu


@pytest.fixture(autouse=True)
def _clear_dedup_registry():
    sfu._SENT_FILE_PATHS_BY_SESSION.clear()
    yield
    sfu._SENT_FILE_PATHS_BY_SESSION.clear()


def test_partition_and_mark_sent_files():
    new_paths, skipped = sfu._partition_sent_files("s1", [r"C:\tmp\a.md", r"C:\tmp\b.md"])
    assert new_paths == [r"C:\tmp\a.md", r"C:\tmp\b.md"]
    assert skipped == []

    sfu._mark_files_sent("s1", [r"C:\tmp\a.md"])
    new_paths, skipped = sfu._partition_sent_files("s1", [r"C:\tmp\a.md", r"C:\tmp\b.md"])
    assert new_paths == [r"C:\tmp\b.md"]
    assert skipped == [r"C:\tmp\a.md"]

    sfu.clear_sent_files_for_session("s1")
    new_paths, skipped = sfu._partition_sent_files("s1", [r"C:\tmp\a.md"])
    assert new_paths == [r"C:\tmp\a.md"]
    assert skipped == []


def test_artifact_id_stable_and_path_normalized():
    """产物标识：同一路径稳定；WSL /mnt/c 与 Windows 盘符视为同一产物；大小写无关。"""
    assert sfu.artifact_id_for_path(r"C:\tmp\a.md") == sfu.artifact_id_for_path(r"c:\TMP\a.md")
    assert sfu.artifact_id_for_path("/mnt/c/tmp/a.md") == sfu.artifact_id_for_path(r"C:\tmp\a.md")
    assert sfu.artifact_id_for_path(r"C:\tmp\a.md") != sfu.artifact_id_for_path(r"C:\tmp\b.md")
    assert sfu.artifact_id_for_path(r"C:\tmp\a.md").startswith("art-")


def test_send_file_payload_carries_artifact_id(tmp_path):
    """推送帧（与历史记录同一份 payload）必须带 artifact_id，供桌面端两处产物展示共用。"""
    file_path = tmp_path / "handoff.md"
    file_path.write_text("hello", encoding="utf-8")

    toolkit = sfu.SendFileToolkit(request_id="r1", session_id="sess-art", channel_id="desktop")
    mock_server = MagicMock()
    mock_server.send_push = AsyncMock()

    with patch(
        "jiuwenswarm.server.agent_ws_server.AgentWebSocketServer.get_instance",
        return_value=mock_server,
    ), patch(
        "jiuwenswarm.server.runtime.session.session_history.append_history_record",
    ):
        result = asyncio.run(toolkit.send_file(str(file_path)))

    assert "Sent" in result
    payload = mock_server.send_push.await_args.args[0]["payload"]
    assert payload["event_type"] == "chat.file"
    entry = payload["files"][0]
    assert entry["artifact_id"] == sfu.artifact_id_for_path(str(file_path))
    assert entry["name"] == "handoff.md"


def test_send_file_skips_duplicate_after_success(tmp_path):
    file_path = tmp_path / "handoff.md"
    file_path.write_text("hello", encoding="utf-8")

    toolkit = sfu.SendFileToolkit(
        request_id="r1",
        session_id="sess-1",
        channel_id="web",
    )
    mock_server = MagicMock()
    mock_server.send_push = AsyncMock()

    with patch(
        "jiuwenswarm.server.agent_ws_server.AgentWebSocketServer.get_instance",
        return_value=mock_server,
    ), patch(
        "jiuwenswarm.server.runtime.session.session_history.append_history_record",
    ):
        first = asyncio.run(toolkit.send_file(str(file_path)))
        second = asyncio.run(toolkit.send_file(str(file_path)))

    assert "Sent" in first
    assert "skipping duplicate delivery" in second
    assert not second.startswith("success=False")
    assert mock_server.send_push.await_count == 1


def test_send_file_missing_all_uses_failure_envelope():
    toolkit = sfu.SendFileToolkit(
        request_id="r1",
        session_id="sess-missing",
        channel_id="desktop",
    )
    mock_server = MagicMock()
    mock_server.send_push = AsyncMock()
    missing = r"C:\Users\paizh\Documents\missing.docx"

    with patch(
        "jiuwenswarm.server.agent_ws_server.AgentWebSocketServer.get_instance",
        return_value=mock_server,
    ), patch(
        "jiuwenswarm.server.runtime.session.session_history.append_history_record",
    ):
        result = asyncio.run(toolkit.send_file(missing))

    assert result.startswith("success=False error='")
    assert "data=None" not in result
    assert "Failed to send files: none of the files exist" in result
    assert "missing.docx" in result
    assert mock_server.send_push.await_count == 0


def test_send_file_push_error_uses_failure_envelope(tmp_path):
    file_path = tmp_path / "handoff.md"
    file_path.write_text("hello", encoding="utf-8")

    toolkit = sfu.SendFileToolkit(
        request_id="r1",
        session_id="sess-push-fail",
        channel_id="desktop",
    )
    mock_server = MagicMock()
    mock_server.send_push = AsyncMock(side_effect=RuntimeError("pipe down"))

    with patch(
        "jiuwenswarm.server.agent_ws_server.AgentWebSocketServer.get_instance",
        return_value=mock_server,
    ), patch(
        "jiuwenswarm.server.runtime.session.session_history.append_history_record",
    ):
        result = asyncio.run(toolkit.send_file(str(file_path)))

    assert result.startswith("success=False error='")
    assert "data=None" not in result
    assert "Failed to submit files:" in result
    assert "pipe down" in result


def test_send_file_success_completes_open_todos_and_pushes_update(tmp_path, monkeypatch):
    file_path = tmp_path / "handoff.md"
    file_path.write_text("hello", encoding="utf-8")
    todo_root = tmp_path / "todo"
    session_dir = todo_root / "sess-deliver"
    session_dir.mkdir(parents=True)
    (session_dir / "todo.json").write_text(
        '[{"id":"gen","content":"生成","activeForm":"生成","status":"in_progress"},'
        '{"id":"deliver","content":"交付","activeForm":"交付","status":"pending"}]',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.todo_snapshot.get_deepagent_todo_dir",
        lambda: todo_root,
    )

    toolkit = sfu.SendFileToolkit(
        request_id="r1",
        session_id="sess-deliver",
        channel_id="desktop",
    )
    mock_server = MagicMock()
    mock_server.send_push = AsyncMock()

    with patch(
        "jiuwenswarm.server.agent_ws_server.AgentWebSocketServer.get_instance",
        return_value=mock_server,
    ), patch(
        "jiuwenswarm.server.runtime.session.session_history.append_history_record",
    ):
        result = asyncio.run(toolkit.send_file(str(file_path)))

    assert "Sent" in result
    assert mock_server.send_push.await_count == 2
    todo_payload = mock_server.send_push.await_args_list[1].args[0]["payload"]
    assert todo_payload["event_type"] == "todo.updated"
    assert {t["id"]: t["status"] for t in todo_payload["todos"]} == {
        "gen": "completed",
        "deliver": "completed",
    }
