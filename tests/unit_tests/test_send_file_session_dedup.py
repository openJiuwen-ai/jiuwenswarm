import asyncio
from unittest.mock import patch

import pytest

from jiuwenswarm.agents.harness.common.tools import send_file_to_user as sfu
from jiuwenswarm.agents.harness.common.tools.web_file_download import (
    build_file_download_info,
    validate_file_download_token,
)


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


def test_download_info_includes_agentos_user_id_in_url(tmp_path):
    file_path = tmp_path / "report.txt"
    file_path.write_text("hello", encoding="utf-8")

    info = build_file_download_info(
        str(file_path), "report.txt", "session-1", user_id="user-1"
    )

    assert "user_id=user-1" in info["download_url"]
    payload = validate_file_download_token(info["download_token"])
    assert payload is not None
    assert "exp" not in payload


def test_send_file_skips_duplicate_after_success(tmp_path):
    file_path = tmp_path / "handoff.md"
    file_path.write_text("hello", encoding="utf-8")

    toolkit = sfu.SendFileToolkit(
        request_id="r1",
        session_id="sess-1",
        channel_id="web",
    )
    pushed: list[dict] = []

    async def _push(message: dict) -> bool:
        pushed.append(message)
        return True

    with patch.object(sfu, "send_runtime_push", _push), patch(
        "jiuwenswarm.server.runtime.session.session_history.append_history_record",
    ):
        first = asyncio.run(toolkit.send_file(str(file_path)))
        second = asyncio.run(toolkit.send_file(str(file_path)))

    assert "成功发送" in first
    assert "最终交付文件已位于当前项目目录" not in first
    assert "跳过重复投递" in second
    assert len(pushed) == 1


def test_history_failure_after_push_does_not_duplicate_delivery(tmp_path):
    file_path = tmp_path / "delivered.md"
    file_path.write_text("hello", encoding="utf-8")
    toolkit = sfu.SendFileToolkit(
        request_id="r-history",
        session_id="sess-history",
        channel_id="web",
    )
    pushed: list[dict] = []

    async def _push(message: dict) -> bool:
        pushed.append(message)
        return True

    with patch.object(sfu, "send_runtime_push", _push), patch(
        "jiuwenswarm.server.runtime.session.session_history.append_history_record",
        side_effect=OSError("history unavailable"),
    ):
        first = asyncio.run(toolkit.send_file(str(file_path)))
        second = asyncio.run(toolkit.send_file(str(file_path)))

    assert "成功发送" in first
    assert "跳过重复投递" in second
    assert len(pushed) == 1


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
    pushed: list[dict] = []

    async def _push(message: dict) -> bool:
        pushed.append(message)
        return True

    with patch.object(sfu, "send_runtime_push", _push), patch(
        "jiuwenswarm.server.runtime.session.session_history.append_history_record",
    ) as append_history:
        result = asyncio.run(toolkit.send_file(str(source)))

    delivered = project_root / "reports" / "poem-gu.txt"
    assert "成功发送" in result
    assert delivered.read_text(encoding="utf-8") == "古诗"
    payload = pushed[0]["payload"]["files"]
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
