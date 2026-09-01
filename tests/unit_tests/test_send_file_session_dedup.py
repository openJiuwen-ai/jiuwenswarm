import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.agents.harness.common.tools import send_file_to_user as sfu
from jiuwenswarm.agents.harness.common.tools.web_file_download import build_file_download_info


@pytest.fixture(autouse=True)
def _clear_dedup_registry():
    sfu._SENT_FILE_PATHS_BY_SESSION.clear()
    sfu._TOOLKITS_BY_SESSION.clear()
    sfu.bind_active_send_file_toolkit(None)
    yield
    sfu._SENT_FILE_PATHS_BY_SESSION.clear()
    sfu._TOOLKITS_BY_SESSION.clear()
    sfu.bind_active_send_file_toolkit(None)


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


def test_download_info_includes_agentos_user_id_in_url(tmp_path):
    file_path = tmp_path / "report.txt"
    file_path.write_text("hello", encoding="utf-8")

    info = build_file_download_info(
        str(file_path), "report.txt", "session-1", user_id="user-1"
    )

    assert "user_id=user-1" in info["download_url"]


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
    assert "最终交付文件已位于当前项目目录" not in first
    assert "跳过重复投递" in second
    assert mock_server.send_push.await_count == 1


def test_send_file_materializes_team_workspace_files_in_project(tmp_path):
    team_root = tmp_path / "team-workspace"
    project_root = tmp_path / "project"
    source = team_root / "reports" / "poem-gu.txt"
    source.parent.mkdir(parents=True)
    source.write_text("古诗", encoding="utf-8")

    toolkit = sfu.SendFileToolkit(
        request_id="r2",
        session_id="sess-2",
        channel_id="web",
        project_dir=str(project_root),
        team_workspace_root=str(team_root),
    )
    mock_server = MagicMock()
    mock_server.send_push = AsyncMock()

    with patch(
        "jiuwenswarm.server.agent_ws_server.AgentWebSocketServer.get_instance",
        return_value=mock_server,
    ), patch(
        "jiuwenswarm.server.runtime.session.session_history.append_history_record",
    ) as append_history:
        result = asyncio.run(toolkit.send_file(str(source)))

    delivered = project_root / "reports" / "poem-gu.txt"
    assert "成功发送" in result
    assert delivered.read_text(encoding="utf-8") == "古诗"
    payload = mock_server.send_push.await_args.args[0]["payload"]["files"]
    assert payload[0]["path"] == str(delivered)
    history_extra = append_history.call_args.kwargs["extra"]
    assert history_extra["files"][0]["path"] == str(delivered)


def test_send_file_does_not_move_non_team_files(tmp_path):
    team_root = tmp_path / "team-workspace"
    project_root = tmp_path / "project"
    source = tmp_path / "downloads" / "existing.txt"
    source.parent.mkdir(parents=True)
    source.write_text("existing", encoding="utf-8")

    toolkit = sfu.SendFileToolkit(
        request_id="r3",
        session_id="sess-3",
        channel_id="web",
        project_dir=str(project_root),
        team_workspace_root=str(team_root),
    )

    assert toolkit._materialize_team_deliverable(str(source)) == str(source)
    assert not project_root.exists()


def test_send_file_does_not_overwrite_different_project_file(tmp_path):
    team_root = tmp_path / "team-workspace"
    project_root = tmp_path / "project"
    source = team_root / "result.txt"
    destination = project_root / "result.txt"
    source.parent.mkdir(parents=True)
    destination.parent.mkdir(parents=True)
    source.write_text("new", encoding="utf-8")
    destination.write_text("user-owned", encoding="utf-8")

    toolkit = sfu.SendFileToolkit(
        request_id="r4",
        session_id="sess-4",
        channel_id="web",
        project_dir=str(project_root),
        team_workspace_root=str(team_root),
    )

    with pytest.raises(FileExistsError):
        toolkit._materialize_team_deliverable(str(source))
    assert destination.read_text(encoding="utf-8") == "user-owned"


def test_send_file_resolves_project_from_session_and_infers_team_root(tmp_path):
    team_root = tmp_path / ".agent_teams" / "writers" / "team-workspace"
    project_root = tmp_path / "project"
    source = team_root / "poem.txt"
    source.parent.mkdir(parents=True)
    source.write_text("poem", encoding="utf-8")
    toolkit = sfu.SendFileToolkit(
        request_id="r5",
        session_id="sess-5",
        channel_id="web",
    )

    with patch(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        return_value={"project_dir": str(project_root)},
    ):
        delivered = toolkit._materialize_team_deliverable(str(source))

    assert delivered == str(project_root / "poem.txt")
    assert (project_root / "poem.txt").read_text(encoding="utf-8") == "poem"


def test_send_file_keeps_worktree_file_in_worktree(tmp_path):
    project_root = tmp_path / "project"
    worktree_root = tmp_path / ".worktrees" / "member-1"
    source = worktree_root / "src" / "feature.py"
    source.parent.mkdir(parents=True)
    source.write_text("feature = True", encoding="utf-8")
    toolkit = sfu.SendFileToolkit(
        request_id="r6",
        session_id="sess-6",
        channel_id="web",
        project_dir=str(project_root),
        team_workspace_root=str(tmp_path / ".agent_teams" / "team" / "team-workspace"),
    )

    assert toolkit._materialize_team_deliverable(str(source)) == str(source)
    assert not project_root.exists()


def test_deliver_file_to_user_uses_bound_toolkit(tmp_path):
    file_path = tmp_path / "generated.png"
    file_path.write_text("img", encoding="utf-8")

    toolkit = sfu.SendFileToolkit(
        request_id="r2",
        session_id="sess-2",
        channel_id="web",
    )
    mock_server = MagicMock()
    mock_server.send_push = AsyncMock()
    sfu.bind_active_send_file_toolkit(toolkit)

    try:
        with patch(
            "jiuwenswarm.server.agent_ws_server.AgentWebSocketServer.get_instance",
            return_value=mock_server,
        ), patch(
            "jiuwenswarm.server.runtime.session.session_history.append_history_record",
        ):
            result = asyncio.run(sfu.deliver_file_to_user(str(file_path)))
    finally:
        sfu.bind_active_send_file_toolkit(None)

    assert "成功发送" in result
    assert mock_server.send_push.await_count == 1


def test_deliver_file_to_user_noop_without_toolkit(tmp_path):
    file_path = tmp_path / "orphan.mp4"
    file_path.write_bytes(b"video")
    sfu.bind_active_send_file_toolkit(None)
    assert asyncio.run(sfu.deliver_file_to_user(str(file_path))) == ""


def test_session_registry_isolates_concurrent_bindings(tmp_path):
    """Session-keyed registry must not let one request overwrite another's toolkit."""
    file_a = tmp_path / "a.png"
    file_a.write_text("a", encoding="utf-8")

    toolkit_a = sfu.SendFileToolkit(request_id="ra", session_id="sess-a", channel_id="web")
    toolkit_b = sfu.SendFileToolkit(request_id="rb", session_id="sess-b", channel_id="web")
    mock_server = MagicMock()
    mock_server.send_push = AsyncMock()

    sfu.bind_active_send_file_toolkit(toolkit_a)
    sfu.bind_active_send_file_toolkit(toolkit_b)

    # ContextVar points at the latest bind, but each session keeps its own entry.
    assert sfu._lookup_toolkit_for_session("sess-a") is toolkit_a
    assert sfu._lookup_toolkit_for_session("sess-b") is toolkit_b

    # Simulate a worker that lost the request ContextVar but knows its session.
    sfu._ACTIVE_SEND_FILE_TOOLKIT.set(None)

    with patch(
        "jiuwenswarm.server.agent_ws_server.AgentWebSocketServer.get_instance",
        return_value=mock_server,
    ), patch(
        "jiuwenswarm.server.runtime.session.session_history.append_history_record",
    ):
        result = asyncio.run(sfu.deliver_file_to_user(str(file_a), session_id="sess-a"))

    assert "成功发送" in result
    assert mock_server.send_push.await_count == 1
    call = mock_server.send_push.await_args
    payload = call.kwargs.get("msg") if call.kwargs else None
    if payload is None and call.args:
        payload = call.args[0] if call.args else None
    if isinstance(payload, dict):
        assert payload.get("session_id") == "sess-a"


def test_clear_active_send_file_toolkit_drops_session_entry():
    toolkit = sfu.SendFileToolkit(request_id="r", session_id="sess-clear", channel_id="web")
    sfu.bind_active_send_file_toolkit(toolkit)
    assert sfu._lookup_toolkit_for_session("sess-clear") is toolkit
    sfu.clear_active_send_file_toolkit(session_id="sess-clear")
    assert sfu._lookup_toolkit_for_session("sess-clear") is None
    assert sfu._ACTIVE_SEND_FILE_TOOLKIT.get() is None
