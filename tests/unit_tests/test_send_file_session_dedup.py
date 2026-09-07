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

    assert "成功发送" in first
    assert "跳过重复投递" in second
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
    assert "发送文件失败：所有文件均不存在" in result
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
    assert "提交文件失败:" in result
    assert "pipe down" in result
