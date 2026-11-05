import asyncio
import json
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.agents.harness.common.tools.send_file_to_user import (
    SendFileToolkit,
    _SENT_FILE_PATHS_BY_SESSION,
    _mark_files_sent,
    _partition_sent_files,
    clear_sent_files_for_session,
)


@pytest.fixture(autouse=True)
def _clear_dedup_registry(monkeypatch):
    """Personal-path send_file tests must not enter enterprise OBS branch."""
    _SENT_FILE_PATHS_BY_SESSION.clear()
    monkeypatch.setenv("JIUWENSWARM_EDITION", "personal")
    monkeypatch.delenv("JIUWENSWARM_FILE_DOWNLOAD_VIA_PUSH", raising=False)
    yield
    _SENT_FILE_PATHS_BY_SESSION.clear()


def test_partition_and_mark_sent_files():
    new_paths, skipped = _partition_sent_files("s1", [r"C:\tmp\a.md", r"C:\tmp\b.md"])
    assert new_paths == [r"C:\tmp\a.md", r"C:\tmp\b.md"]
    assert skipped == []

    _mark_files_sent("s1", [r"C:\tmp\a.md"])
    new_paths, skipped = _partition_sent_files("s1", [r"C:\tmp\a.md", r"C:\tmp\b.md"])
    assert new_paths == [r"C:\tmp\b.md"]
    assert skipped == [r"C:\tmp\a.md"]

    clear_sent_files_for_session("s1")
    new_paths, skipped = _partition_sent_files("s1", [r"C:\tmp\a.md"])
    assert new_paths == [r"C:\tmp\a.md"]
    assert skipped == []


def test_send_file_skips_duplicate_after_success(tmp_path, monkeypatch):
    file_path = tmp_path / "handoff.md"
    file_path.write_text("hello", encoding="utf-8")

    toolkit = SendFileToolkit(
        request_id="r1",
        session_id="sess-1",
        channel_id="web",
    )
    mock_server = MagicMock()
    mock_server.send_push = AsyncMock(return_value=1)

    class _FakeAgentWebSocketServer:
        @staticmethod
        def get_instance():
            return mock_server

    monkeypatch.setitem(
        sys.modules,
        "jiuwenswarm.server.agent_ws_server",
        types.SimpleNamespace(AgentWebSocketServer=_FakeAgentWebSocketServer),
    )

    with patch(
        "jiuwenswarm.server.runtime.session.session_history.append_history_record",
    ):
        first = asyncio.run(toolkit.send_file(str(file_path)))
        second = asyncio.run(toolkit.send_file(str(file_path)))

    assert "成功发送" in first
    assert "跳过重复投递" in second
    assert mock_server.send_push.await_count == 1


def test_send_file_force_resends_after_duplicate(tmp_path, monkeypatch):
    file_path = tmp_path / "ai_chip_report.md"
    file_path.write_text("hello", encoding="utf-8")

    toolkit = SendFileToolkit(
        request_id="r-resend",
        session_id="sess-resend",
        channel_id="officeclaw",
    )
    mock_server = MagicMock()
    mock_server.send_push = AsyncMock(return_value=1)

    class _FakeAgentWebSocketServer:
        @staticmethod
        def get_instance():
            return mock_server

    monkeypatch.setitem(
        sys.modules,
        "jiuwenswarm.server.agent_ws_server",
        types.SimpleNamespace(AgentWebSocketServer=_FakeAgentWebSocketServer),
    )

    with patch(
        "jiuwenswarm.server.runtime.session.session_history.append_history_record",
    ):
        first = asyncio.run(toolkit.send_file(str(file_path)))
        second = asyncio.run(toolkit.send_file(str(file_path)))
        third = asyncio.run(toolkit.send_file(str(file_path), force=True))

    assert "成功发送" in first
    assert "跳过重复投递" in second
    assert "成功发送" in third
    assert mock_server.send_push.await_count == 2


def test_duplicate_skip_returns_a_usable_file_reference(tmp_path, monkeypatch):
    file_path = tmp_path / "ai_chip_report.html"
    file_path.write_text("<html></html>", encoding="utf-8")

    toolkit = SendFileToolkit(
        request_id="r-skip",
        session_id="sess-skip",
        channel_id="officeclaw",
    )
    mock_server = MagicMock()
    mock_server.send_push = AsyncMock(return_value=1)

    class _FakeAgentWebSocketServer:
        @staticmethod
        def get_instance():
            return mock_server

    monkeypatch.setitem(
        sys.modules,
        "jiuwenswarm.server.agent_ws_server",
        types.SimpleNamespace(AgentWebSocketServer=_FakeAgentWebSocketServer),
    )

    with patch(
        "jiuwenswarm.server.runtime.session.session_history.append_history_record",
    ), patch(
        "jiuwenswarm.agents.harness.common.tools.web_file_download.build_file_download_info",
        return_value={"download_url": "https://example.test/file", "size": 5},
    ) as build_info:
        asyncio.run(toolkit.send_file(str(file_path)))
        build_info.reset_mock()
        skipped = asyncio.run(toolkit.send_file(str(file_path)))

    assert "跳过重复投递" in skipped
    assert "https://example.test/file" in skipped
    assert build_info.call_count == 1


def test_duplicate_skip_falls_back_to_path_when_link_generation_fails(tmp_path, monkeypatch):
    file_path = tmp_path / "ai_chip_report.md"
    file_path.write_text("hello", encoding="utf-8")

    toolkit = SendFileToolkit(
        request_id="r-skip-fallback",
        session_id="sess-skip-fallback",
        channel_id="officeclaw",
    )
    mock_server = MagicMock()
    mock_server.send_push = AsyncMock(return_value=1)

    class _FakeAgentWebSocketServer:
        @staticmethod
        def get_instance():
            return mock_server

    monkeypatch.setitem(
        sys.modules,
        "jiuwenswarm.server.agent_ws_server",
        types.SimpleNamespace(AgentWebSocketServer=_FakeAgentWebSocketServer),
    )

    with patch(
        "jiuwenswarm.server.runtime.session.session_history.append_history_record",
    ), patch(
        "jiuwenswarm.agents.harness.common.tools.web_file_download.build_file_download_info",
        side_effect=RuntimeError("download token unavailable"),
    ):
        asyncio.run(toolkit.send_file(str(file_path)))
        skipped = asyncio.run(toolkit.send_file(str(file_path)))

    assert "跳过重复投递" in skipped
    assert str(file_path) in skipped
    assert "https://" not in skipped


def test_send_file_fails_when_send_push_delivers_zero(tmp_path, monkeypatch):
    file_path = tmp_path / "report.pptx"
    file_path.write_bytes(b"fake-pptx")

    toolkit = SendFileToolkit(
        request_id="r-fail",
        session_id="sess-fail",
        channel_id="officeclaw",
    )
    mock_server = MagicMock()
    mock_server.send_push = AsyncMock(return_value=0)

    class _FakeAgentWebSocketServer:
        @staticmethod
        def get_instance():
            return mock_server

    monkeypatch.setitem(
        sys.modules,
        "jiuwenswarm.server.agent_ws_server",
        types.SimpleNamespace(AgentWebSocketServer=_FakeAgentWebSocketServer),
    )

    with patch(
        "jiuwenswarm.server.runtime.session.session_history.append_history_record",
    ) as append_history:
        result = asyncio.run(toolkit.send_file(str(file_path)))

    assert "发送文件失败" in result
    assert "成功发送" not in result
    append_history.assert_not_called()
    # 失败不得记入去重表，否则重试会被「已发送」跳过
    new_paths, skipped = _partition_sent_files("sess-fail", [str(file_path)])
    assert new_paths == [str(file_path)]
    assert skipped == []
    assert mock_server.send_push.await_count == 1


def test_send_file_succeeds_when_append_history_raises(tmp_path, monkeypatch):
    # 文件已通过 send_push 送达后，append_history_record 抛异常不得伪装成
    # 「提交文件失败」；且 _mark_files_sent 必须已执行，重试时被去重跳过。
    file_path = tmp_path / "export.csv"
    file_path.write_text("a,b\n1,2", encoding="utf-8")

    toolkit = SendFileToolkit(
        request_id="r-hist",
        session_id="sess-hist",
        channel_id="web",
    )
    mock_server = MagicMock()
    mock_server.send_push = AsyncMock(return_value=1)

    class _FakeAgentWebSocketServer:
        @staticmethod
        def get_instance():
            return mock_server

    monkeypatch.setitem(
        sys.modules,
        "jiuwenswarm.server.agent_ws_server",
        types.SimpleNamespace(AgentWebSocketServer=_FakeAgentWebSocketServer),
    )

    with patch(
        "jiuwenswarm.server.runtime.session.session_history.append_history_record",
        side_effect=RuntimeError("db timeout"),
    ) as append_history:
        result = asyncio.run(toolkit.send_file(str(file_path)))

    assert "成功发送" in result
    assert "提交文件失败" not in result
    append_history.assert_called_once()
    # 去重标记已先于历史写入执行：重试同一文件应被跳过而非重复投递
    new_paths, skipped = _partition_sent_files("sess-hist", [str(file_path)])
    assert new_paths == []
    assert skipped == [str(file_path)]
    assert mock_server.send_push.await_count == 1


def test_all_missing_files_return_non_retryable_contract(tmp_path):
    toolkit = SendFileToolkit(
        request_id="r-missing",
        session_id="sess-missing",
        channel_id="web",
    )
    missing = tmp_path / "ai_chip_report.md"

    result = asyncio.run(toolkit.send_file(str(missing)))

    payload = json.loads(result)
    assert payload["contract"] == "jiuwenswarm.non_retryable.v1"
    assert payload["success"] is False
    assert payload["retryable"] is False
    assert payload["suggested_tool"] == "read_file"
    assert "ai_chip_report.md" in payload["error"]
