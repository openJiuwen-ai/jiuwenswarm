import asyncio
import os
from unittest.mock import patch

import pytest

from jiuwenswarm.agents.harness.common.tools import send_file_to_user as sfu
from jiuwenswarm.agents.harness.common.tools.web_file_download import (
    build_file_download_info,
    validate_file_download_token,
)


@pytest.fixture(autouse=True)
def _clear_dedup_registry():
    sfu._SENT_FILE_FINGERPRINTS_BY_SESSION.clear()
    yield
    sfu._SENT_FILE_FINGERPRINTS_BY_SESSION.clear()


def test_normalize_sent_file_path_folds_separators_and_case():
    assert sfu._normalize_sent_file_path(r"C:\tmp\A.md") == sfu._normalize_sent_file_path(
        r"c:/tmp/a.md"
    )


def test_fingerprint_missing_path_returns_none(tmp_path):
    assert sfu._sent_file_fingerprint(str(tmp_path / "missing.md")) is None
    existing = tmp_path / "existing.md"
    existing.write_text("content", encoding="utf-8")
    assert sfu._sent_file_fingerprint(str(existing)) == (
        existing.stat().st_size,
        existing.stat().st_mtime_ns,
    )


def test_partition_and_mark_sent_files(tmp_path):
    first = tmp_path / "a.md"
    second = tmp_path / "b.md"
    first.write_text("a", encoding="utf-8")
    second.write_text("b", encoding="utf-8")
    fingerprints = {
        sfu._normalize_sent_file_path(str(first)): sfu._sent_file_fingerprint(str(first)),
        sfu._normalize_sent_file_path(str(second)): sfu._sent_file_fingerprint(str(second)),
    }

    new_paths, skipped = sfu._partition_sent_files(
        "s1", [str(first), str(second)], fingerprints
    )
    assert new_paths == [str(first), str(second)]
    assert skipped == []

    sfu._mark_files_sent("s1", [str(first)], fingerprints)
    new_paths, skipped = sfu._partition_sent_files(
        "s1", [str(first), str(second)], fingerprints
    )
    assert new_paths == [str(second)]
    assert skipped == [str(first)]

    # The same path twice in one call is still delivered only once.
    new_paths, skipped = sfu._partition_sent_files(
        "s1", [str(second), str(second)], fingerprints
    )
    assert new_paths == [str(second)]
    assert skipped == [str(second)]

    sfu.clear_sent_files_for_session("s1")
    new_paths, skipped = sfu._partition_sent_files("s1", [str(first)], fingerprints)
    assert new_paths == [str(first)]
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


def test_send_file_resends_after_content_modification(tmp_path):
    file_path = tmp_path / "handoff.md"
    file_path.write_text("v1", encoding="utf-8")
    toolkit = sfu.SendFileToolkit(
        request_id="r-mod",
        session_id="sess-mod",
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
        file_path.write_text("v2 with a longer body", encoding="utf-8")
        second = asyncio.run(toolkit.send_file(str(file_path)))

    assert "成功发送" in first
    assert "成功发送" in second
    assert len(pushed) == 2
    assert pushed[1]["payload"]["files"][0]["size"] == file_path.stat().st_size


def test_send_file_resends_when_only_mtime_changes(tmp_path):
    file_path = tmp_path / "same-size.md"
    file_path.write_text("same-size", encoding="utf-8")
    toolkit = sfu.SendFileToolkit(
        request_id="r-mtime",
        session_id="sess-mtime",
        channel_id="web",
    )
    pushed: list[dict] = []

    async def _push(message: dict) -> bool:
        pushed.append(message)
        return True

    with patch.object(sfu, "send_runtime_push", _push), patch(
        "jiuwenswarm.server.runtime.session.session_history.append_history_record",
    ):
        asyncio.run(toolkit.send_file(str(file_path)))
        stat_result = file_path.stat()
        os.utime(
            file_path,
            ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + 1_000_000),
        )
        second = asyncio.run(toolkit.send_file(str(file_path)))

    assert "成功发送" in second
    assert len(pushed) == 2


def test_send_file_mixed_batch_delivers_changed_and_skips_unchanged(tmp_path):
    changed = tmp_path / "changed.md"
    unchanged = tmp_path / "unchanged.md"
    changed.write_text("v1", encoding="utf-8")
    unchanged.write_text("keep", encoding="utf-8")
    toolkit = sfu.SendFileToolkit(
        request_id="r-mixed",
        session_id="sess-mixed",
        channel_id="web",
    )
    pushed: list[dict] = []

    async def _push(message: dict) -> bool:
        pushed.append(message)
        return True

    with patch.object(sfu, "send_runtime_push", _push), patch(
        "jiuwenswarm.server.runtime.session.session_history.append_history_record",
    ):
        asyncio.run(toolkit.send_file([str(changed), str(unchanged)]))
        changed.write_text("v2 with a longer body", encoding="utf-8")
        second = asyncio.run(toolkit.send_file([str(changed), str(unchanged)]))

    assert len(pushed) == 2
    second_paths = [item["path"] for item in pushed[1]["payload"]["files"]]
    assert second_paths == [str(changed)]
    assert "成功发送 1 个文件" in second
    assert "已跳过" in second
    assert str(unchanged) in second


def test_send_file_records_latest_fingerprint(tmp_path):
    file_path = tmp_path / "latest.md"
    file_path.write_text("v1", encoding="utf-8")
    toolkit = sfu.SendFileToolkit(
        request_id="r-latest",
        session_id="sess-latest",
        channel_id="web",
    )
    pushed: list[dict] = []

    async def _push(message: dict) -> bool:
        pushed.append(message)
        return True

    with patch.object(sfu, "send_runtime_push", _push), patch(
        "jiuwenswarm.server.runtime.session.session_history.append_history_record",
    ):
        asyncio.run(toolkit.send_file(str(file_path)))
        file_path.write_text("v2 with a longer body", encoding="utf-8")
        asyncio.run(toolkit.send_file(str(file_path)))
        third = asyncio.run(toolkit.send_file(str(file_path)))

    assert "跳过重复投递" in third
    assert len(pushed) == 2


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
