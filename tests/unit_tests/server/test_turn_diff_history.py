# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""逐轮 Diff 历史查询测试。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jiuwenswarm.server.runtime.session.git_diff_status import (
    DiffFileEntry,
    DiffStatusService,
)
from jiuwenswarm.server.runtime.session.project_git import GitError
from jiuwenswarm.server.utils.diff_service import (
    MAX_DIFF_SIZE_BYTES,
    DiffHistoryExpiredError,
    DiffService,
)


@pytest.fixture(autouse=True)
def _avoid_snapshot_disk_io(monkeypatch):
    monkeypatch.setattr(DiffService, "_load_turn_snapshot", lambda self, session_id, change_set_id: None)
    monkeypatch.setattr(DiffService, "_save_turn_snapshot", lambda self, session_id, turn: None)


@pytest.fixture(autouse=True)
def _default_git_service(monkeypatch):
    service = SimpleNamespace(
        status=lambda project: SimpleNamespace(
            error=None,
            is_git=True,
            repo_root="/proj",
            branch="main",
            head="abc123",
            transient=False,
            is_dirty=False,
        ),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.project_git.get_project_git_service",
        lambda: service,
    )


def _ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


_HISTORY: list[dict] = [
    {"role": "user", "timestamp": 1784542845.0, "content": "first prompt",
     "request_id": "req-001", "id": "req-001:user"},
    {"role": "assistant", "timestamp": 1784542850.0, "content": "response1",
     "request_id": "req-001", "id": "req-001:assistant"},
    {"role": "user", "timestamp": 1784542900.0, "content": "second prompt",
     "request_id": "req-002", "id": "req-002:user"},
    {"role": "assistant", "timestamp": 1784542905.0, "content": "response2",
     "request_id": "req-002", "id": "req-002:assistant"},
]

_FILE_OPS: dict[str, list[dict]] = {
    "/proj/file_a.py": [
        {
            "action": "write",
            "timestamp": _ts(1784542850.0),
            "old_content": "line1\nline2\n",
            "new_content": "line1\nline2\nline3\n",
        },
    ],
    "/proj/file_b.py": [
        {
            "action": "write",
            "timestamp": _ts(1784542905.0),
            "old_content": "old\n",
            "new_content": "new\n",
        },
    ],
}

_PROJECT = SimpleNamespace(
    project_id="proj-1",
    project_dir="/proj",
    work_mode="code",
    name="test-project",
)


def _patch_diff_service():
    ph = patch.object(DiffService, "_read_history", return_value=_HISTORY)
    pa = patch.object(DiffService, "_read_agent_history", return_value=_FILE_OPS)
    pl = patch.object(DiffService, "_load_change_sets", return_value=[])
    ps = patch.object(DiffService, "_save_change_sets", return_value=None)
    return ph, pa, pl, ps


def _patch_diff_service_with_persistence():
    saved: list[dict] = []

    def _load(session_id):
        return list(saved)

    def _save(session_id, change_sets):
        saved.clear()
        saved.extend(change_sets)

    ph = patch.object(DiffService, "_read_history", return_value=_HISTORY)
    pa = patch.object(DiffService, "_read_agent_history", return_value=_FILE_OPS)
    pl = patch.object(DiffService, "_load_change_sets", side_effect=_load)
    ps = patch.object(DiffService, "_save_change_sets", side_effect=_save)
    return ph, pa, pl, ps


def test_get_turn_diff_returns_matching_turn():
    ph, pa, pl, ps = _patch_diff_service()
    with ph, pa, pl, ps:
        service = DiffService()
        turn = service.get_turn_diff("sess-1", turn_index=1, project_dir="/proj")
    assert turn is not None
    assert turn["turnIndex"] == 1
    assert "/proj/file_a.py" in turn["files"]
    assert "change_set_id" in turn
    assert turn["request_id"] == "req-001"
    assert turn["user_message_id"] == "req-001:user"
    assert turn["assistant_message_id"] == "req-001:assistant"
    assert turn["status"] == "completed"


def test_get_turn_diff_returns_none_for_missing_turn():
    ph, pa, pl, ps = _patch_diff_service()
    with ph, pa, pl, ps:
        service = DiffService()
        turn = service.get_turn_diff("sess-1", turn_index=99, project_dir="/proj")
    assert turn is None


def test_get_turn_diff_finds_second_turn():
    ph, pa, pl, ps = _patch_diff_service()
    with ph, pa, pl, ps:
        service = DiffService()
        turn = service.get_turn_diff("sess-1", turn_index=2, project_dir="/proj")
    assert turn is not None
    assert turn["turnIndex"] == 2
    assert "/proj/file_b.py" in turn["files"]
    assert turn["request_id"] == "req-002"


def _patch_turn_diff(file_ops: dict) -> tuple:
    ph = patch.object(DiffService, "_read_history", return_value=_HISTORY)
    pa = patch.object(DiffService, "_read_agent_history", return_value=file_ops)
    pl = patch.object(DiffService, "_load_change_sets", return_value=[])
    ps = patch.object(DiffService, "_save_change_sets", return_value=None)
    return ph, pa, pl, ps


def test_get_turn_diff_large_file_marked_large():
    """超过 1MB 的 turn 改动标记 isLargeFile、不返回内容（与工作区 diff 对齐）。"""
    big_content = "".join(f"line {i}: " + "x" * 250 + "\n" for i in range(5000))
    assert len(big_content.encode("utf-8")) > MAX_DIFF_SIZE_BYTES
    file_ops = {
        "/proj/big.py": [{
            "action": "write",
            "timestamp": _ts(1784542850.0),
            "old_content": None,
            "new_content": big_content,
        }],
    }
    ph, pa, pl, ps = _patch_turn_diff(file_ops)
    with ph, pa, pl, ps:
        turn = DiffService().get_turn_diff("sess-1", turn_index=1, project_dir="/proj")
    assert turn is not None
    info = turn["files"]["/proj/big.py"]
    assert info["isLargeFile"] is True
    assert info["hunks"] == []
    assert info["isTruncated"] is False
    assert info["linesAdded"] == 5000
    assert info["linesRemoved"] == 0


def test_get_turn_diff_large_rewrite_marked_large():
    """实际 diff 超过 1MB 的大文件改写标记为大文件预览。"""
    old_content = "".join(f"old {i}: " + "x" * 250 + "\n" for i in range(5000))
    new_content = "".join(f"new {i}: " + "y" * 250 + "\n" for i in range(5000))
    assert len(old_content) > MAX_DIFF_SIZE_BYTES
    assert len(new_content) > MAX_DIFF_SIZE_BYTES
    file_ops = {
        "/proj/rewrite.py": [{
            "action": "write",
            "timestamp": _ts(1784542850.0),
            "old_content": old_content,
            "new_content": new_content,
        }],
    }
    ph, pa, pl, ps = _patch_turn_diff(file_ops)
    with ph, pa, pl, ps:
        turn = DiffService().get_turn_diff("sess-1", turn_index=1, project_dir="/proj")
    assert turn is not None
    info = turn["files"]["/proj/rewrite.py"]
    assert info["isLargeFile"] is True
    assert info["hunks"] == []
    assert info["linesAdded"] == 5000
    assert info["linesRemoved"] == 5000


def test_get_turn_diff_large_file_small_edit_shows_actual_diff():
    """大文件仅修改一行时，按实际 patch 大小返回预览与行数统计。"""
    old_lines = [f"line {i}: " + "x" * 250 + "\n" for i in range(5000)]
    new_lines = old_lines.copy()
    new_lines[2500] = "line 2500: " + "y" * 250 + "\n"
    old_content = "".join(old_lines)
    new_content = "".join(new_lines)
    assert len(old_content.encode("utf-8")) > MAX_DIFF_SIZE_BYTES

    file_ops = {
        "/proj/small-edit.py": [{
            "action": "write",
            "timestamp": _ts(1784542850.0),
            "old_content": old_content,
            "new_content": new_content,
        }],
    }
    ph, pa, pl, ps = _patch_turn_diff(file_ops)
    with ph, pa, pl, ps:
        turn = DiffService().get_turn_diff("sess-1", turn_index=1, project_dir="/proj")

    assert turn is not None
    info = turn["files"]["/proj/small-edit.py"]
    assert info["isLargeFile"] is False
    assert info["linesAdded"] == 1
    assert info["linesRemoved"] == 1
    assert [line for hunk in info["hunks"] for line in hunk["lines"] if line.startswith("+")] == [
        "+line 2500: " + "y" * 250
    ]


def test_get_turn_diff_file_under_1mb_shows_full():
    """1MB 以内的 turn 改动整段返回，不做 400 行截断。"""
    content = "".join(f"line {i}: " + "y" * 50 + "\n" for i in range(500))
    assert len(content.encode("utf-8")) < MAX_DIFF_SIZE_BYTES
    file_ops = {
        "/proj/small.py": [{
            "action": "write",
            "timestamp": _ts(1784542850.0),
            "old_content": None,
            "new_content": content,
        }],
    }
    ph, pa, pl, ps = _patch_turn_diff(file_ops)
    with ph, pa, pl, ps:
        turn = DiffService().get_turn_diff("sess-1", turn_index=1, project_dir="/proj")
    assert turn is not None
    info = turn["files"]["/proj/small.py"]
    assert info["isLargeFile"] is False
    assert info["isTruncated"] is False
    assert len(info["hunks"]) == 1
    assert len(info["hunks"][0]["lines"]) == 500
    assert info["linesAdded"] == 500


def test_legacy_snapshot_normalizes_oversized_preview():
    """旧快照也按实际 diff 内容大小限制预览。"""
    snapshot = {
        "turnIndex": 1,
        "files": {
            "/proj/legacy.py": {
                "isLargeFile": False,
                "linesAdded": 1,
                "linesRemoved": 0,
                "hunks": [{"lines": ["+" + "x" * MAX_DIFF_SIZE_BYTES]}],
            }
        },
    }

    DiffService._normalize_snapshot_diff_sizes(snapshot)

    entry = snapshot["files"]["/proj/legacy.py"]
    assert entry["isLargeFile"] is True
    assert entry["hunks"] == []
    assert entry["linesAdded"] == 1


def test_get_turn_diffs_reads_extra_history_roots(tmp_path, monkeypatch):
    """Team 模式的成员 workspace file_ops 也应纳入 last_turn diff。"""
    agent_ws = tmp_path / "agent-ws"
    user_ws = tmp_path / "user-ws"
    project_root = tmp_path / "project"
    workspaces_root = tmp_path / "team-home" / "workspaces"
    team_root = workspaces_root / "worker_workspace"
    team_hist = team_root / ".agent_history"
    team_hist.mkdir(parents=True)
    target_file = project_root / "team_file.py"
    session_file = tmp_path / ".agent_teams" / "unit-team" / "sessions" / "sess-1" / "state.json"
    (team_hist / "file_ops_jiuwen_team_unit_worker_sess-1.json").write_text(
        json.dumps(
            {
                str(target_file): [
                    {
                        "action": "write",
                        "timestamp": _ts(1784542850.0),
                        "old_content": "old\n",
                        "new_content": "old\nnew\n",
                    },
                ],
                str(session_file): [
                    {
                        "action": "write",
                        "timestamp": _ts(1784542850.0),
                        "old_content": "",
                        "new_content": "internal scratch\n",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_agent_workspace_dir",
        lambda: agent_ws,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_user_workspace_dir",
        lambda: user_ws,
    )
    ph = patch.object(DiffService, "_read_history", return_value=_HISTORY)
    pl = patch.object(DiffService, "_load_change_sets", return_value=[])
    ps = patch.object(DiffService, "_save_change_sets", return_value=None)

    with ph, pl, ps:
        service = DiffService()
        turns = service.get_turn_diffs(
            "sess-1",
            str(project_root),
            extra_history_roots=[str(workspaces_root)],
        )

    assert len(turns) == 1
    expected_path = str(target_file.resolve())
    session_path = str(session_file.resolve())
    assert expected_path in turns[0]["files"]
    assert session_path in turns[0]["files"]
    assert turns[0]["files"][expected_path]["linesAdded"] == 1


def test_get_turn_diffs_keeps_default_team_workspace_deliverables(tmp_path, monkeypatch):
    """默认 .agent_teams/team-workspace 下的 file_ops 条目都应进入 last-turn。"""
    agent_ws = tmp_path / "agent-ws"
    user_ws = tmp_path / "user-ws"
    project_root = tmp_path / "project"
    team_root = tmp_path / ".agent_teams" / "unit-team" / "team-workspace"
    team_hist = team_root / ".agent_history"
    team_hist.mkdir(parents=True)
    deliverable = team_root / "poem-tang.md"
    bookkeeping = team_root / ".jiuwen" / "state.json"
    (team_hist / "file_ops_leader_sess-1.json").write_text(
        json.dumps(
            {
                str(deliverable): [
                    {
                        "action": "write",
                        "timestamp": _ts(1784542850.0),
                        "old_content": None,
                        "new_content": "spring rain\n",
                    },
                ],
                str(bookkeeping): [
                    {
                        "action": "write",
                        "timestamp": _ts(1784542850.0),
                        "old_content": None,
                        "new_content": "{}\n",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_agent_workspace_dir",
        lambda: agent_ws,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_user_workspace_dir",
        lambda: user_ws,
    )
    ph = patch.object(DiffService, "_read_history", return_value=_HISTORY)
    pl = patch.object(DiffService, "_load_change_sets", return_value=[])
    ps = patch.object(DiffService, "_save_change_sets", return_value=None)

    with ph, pl, ps:
        service = DiffService()
        turns = service.get_turn_diffs(
            "sess-1",
            str(project_root),
            extra_history_roots=[str(team_root)],
        )

    expected_path = str(deliverable.resolve())
    bookkeeping_path = str(bookkeeping.resolve())
    assert len(turns) == 1
    assert expected_path in turns[0]["files"]
    assert bookkeeping_path in turns[0]["files"]
    assert turns[0]["files"][expected_path]["isNewFile"] is True


def test_get_turn_diffs_maps_project_worktree_file_ops_to_repo_root(tmp_path, monkeypatch):
    """成员在 project/.worktrees 中写入的 file_ops 应按主项目路径统计。"""
    agent_ws = tmp_path / "agent-ws"
    user_ws = tmp_path / "user-ws"
    project_root = tmp_path / "project"
    worktree_root = project_root / ".worktrees" / "worker"
    worktree_hist = worktree_root / ".agent_history"
    worktree_hist.mkdir(parents=True)
    worktree_file = worktree_root / "src" / "feature.py"
    canonical_file = project_root / "src" / "feature.py"
    (worktree_hist / "file_ops_worker_sess-1.json").write_text(
        json.dumps(
            {
                str(worktree_file): [
                    {
                        "action": "write",
                        "timestamp": _ts(1784542850.0),
                        "old_content": "old\n",
                        "new_content": "old\nnew\n",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_agent_workspace_dir",
        lambda: agent_ws,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_user_workspace_dir",
        lambda: user_ws,
    )
    monkeypatch.setattr(
        DiffService,
        "_get_git_common_worktree_root",
        staticmethod(lambda root: project_root.resolve() if root.resolve() == worktree_root.resolve() else None),
    )
    ph = patch.object(DiffService, "_read_history", return_value=_HISTORY)
    pl = patch.object(DiffService, "_load_change_sets", return_value=[])
    ps = patch.object(DiffService, "_save_change_sets", return_value=None)

    with ph, pl, ps:
        service = DiffService()
        turns = service.get_turn_diffs("sess-1", str(project_root))

    assert len(turns) == 1
    expected_path = str(canonical_file.resolve())
    worktree_path = str(worktree_file.resolve())
    assert expected_path in turns[0]["files"]
    assert worktree_path not in turns[0]["files"]
    assert turns[0]["files"][expected_path]["linesAdded"] == 1


def test_get_turn_diffs_keeps_nearby_distinct_member_edits(tmp_path, monkeypatch):
    """不同成员几乎同时改同一文件时,内容不同不应被去重吞掉。"""
    agent_ws = tmp_path / "agent-ws"
    user_ws = tmp_path / "user-ws"
    project_root = tmp_path / "project"
    worktrees_root = project_root / ".worktrees"
    first_root = worktrees_root / "first"
    second_root = worktrees_root / "second"
    (first_root / ".agent_history").mkdir(parents=True)
    (second_root / ".agent_history").mkdir(parents=True)
    canonical_file = project_root / "src" / "feature.py"
    first_file = first_root / "src" / "feature.py"
    second_file = second_root / "src" / "feature.py"
    entry_ts = _ts(1784542850.0)
    (first_root / ".agent_history" / "file_ops_first_sess-1.json").write_text(
        json.dumps({
            str(first_file): [{
                "action": "write",
                "timestamp": entry_ts,
                "old_content": "base\n",
                "new_content": "base\nfirst\n",
            }],
        }),
        encoding="utf-8",
    )
    (second_root / ".agent_history" / "file_ops_second_sess-1.json").write_text(
        json.dumps({
            str(second_file): [{
                "action": "write",
                "timestamp": entry_ts,
                "old_content": "base\n",
                "new_content": "base\nsecond\n",
            }],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_agent_workspace_dir",
        lambda: agent_ws,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_user_workspace_dir",
        lambda: user_ws,
    )

    def _fake_common_root(root):
        resolved = root.resolve()
        if resolved in {first_root.resolve(), second_root.resolve()}:
            return project_root.resolve()
        return None

    monkeypatch.setattr(
        DiffService,
        "_get_git_common_worktree_root",
        staticmethod(_fake_common_root),
    )
    ph = patch.object(DiffService, "_read_history", return_value=_HISTORY)
    pl = patch.object(DiffService, "_load_change_sets", return_value=[])
    ps = patch.object(DiffService, "_save_change_sets", return_value=None)

    with ph, pl, ps:
        service = DiffService()
        turns = service.get_turn_diffs("sess-1", str(project_root))

    assert len(turns) == 1
    expected_path = str(canonical_file.resolve())
    assert list(turns[0]["files"]) == [expected_path]
    assert turns[0]["files"][expected_path]["linesAdded"] == 2


def test_get_turn_diffs_keeps_distinct_project_and_worktree_edits(tmp_path, monkeypatch):
    """project 与 worktree 同改 canonical 文件时,低优先级来源不应整文件跳过。"""
    agent_ws = tmp_path / "agent-ws"
    user_ws = tmp_path / "user-ws"
    project_root = tmp_path / "project"
    project_hist = project_root / ".agent_history"
    worktree_root = project_root / ".worktrees" / "worker"
    worktree_hist = worktree_root / ".agent_history"
    project_hist.mkdir(parents=True)
    worktree_hist.mkdir(parents=True)
    canonical_file = project_root / "src" / "feature.py"
    worktree_file = worktree_root / "src" / "feature.py"

    (project_hist / "file_ops_project_sess-1.json").write_text(
        json.dumps({
            str(canonical_file): [{
                "action": "write",
                "timestamp": _ts(1784542850.0),
                "old_content": "base\n",
                "new_content": "base\nproject\n",
            }],
        }),
        encoding="utf-8",
    )
    (worktree_hist / "file_ops_worker_sess-1.json").write_text(
        json.dumps({
            str(worktree_file): [{
                "action": "write",
                "timestamp": _ts(1784542852.0),
                "old_content": "base\nproject\n",
                "new_content": "base\nproject\nworker\n",
            }],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_agent_workspace_dir",
        lambda: agent_ws,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_user_workspace_dir",
        lambda: user_ws,
    )
    monkeypatch.setattr(
        DiffService,
        "_get_git_common_worktree_root",
        staticmethod(lambda root: project_root.resolve() if root.resolve() == worktree_root.resolve() else None),
    )
    ph = patch.object(DiffService, "_read_history", return_value=_HISTORY)
    pl = patch.object(DiffService, "_load_change_sets", return_value=[])
    ps = patch.object(DiffService, "_save_change_sets", return_value=None)

    with ph, pl, ps:
        service = DiffService()
        turns = service.get_turn_diffs("sess-1", str(project_root))

    expected_path = str(canonical_file.resolve())
    assert len(turns) == 1
    assert list(turns[0]["files"]) == [expected_path]
    assert turns[0]["files"][expected_path]["linesAdded"] == 2


def test_get_turn_diffs_merges_case_variant_paths(tmp_path, monkeypatch):
    """Windows 上同一路径大小写不同也应归并为同一文件历史。"""
    agent_ws = tmp_path / "agent-ws"
    user_ws = tmp_path / "user-ws"
    project_root = tmp_path / "project"
    project_hist = project_root / ".agent_history"
    project_hist.mkdir(parents=True)
    target_file = project_root / "src" / "feature.py"
    variant_path = str(target_file).upper()

    (project_hist / "file_ops_project_sess-1.json").write_text(
        json.dumps({
            str(target_file): [{
                "action": "write",
                "timestamp": _ts(1784542850.0),
                "old_content": "base\n",
                "new_content": "base\nlower\n",
            }],
            variant_path: [{
                "action": "write",
                "timestamp": _ts(1784542852.0),
                "old_content": "base\nlower\n",
                "new_content": "base\nlower\nupper\n",
            }],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_agent_workspace_dir",
        lambda: agent_ws,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_user_workspace_dir",
        lambda: user_ws,
    )
    ph = patch.object(DiffService, "_read_history", return_value=_HISTORY)
    pl = patch.object(DiffService, "_load_change_sets", return_value=[])
    ps = patch.object(DiffService, "_save_change_sets", return_value=None)

    with ph, pl, ps:
        service = DiffService()
        turns = service.get_turn_diffs("sess-1", str(project_root))

    assert len(turns) == 1
    assert len(turns[0]["files"]) == 1
    entry = next(iter(turns[0]["files"].values()))
    assert entry["linesAdded"] == 2


def test_get_turn_diffs_reads_member_workspace_worktree_file_ops(tmp_path, monkeypatch):
    """显式 member workspace 下的 .worktrees 也应纳入 last_turn。"""
    agent_ws = tmp_path / "agent-ws"
    user_ws = tmp_path / "user-ws"
    project_root = tmp_path / "project"
    member_root = tmp_path / "member-workspace"
    worktree_root = member_root / ".worktrees" / "worker"
    (worktree_root / ".agent_history").mkdir(parents=True)
    worktree_file = worktree_root / "src" / "feature.py"
    canonical_file = project_root / "src" / "feature.py"
    (worktree_root / ".agent_history" / "file_ops_worker_sess-1.json").write_text(
        json.dumps({
            str(worktree_file): [{
                "action": "write",
                "timestamp": _ts(1784542850.0),
                "old_content": "old\n",
                "new_content": "old\nnew\n",
            }],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_agent_workspace_dir",
        lambda: agent_ws,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_user_workspace_dir",
        lambda: user_ws,
    )
    monkeypatch.setattr(
        DiffService,
        "_get_git_common_worktree_root",
        staticmethod(lambda root: project_root.resolve() if root.resolve() == worktree_root.resolve() else None),
    )
    ph = patch.object(DiffService, "_read_history", return_value=_HISTORY)
    pl = patch.object(DiffService, "_load_change_sets", return_value=[])
    ps = patch.object(DiffService, "_save_change_sets", return_value=None)

    with ph, pl, ps:
        service = DiffService()
        turns = service.get_turn_diffs(
            "sess-1",
            str(project_root),
            extra_history_roots=[str(member_root)],
        )

    expected_path = str(canonical_file.resolve())
    assert len(turns) == 1
    assert expected_path in turns[0]["files"]
    assert turns[0]["files"][expected_path]["linesAdded"] == 1


def test_get_turn_diffs_does_not_scan_project_child_history_dirs(tmp_path, monkeypatch):
    """普通 project_dir 不应扫描一级子目录中的 .agent_history。"""
    agent_ws = tmp_path / "agent-ws"
    user_ws = tmp_path / "user-ws"
    project_root = tmp_path / "project"
    project_hist = project_root / ".agent_history"
    nested_hist = project_root / "nested" / ".agent_history"
    project_hist.mkdir(parents=True)
    nested_hist.mkdir(parents=True)
    project_file = project_root / "kept.py"
    nested_file = project_root / "nested" / "leaked.py"
    (project_hist / "file_ops_agent_sess-1.json").write_text(
        json.dumps(
            {
                str(project_file): [
                    {
                        "action": "write",
                        "timestamp": _ts(1784542850.0),
                        "old_content": "old\n",
                        "new_content": "old\nnew\n",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (nested_hist / "file_ops_agent_sess-1.json").write_text(
        json.dumps(
            {
                str(nested_file): [
                    {
                        "action": "write",
                        "timestamp": _ts(1784542850.0),
                        "old_content": "old\n",
                        "new_content": "old\nnew\n",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_agent_workspace_dir",
        lambda: agent_ws,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_user_workspace_dir",
        lambda: user_ws,
    )
    ph = patch.object(DiffService, "_read_history", return_value=_HISTORY)
    pl = patch.object(DiffService, "_load_change_sets", return_value=[])
    ps = patch.object(DiffService, "_save_change_sets", return_value=None)

    with ph, pl, ps:
        service = DiffService()
        turns = service.get_turn_diffs("sess-1", str(project_root))

    assert len(turns) == 1
    assert str(project_file.resolve()) in turns[0]["files"]
    assert str(nested_file.resolve()) not in turns[0]["files"]


def test_get_turn_diff_by_change_set_id():
    ph, pa, pl, ps = _patch_diff_service_with_persistence()
    with ph, pa, pl, ps:
        service = DiffService()
        turns = service.get_turn_diffs("sess-1", "/proj")
        cs_id = turns[-1]["change_set_id"]
        turn = service.get_turn_diff(
            "sess-1", change_set_id=cs_id, project_dir="/proj",
        )
    assert turn is not None
    assert turn["turnIndex"] == 1
    assert turn["change_set_id"] == cs_id


def test_get_turn_diff_change_set_id_not_found():
    ph, pa, pl, ps = _patch_diff_service()
    with ph, pa, pl, ps:
        service = DiffService()
        turn = service.get_turn_diff(
            "sess-1", change_set_id="cs_nonexistent", project_dir="/proj",
        )
    assert turn is None


def test_get_turn_diff_neither_specified():
    ph, pa, pl, ps = _patch_diff_service()
    with ph, pa, pl, ps:
        service = DiffService()
        turn = service.get_turn_diff("sess-1", project_dir="/proj")
    assert turn is None


def test_change_set_id_format():
    ph, pa, pl, ps = _patch_diff_service()
    with ph, pa, pl, ps:
        service = DiffService()
        turn = service.get_turn_diff("sess-1", turn_index=1, project_dir="/proj")
    assert turn is not None
    cs_id = turn["change_set_id"]
    assert cs_id.startswith("cs_sess-1_1_")
    suffix = cs_id.split("_", 3)[-1]
    assert len(suffix) == 8
    int(suffix, 16)


def test_change_set_id_is_stable():
    ph, pa, pl, ps = _patch_diff_service_with_persistence()
    with ph, pa, pl, ps:
        service = DiffService()
        turn1 = service.get_turn_diff("sess-1", turn_index=1, project_dir="/proj")
        assert turn1 is not None
        cs_id_1 = turn1["change_set_id"]
        turn2 = service.get_turn_diff("sess-1", turn_index=1, project_dir="/proj")
        assert turn2 is not None
        assert turn2["change_set_id"] == cs_id_1


def test_change_set_id_is_stable_without_request_id():
    legacy_history = [
        {"role": "user", "timestamp": 1784542845.0, "content": "legacy prompt"},
        {"role": "assistant", "timestamp": 1784542850.0, "content": "response"},
    ]
    saved: list[dict] = []

    def _load(session_id):
        return list(saved)

    def _save(session_id, change_sets):
        saved.clear()
        saved.extend(change_sets)

    ph = patch.object(DiffService, "_read_history", return_value=legacy_history)
    pa = patch.object(DiffService, "_read_agent_history", return_value=_FILE_OPS)
    pl = patch.object(DiffService, "_load_change_sets", side_effect=_load)
    ps = patch.object(DiffService, "_save_change_sets", side_effect=_save)
    with ph, pa, pl, ps:
        service = DiffService()
        turn1 = service.get_turn_diff("legacy", turn_index=1, project_dir="/proj")
        assert turn1 is not None
        cs_id = turn1["change_set_id"]
        turn2 = service.get_turn_diff("legacy", turn_index=1, project_dir="/proj")
    assert turn2 is not None
    assert turn2["change_set_id"] == cs_id


def test_change_set_id_new_after_rewind():
    saved: list[dict] = []
    history_holder: list[list[dict]] = [list(_HISTORY)]

    def _load(session_id):
        return list(saved)

    def _save(session_id, change_sets):
        saved.clear()
        saved.extend(change_sets)

    def _read_history(session_id):
        return history_holder[0]

    ph = patch.object(DiffService, "_read_history", side_effect=_read_history)
    pa = patch.object(DiffService, "_read_agent_history", return_value=_FILE_OPS)
    pl = patch.object(DiffService, "_load_change_sets", side_effect=_load)
    ps = patch.object(DiffService, "_save_change_sets", side_effect=_save)
    with ph, pa, pl, ps:
        service = DiffService()
        turn1 = service.get_turn_diff("sess-1", turn_index=1, project_dir="/proj")
        assert turn1 is not None
        old_cs_id = turn1["change_set_id"]
        assert turn1["request_id"] == "req-001"

        history_holder[0] = [
            {"role": "user", "timestamp": 1784542846.0, "content": "rewritten prompt",
             "request_id": "req-new", "id": "req-new:user"},
            {"role": "assistant", "timestamp": 1784542850.0, "content": "response",
             "request_id": "req-new", "id": "req-new:assistant"},
            {"role": "user", "timestamp": 1784542900.0, "content": "second prompt",
             "request_id": "req-002", "id": "req-002:user"},
            {"role": "assistant", "timestamp": 1784542905.0, "content": "response2",
             "request_id": "req-002", "id": "req-002:assistant"},
        ]

        turn2 = service.get_turn_diff("sess-1", turn_index=1, project_dir="/proj")
        assert turn2 is not None
        new_cs_id = turn2["change_set_id"]

    assert old_cs_id != new_cs_id
    assert turn2["request_id"] == "req-new"
    assert turn2["user_message_id"] == "req-new:user"


def test_mark_turn_discarded_preserves_snapshot():
    saved: list[dict] = []
    snapshots: dict[str, dict] = {}

    def _load(session_id):
        return list(saved)

    def _save(session_id, change_sets):
        saved.clear()
        saved.extend(change_sets)

    def _load_snapshot(self, session_id, change_set_id):
        return snapshots.get(change_set_id)

    def _save_snapshot(self, session_id, turn):
        snapshots[turn["change_set_id"]] = dict(turn)

    ph = patch.object(DiffService, "_read_history", return_value=_HISTORY)
    pa = patch.object(DiffService, "_read_agent_history", return_value=_FILE_OPS)
    pl = patch.object(DiffService, "_load_change_sets", side_effect=_load)
    ps = patch.object(DiffService, "_save_change_sets", side_effect=_save)
    pls = patch.object(DiffService, "_load_turn_snapshot", _load_snapshot)
    pss = patch.object(DiffService, "_save_turn_snapshot", _save_snapshot)
    with ph, pa, pl, ps, pls, pss:
        service = DiffService()
        cs_id = service.mark_turn_discarded("sess-1", 1, project_dir="/proj")
        assert cs_id is not None
        turn = service.get_turn_diff("sess-1", change_set_id=cs_id, project_dir="/proj")
    assert turn is not None
    assert turn["status"] == "discarded"
    assert "/proj/file_a.py" in turn["files"]


def test_truncate_file_ops_reads_extra_history_roots(tmp_path, monkeypatch):
    """撤销本轮修改时也应截断 team 额外目录中的 session-specific file_ops。"""
    agent_ws = tmp_path / "agent-ws"
    user_ws = tmp_path / "user-ws"
    workspaces_root = tmp_path / "team-home" / "workspaces"
    team_root = workspaces_root / "worker_workspace"
    team_hist = team_root / ".agent_history"
    team_hist.mkdir(parents=True)
    history_file = team_hist / "file_ops_jiuwen_team_unit_worker_sess-1.json"
    history_file.write_text(
        json.dumps(
            {
                str(team_root / "team_file.py"): [
                    {
                        "action": "write",
                        "timestamp": _ts(1784542840.0),
                        "old_content": "before\n",
                        "new_content": "middle\n",
                    },
                    {
                        "action": "write",
                        "timestamp": _ts(1784542850.0),
                        "old_content": "middle\n",
                        "new_content": "after\n",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_agent_workspace_dir",
        lambda: agent_ws,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_user_workspace_dir",
        lambda: user_ws,
    )

    service = DiffService()
    service.truncate_file_ops_by_timestamp(
        "sess-1",
        1784542850.0,
        project_dir="/proj",
        extra_history_roots=[str(workspaces_root)],
    )

    data = json.loads(history_file.read_text(encoding="utf-8"))
    entries = next(iter(data.values()))
    assert len(entries) == 1
    assert entries[0]["new_content"] == "middle\n"


def test_truncate_file_ops_reads_worktree_history_roots(tmp_path, monkeypatch):
    """撤销本轮修改时也应截断 worktree 容器中的 session-specific file_ops。"""
    agent_ws = tmp_path / "agent-ws"
    user_ws = tmp_path / "user-ws"
    project_root = tmp_path / "project"
    worktree_root = project_root / ".worktrees" / "worker"
    worktree_hist = worktree_root / ".agent_history"
    worktree_hist.mkdir(parents=True)
    history_file = worktree_hist / "file_ops_worker_sess-1.json"
    history_file.write_text(
        json.dumps(
            {
                str(worktree_root / "src" / "feature.py"): [
                    {
                        "action": "write",
                        "timestamp": _ts(1784542855.0),
                        "old_content": "old",
                        "new_content": "before",
                    },
                    {
                        "action": "write",
                        "timestamp": _ts(1784542865.0),
                        "old_content": "before",
                        "new_content": "after",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_agent_workspace_dir",
        lambda: agent_ws,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_user_workspace_dir",
        lambda: user_ws,
    )

    service = DiffService()
    service.truncate_file_ops_by_timestamp(
        "sess-1",
        1784542860.0,
        project_dir=str(project_root),
    )

    data = json.loads(history_file.read_text(encoding="utf-8"))
    remaining = data[str(worktree_root / "src" / "feature.py")]
    assert len(remaining) == 1
    assert remaining[0]["timestamp"] == _ts(1784542855.0)


def test_turn_diff_list_returns_summaries_with_files_without_hunks():
    ph, pa, pl, ps = _patch_diff_service()
    with ph, pa, pl, ps:
        result = DiffStatusService.get_turn_diff_list(
            project=_PROJECT, session_id="sess-1", limit=50,
        )
    assert result["project_id"] == "proj-1"
    assert result["session_id"] == "sess-1"
    assert result["repo_root"] == "/proj"
    assert result["branch"] == "main"
    assert result["base_head"] == "abc123"
    assert result["total"] == 2
    assert result["limit"] == 50
    assert result["cursor"] == 0
    assert result["next_cursor"] == 2
    assert result["has_more"] is False
    assert result["turns"][0]["turn_index"] == 2
    assert result["turns"][1]["turn_index"] == 1
    for summary in result["turns"]:
        assert "files" in summary
        assert "kind" in summary
        assert "timestamp" in summary
        assert "user_prompt_preview" in summary
        assert "stats" in summary
        for file_entry in summary["files"].values():
            assert file_entry["hunks"] == []
            assert "change_type" in file_entry
            assert "is_deleted_file" in file_entry
    assert "file_b.py" in result["turns"][0]["files"]
    assert "file_a.py" in result["turns"][1]["files"]


def test_turn_diff_list_includes_change_set_metadata():
    ph, pa, pl, ps = _patch_diff_service()
    with ph, pa, pl, ps:
        result = DiffStatusService.get_turn_diff_list(
            project=_PROJECT, session_id="sess-1", limit=50,
        )
    for summary in result["turns"]:
        assert "change_set_id" in summary
        assert summary["change_set_id"].startswith("cs_sess-1_")
        assert "request_id" in summary
        assert "assistant_message_id" in summary
        assert "user_message_id" in summary
        assert summary["status"] == "completed"
    assert result["turns"][0]["request_id"] == "req-002"
    assert result["turns"][0]["user_message_id"] == "req-002:user"
    assert result["turns"][1]["request_id"] == "req-001"
    assert result["turns"][1]["assistant_message_id"] == "req-001:assistant"


def test_get_session_extra_history_roots_infers_team_workspaces(tmp_path):
    with (
        patch(
            "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
            return_value={
                "team_name": "unit-team",
                "team_file_monitor_roots": [
                    str(tmp_path / ".agent_teams" / "unit-team" / "team-workspace"),
                ],
            },
        ),
        patch(
            "openjiuwen.agent_teams.paths.team_home",
            return_value=tmp_path / "team-home",
        ),
    ):
        from jiuwenswarm.server.runtime.session.git_diff_status import (
            get_session_extra_history_roots,
        )

        roots = get_session_extra_history_roots("sess-1")

    assert str(tmp_path / ".agent_teams" / "unit-team" / "team-workspace") in roots
    assert str(tmp_path / ".agent_teams" / "unit-team" / "workspaces") in roots
    assert str(tmp_path / ".agent_teams" / "unit-team" / "sessions" / "sess-1" / "worktrees") in roots
    assert str(tmp_path / "team-home" / "team-workspace") in roots
    assert str(tmp_path / "team-home" / "workspaces") in roots
    assert str(tmp_path / "team-home" / "sessions" / "sess-1" / "worktrees") in roots


def test_get_session_extra_history_roots_sanitizes_manual_session_worktree(tmp_path):
    with (
        patch(
            "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
            return_value={
                "team_name": "unit-team",
                "team_file_monitor_roots": [
                    str(tmp_path / ".agent_teams" / "unit-team" / "team-workspace"),
                ],
            },
        ),
        patch(
            "openjiuwen.agent_teams.paths.team_home",
            return_value=tmp_path / "team-home",
        ),
    ):
        from jiuwenswarm.server.runtime.session.git_diff_status import (
            get_session_extra_history_roots,
        )

        roots = get_session_extra_history_roots("sess/1:bad")

    assert str(tmp_path / ".agent_teams" / "unit-team" / "sessions" / "sess_1_bad" / "worktrees") in roots
    assert str(tmp_path / ".agent_teams" / "unit-team" / "sessions" / "sess" / "1:bad" / "worktrees") not in roots
    assert str(tmp_path / "team-home" / "sessions" / "sess_1_bad" / "worktrees") in roots


def test_get_session_extra_history_roots_adds_spawned_member_workspaces(tmp_path):
    history = [
        {
            "role": "assistant",
            "extra": {
                "event": {
                    "type": "team.member.spawned",
                    "name": "poet-song",
                    "member_id": "poet-song-id",
                }
            },
        }
    ]
    with (
        patch(
            "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
            return_value={
                "team_name": "unit-team",
                "team_file_monitor_roots": [
                    str(tmp_path / ".agent_teams" / "unit-team" / "team-workspace"),
                ],
            },
        ),
        patch(
            "jiuwenswarm.server.runtime.session.session_history.load_history_records",
            return_value=history,
        ),
        patch(
            "openjiuwen.agent_teams.paths.team_home",
            return_value=tmp_path / "team-home",
        ),
        patch(
            "openjiuwen.agent_teams.paths.independent_member_workspace",
            side_effect=lambda name: tmp_path / "independent" / f"{name}_workspace",
        ),
    ):
        from jiuwenswarm.server.runtime.session.git_diff_status import (
            get_session_extra_history_roots,
        )

        roots = get_session_extra_history_roots("sess-1")

    assert str(tmp_path / ".agent_teams" / "unit-team" / "workspaces" / "poet-song_workspace") in roots
    assert str(tmp_path / "team-home" / "workspaces" / "poet-song_workspace") in roots
    assert str(tmp_path / "independent" / "poet-song_workspace") in roots


def test_get_session_extra_history_roots_discovers_sub_agent_workspaces(tmp_path):
    """Single-agent mode (no team_name) should still find sub-agent dirs under workspace/sub_agents."""
    sub_agents_dir = tmp_path / "workspace" / "sub_agents"
    sub_agents_dir.mkdir(parents=True)
    (sub_agents_dir / "sess-1_sub_general-purpose_abc").mkdir()
    (sub_agents_dir / "sess-1_sub_general-purpose_def").mkdir()
    (sub_agents_dir / "sess-other_sub_general-purpose_xyz").mkdir()
    with (
        patch(
            "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
            return_value={
                "team_name": "",
                "team_file_monitor_roots": None,
            },
        ),
        patch(
            "jiuwenswarm.common.utils.get_agent_workspace_dir",
            return_value=tmp_path / "workspace",
        ),
    ):
        from jiuwenswarm.server.runtime.session.git_diff_status import (
            get_session_extra_history_roots,
        )

        roots = get_session_extra_history_roots("sess-1")

    assert str(sub_agents_dir / "sess-1_sub_general-purpose_abc") in roots
    assert str(sub_agents_dir / "sess-1_sub_general-purpose_def") in roots
    assert str(sub_agents_dir / "sess-other_sub_general-purpose_xyz") not in roots


def test_is_valid_file_ops_file_uses_suffix_match():
    """session_id 后缀匹配,避免子串误匹配其他 session 的 file_ops 文件。"""
    service = DiffService()
    # 正确匹配: session_id 是 .json 前的最后一段
    assert service._is_valid_file_ops_file("file_ops_agent_sess-1.json", "sess-1")
    assert service._is_valid_file_ops_file("file_ops_agent_sess-1.json", "sess-1", require_session=True)
    # 前缀不同的 session 不应匹配(sess-1 不应匹配 sess-10)
    assert not service._is_valid_file_ops_file("file_ops_agent_sess-10.json", "sess-1")
    # session_id 不在末尾段时不应匹配
    assert not service._is_valid_file_ops_file("file_ops_sess-1_agent.json", "sess-1")
    # 非 file_ops 前缀
    assert not service._is_valid_file_ops_file("other_ops_agent_sess-1.json", "sess-1")
    # 非 .json 后缀
    assert not service._is_valid_file_ops_file("file_ops_agent_sess-1.txt", "sess-1")
    # require_session=True 但 session_id=None
    assert not service._is_valid_file_ops_file("file_ops_agent.json", None, require_session=True)
    # require_session=False 且 session_id=None: 接受所有 file_ops 文件
    assert service._is_valid_file_ops_file("file_ops_agent.json", None)
    assert service._is_valid_file_ops_file("file_ops_agent.json", None, require_session=False)


def test_is_valid_file_ops_file_matches_sub_agent_sessions():
    """父 session_id 也应匹配子 agent 会话的 file_ops 文件(后缀 _sub_{type}_{suffix})。"""
    service = DiffService()
    parent = "sess_19fa7d326c9_87aa9a3ff27a"
    sub_name = "file_ops_93eeae01a6bb439eb7e241a9c8d8d375_sess_19fa7d326c9_87aa9a3ff27a_sub_general-purpose_17a7bada.json"
    assert service._is_valid_file_ops_file(sub_name, parent)
    assert service._is_valid_file_ops_file(sub_name, parent, require_session=True)
    exact_name = "file_ops_jiuwenswarm_sess_19fa7d326c9_87aa9a3ff27a.json"
    assert service._is_valid_file_ops_file(exact_name, parent)
    other_parent = "sess_other"
    assert not service._is_valid_file_ops_file(sub_name, other_parent)
    assert not service._is_valid_file_ops_file("file_ops_agent_sess_10.json", "sess_1")
    spoofed = "file_ops_abc_sess-1_sub_def_sess-other.json"
    assert service._is_valid_file_ops_file(spoofed, "sess-1")
    empty_agent = "file_ops__sess-1_sub_general-purpose_abc.json"
    assert not service._is_valid_file_ops_file(empty_agent, "sess-1")


def test_multi_history_root_first_wins_for_duplicate_entries(tmp_path, monkeypatch):
    """project_dir 的 file_ops 仅在重复 entry 冲突时优先。"""
    agent_ws = tmp_path / "agent-ws"
    user_ws = tmp_path / "user-ws"
    project_root = tmp_path / "project"
    extra_root = tmp_path / "extra"

    proj_hist = project_root / ".agent_history"
    extra_hist = extra_root / ".agent_history"
    proj_hist.mkdir(parents=True)
    extra_hist.mkdir(parents=True)

    target_file = str(project_root / "shared.py")
    # project_dir 记录的 old_content 优先
    (proj_hist / "file_ops_agent_sess-1.json").write_text(
        json.dumps({target_file: [{"action": "write", "timestamp": _ts(1784542850.0),
                                    "old_content": "proj-old\n", "new_content": "proj-new\n"}]}),
        encoding="utf-8",
    )
    # extra_root 记录了同一条重复 entry,不应让统计翻倍
    (extra_hist / "file_ops_agent_sess-1.json").write_text(
        json.dumps({target_file: [{"action": "write", "timestamp": _ts(1784542850.0),
                                    "old_content": "proj-old\n", "new_content": "proj-new\n"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_agent_workspace_dir", lambda: agent_ws)
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_user_workspace_dir", lambda: user_ws)

    ph = patch.object(DiffService, "_read_history", return_value=_HISTORY)
    pl = patch.object(DiffService, "_load_change_sets", return_value=[])
    ps = patch.object(DiffService, "_save_change_sets", return_value=None)
    with ph, pl, ps:
        service = DiffService()
        turns = service.get_turn_diffs(
            "sess-1", str(project_root),
            extra_history_roots=[str(extra_root)],
        )
    assert len(turns) == 1
    entry = turns[0]["files"][target_file]
    # project_dir 优先去重,重复 entry 不应被统计两次。
    assert entry["linesAdded"] == 1
    assert entry["linesRemoved"] == 1


def test_turn_diff_list_respects_limit():
    ph, pa, pl, ps = _patch_diff_service()
    with ph, pa, pl, ps:
        result = DiffStatusService.get_turn_diff_list(
            project=_PROJECT, session_id="sess-1", limit=1,
        )
    assert result["total"] == 2
    assert len(result["turns"]) == 1
    assert result["turns"][0]["turn_index"] == 2
    assert result["next_cursor"] == 1
    assert result["has_more"] is True


def test_turn_diff_list_respects_cursor():
    ph, pa, pl, ps = _patch_diff_service()
    with ph, pa, pl, ps:
        result = DiffStatusService.get_turn_diff_list(
            project=_PROJECT, session_id="sess-1", limit=1, cursor=1,
        )
    assert result["total"] == 2
    assert result["cursor"] == 1
    assert result["next_cursor"] == 2
    assert result["has_more"] is False
    assert len(result["turns"]) == 1
    assert result["turns"][0]["turn_index"] == 1


def test_turn_diff_list_empty_session():
    ph = patch.object(DiffService, "_read_history", return_value=[])
    pa = patch.object(DiffService, "_read_agent_history", return_value={})
    pl = patch.object(DiffService, "_load_change_sets", return_value=[])
    ps = patch.object(DiffService, "_save_change_sets", return_value=None)
    with ph, pa, pl, ps:
        result = DiffStatusService.get_turn_diff_list(
            project=_PROJECT, session_id="empty", limit=50,
        )
    assert result["total"] == 0
    assert result["turns"] == []


def test_turn_diff_list_limit_zero_returns_all():
    ph, pa, pl, ps = _patch_diff_service()
    with ph, pa, pl, ps:
        result = DiffStatusService.get_turn_diff_list(
            project=_PROJECT, session_id="sess-1", limit=0,
        )
    assert result["limit"] == 0
    assert result["total"] == 2
    assert len(result["turns"]) == 2


def _fake_git_service(repo_root="/proj", error=None):
    return SimpleNamespace(
        status=lambda project: SimpleNamespace(
            error=error,
            is_git=error is None,
            repo_root=repo_root,
            branch="main",
            head="abc123",
            transient=False,
            is_dirty=False,
        ),
    )


def test_turn_diff_detail_returns_files_and_hunks():
    ph, pa, pl, ps = _patch_diff_service()
    with ph, pa, pl, ps, patch(
        "jiuwenswarm.server.runtime.session.project_git.get_project_git_service",
        return_value=_fake_git_service(),
    ):
        result = DiffStatusService.get_turn_diff_detail(
            project=_PROJECT, session_id="sess-1", turn_index=1,
        )
    assert result is not None
    assert result["turn_index"] == 1
    assert result["project_id"] == "proj-1"
    assert result["session_id"] == "sess-1"
    assert result["repo_root"] == "/proj"
    assert result["branch"] == "main"
    assert result["base_head"] == "abc123"
    assert "files" in result
    assert "file_a.py" in result["files"]
    file_entry = result["files"]["file_a.py"]
    assert file_entry["status"] == "modified"
    assert file_entry["change_type"] == "modified"
    assert file_entry["is_deleted_file"] is False
    assert file_entry["lines_added"] == 1
    assert file_entry["lines_removed"] == 0
    assert len(file_entry["hunks"]) > 0
    assert "change_set_id" in result
    assert result["request_id"] == "req-001"


def test_turn_diff_detail_returns_none_for_missing_turn():
    ph, pa, pl, ps = _patch_diff_service()
    with ph, pa, pl, ps, patch(
        "jiuwenswarm.server.runtime.session.project_git.get_project_git_service",
        return_value=_fake_git_service(),
    ):
        result = DiffStatusService.get_turn_diff_detail(
            project=_PROJECT, session_id="sess-1", turn_index=99,
        )
    assert result is None


def test_turn_diff_detail_by_change_set_id():
    ph, pa, pl, ps = _patch_diff_service_with_persistence()
    with ph, pa, pl, ps, patch(
        "jiuwenswarm.server.runtime.session.project_git.get_project_git_service",
        return_value=_fake_git_service(),
    ):
        list_result = DiffStatusService.get_turn_diff_list(
            project=_PROJECT, session_id="sess-1", limit=50,
        )
        cs_id = list_result["turns"][-1]["change_set_id"]
        result = DiffStatusService.get_turn_diff_detail(
            project=_PROJECT, session_id="sess-1", change_set_id=cs_id,
        )
    assert result is not None
    assert result["turn_index"] == 1
    assert result["change_set_id"] == cs_id
    assert "file_a.py" in result["files"]


def test_turn_diff_detail_falls_back_when_not_git_repository():
    ph, pa, pl, ps = _patch_diff_service()
    git_error = GitError("NOT_GIT_REPOSITORY", "not a git repository")
    with ph, pa, pl, ps, patch(
        "jiuwenswarm.server.runtime.session.project_git.get_project_git_service",
        return_value=_fake_git_service(repo_root=None, error=git_error),
    ):
        result = DiffStatusService.get_turn_diff_detail(
            project=_PROJECT, session_id="sess-1", turn_index=1,
        )
    assert result is not None
    assert result["repo_root"] == "/proj"
    assert result["branch"] is None
    assert result["base_head"] is None
    assert "file_a.py" in result["files"]
    assert result["files"]["file_a.py"]["hunks"]


def test_turn_diff_detail_falls_back_when_git_not_found():
    ph, pa, pl, ps = _patch_diff_service()
    git_error = GitError("GIT_NOT_FOUND", "git executable not found")
    with ph, pa, pl, ps, patch(
        "jiuwenswarm.server.runtime.session.project_git.get_project_git_service",
        return_value=_fake_git_service(repo_root=None, error=git_error),
    ):
        result = DiffStatusService.get_turn_diff_detail(
            project=_PROJECT, session_id="sess-1", turn_index=1,
        )
    assert result is not None
    assert result["repo_root"] == "/proj"
    assert result["branch"] is None
    assert result["base_head"] is None
    assert "file_a.py" in result["files"]


def test_diff_status_falls_back_to_last_turn_when_not_git_repository():
    ph, pa, pl, ps = _patch_diff_service()
    git_error = GitError("NOT_GIT_REPOSITORY", "not a git repository")
    with ph, pa, pl, ps, patch(
        "jiuwenswarm.server.runtime.session.project_git.get_project_git_service",
        return_value=_fake_git_service(repo_root=None, error=git_error),
    ):
        result = DiffStatusService.get_project_diff_status(
            project=_PROJECT,
            session_id="sess-1",
            include_files=True,
            include_hunks=True,
        ).to_dict(include_hunks=True)
    assert result["repo"]["is_git"] is False
    assert result["repo"]["repo_root"] == "/proj"
    assert result["repo"]["branch"] is None
    assert result["current"] is None
    assert result["last_turn"] is not None
    assert "file_b.py" in result["last_turn"]["files"]


def test_diff_status_falls_back_to_last_turn_when_git_not_found():
    ph, pa, pl, ps = _patch_diff_service()
    git_error = GitError("GIT_NOT_FOUND", "git executable not found")
    with ph, pa, pl, ps, patch(
        "jiuwenswarm.server.runtime.session.project_git.get_project_git_service",
        return_value=_fake_git_service(repo_root=None, error=git_error),
    ):
        result = DiffStatusService.get_project_diff_status(
            project=_PROJECT,
            session_id="sess-1",
            include_files=True,
            include_hunks=True,
        ).to_dict(include_hunks=True)
    assert result["repo"]["is_git"] is False
    assert result["repo"]["repo_root"] == "/proj"
    assert result["repo"]["branch"] is None
    assert result["repo"]["head"] is None
    assert result["current"] is None
    assert result["last_turn"] is not None
    assert "file_b.py" in result["last_turn"]["files"]


def test_diff_status_uses_turn_summaries_for_last_turn_snapshot_fallback():
    snapshot_turn = {
        "turnIndex": 3,
        "timestamp": _ts(1784543000.0),
        "userPromptPreview": "snapshot only",
        "stats": {"filesChanged": 1, "linesAdded": 7, "linesRemoved": 2},
        "files": {
            "/proj/from_snapshot.py": {
                "linesAdded": 7,
                "linesRemoved": 2,
                "isNewFile": True,
            },
        },
        "change_set_id": "cs-snapshot",
        "request_id": "req-snapshot",
        "assistant_message_id": "req-snapshot:assistant",
        "user_message_id": "req-snapshot:user",
        "status": "completed",
    }
    with (
        patch.object(DiffService, "get_git_diff", return_value={}),
        patch.object(DiffService, "get_turn_diffs", return_value=[]) as full_diffs,
        patch.object(
            DiffService,
            "get_turn_diff_summaries",
            return_value=[snapshot_turn],
        ) as summaries,
    ):
        result = DiffStatusService.get_project_diff_status(
            project=_PROJECT,
            session_id="sess-1",
            include_files=False,
            include_hunks=False,
        ).to_dict(include_hunks=False)

    full_diffs.assert_not_called()
    summaries.assert_called_once()
    assert result["last_turn"] is not None
    assert result["last_turn"]["change_set_id"] == "cs-snapshot"
    assert result["last_turn"]["stats"] == {
        "files_changed": 1,
        "lines_added": 7,
        "lines_removed": 2,
    }
    assert result["last_turn"]["files"] == {}


def test_turn_diff_list_falls_back_when_not_git_repository():
    ph, pa, pl, ps = _patch_diff_service()
    git_error = GitError("NOT_GIT_REPOSITORY", "not a git repository")
    with ph, pa, pl, ps, patch(
        "jiuwenswarm.server.runtime.session.project_git.get_project_git_service",
        return_value=_fake_git_service(repo_root=None, error=git_error),
    ):
        result = DiffStatusService.get_turn_diff_list(
            project=_PROJECT, session_id="sess-1", limit=50,
        )
    assert result["project_id"] == "proj-1"
    assert result["session_id"] == "sess-1"
    assert result["repo_root"] == "/proj"
    assert result["branch"] is None
    assert result["base_head"] is None
    assert result["total"] == 2
    assert len(result["turns"]) == 2
    assert "file_b.py" in result["turns"][0]["files"]
    assert "file_a.py" in result["turns"][1]["files"]


def test_turn_diff_detail_respects_include_flags():
    ph, pa, pl, ps = _patch_diff_service()
    with ph, pa, pl, ps:
        result = DiffStatusService.get_turn_diff_detail(
            project=_PROJECT,
            session_id="sess-1",
            turn_index=1,
            include_files=True,
            include_hunks=False,
        )
    assert result is not None
    assert "file_a.py" in result["files"]
    assert result["files"]["file_a.py"]["hunks"] == []


def test_turn_diff_detail_can_omit_files():
    ph, pa, pl, ps = _patch_diff_service()
    with ph, pa, pl, ps:
        result = DiffStatusService.get_turn_diff_detail(
            project=_PROJECT,
            session_id="sess-1",
            turn_index=1,
            include_files=False,
            include_hunks=False,
        )
    assert result is not None
    assert result["files"] == {}


def test_turn_diff_detail_tolerates_transient_git_state(monkeypatch):
    """transient 状态不应阻断历史轮次回放。

    历史轮次基于 file_ops + change_set snapshot,不执行 git 命令。
    transient 时用 project_dir 兜底 repo_context,历史预览仍可用。
    """
    service = SimpleNamespace(
        status=lambda project: SimpleNamespace(
            error=None,
            repo_root="/proj",
            branch="main",
            head="abc123",
            transient=True,
        ),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.project_git.get_project_git_service",
        lambda: service,
    )
    ph, pa, pl, ps = _patch_diff_service()
    with ph, pa, pl, ps:
        result = DiffStatusService.get_turn_diff_detail(
            project=_PROJECT, session_id="sess-1", turn_index=1,
        )
    # 应返回历史数据,而非抛 GIT_TRANSIENT_STATE
    assert result is not None
    assert result["turn_index"] == 1
    # repo_root 用 project_dir 兜底(transient 时无法读 git)
    assert result["repo_root"] == "/proj"
    # 历史 turn 的文件应正常返回(路径相对 repo_root)
    assert "file_a.py" in result["files"]


def test_turn_diff_list_tolerates_transient_git_state(monkeypatch):
    """transient 状态不应阻断历史轮次列表。

    与 turn_diff_detail 同理:list 接口也基于 file_ops + snapshot,
    transient 时用 project_dir 兜底。
    """
    service = SimpleNamespace(
        status=lambda project: SimpleNamespace(
            error=None,
            repo_root="/proj",
            branch="main",
            head="abc123",
            transient=True,
        ),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.project_git.get_project_git_service",
        lambda: service,
    )
    ph, pa, pl, ps = _patch_diff_service()
    with ph, pa, pl, ps:
        result = DiffStatusService.get_turn_diff_list(
            project=_PROJECT, session_id="sess-1",
        )
    # 应返回历史轮次列表,而非抛 GIT_TRANSIENT_STATE
    assert result["total"] >= 1
    assert result["turns"]
    # repo_root 用 project_dir 兜底
    assert result["repo_root"] == "/proj"


def test_turn_diff_detail_tolerates_git_command_failed(monkeypatch):
    """非 transient 的 git 错误(如 command_failed)也不应阻断历史预览。

    timeout/command_failed 与 file_ops 历史回放无关,应同样用 project_dir 兜底。
    """
    from jiuwenswarm.server.runtime.session.project_git import GitError
    service = SimpleNamespace(
        status=lambda project: SimpleNamespace(
            error=GitError(
                code="GIT_COMMAND_FAILED",
                message="git command failed",
                retryable=True,
            ),
            repo_root=None,
            branch=None,
            head=None,
            transient=False,
        ),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.project_git.get_project_git_service",
        lambda: service,
    )
    ph, pa, pl, ps = _patch_diff_service()
    with ph, pa, pl, ps:
        result = DiffStatusService.get_turn_diff_detail(
            project=_PROJECT, session_id="sess-1", turn_index=1,
        )
    # 应返回历史数据,而非抛 GitOperationError
    assert result is not None
    assert result["turn_index"] == 1
    # repo_root 用 project_dir 兜底
    assert result["repo_root"] == "/proj"


def test_get_turn_diff_change_set_orphan_snapshot_is_expired(monkeypatch):
    snapshots = {
        "cs_old": {
            "turnIndex": 1,
            "change_set_id": "cs_old",
            "request_id": "req-old",
            "files": {},
            "stats": {"filesChanged": 0, "linesAdded": 0, "linesRemoved": 0},
        }
    }
    monkeypatch.setattr(
        DiffService,
        "_load_turn_snapshot",
        lambda self, session_id, change_set_id: snapshots.get(change_set_id),
    )
    ph, pa = (
        patch.object(DiffService, "_read_history", return_value=_HISTORY),
        patch.object(DiffService, "_read_agent_history", return_value=_FILE_OPS),
    )
    pl = patch.object(DiffService, "_load_change_sets", return_value=[])
    ps = patch.object(DiffService, "_save_change_sets", return_value=None)
    with ph, pa, pl, ps:
        service = DiffService()
        with pytest.raises(DiffHistoryExpiredError):
            service.get_turn_diff("sess-1", change_set_id="cs_old", project_dir="/proj")


def test_diff_file_entry_serializes_is_untracked():
    entry = DiffFileEntry(file_path="new.txt", is_untracked=True, is_new_file=True)
    assert entry.to_dict()["is_untracked"] is True
    assert entry.to_dict()["change_type"] == "modified"


def test_diff_file_entry_serializes_deleted_contract_fields():
    entry = DiffFileEntry(
        file_path="old.txt", status="deleted", is_deleted_file=True,
    )
    data = entry.to_dict()
    assert data["status"] == "deleted"
    assert data["change_type"] == "deleted"
    assert data["is_deleted_file"] is True


def test_turn_diff_persists_historical_repo_context():
    saved: list[dict] = []

    def _load(session_id):
        return list(saved)

    def _save(session_id, change_sets):
        saved.clear()
        saved.extend(change_sets)

    ph = patch.object(DiffService, "_read_history", return_value=_HISTORY)
    pa = patch.object(DiffService, "_read_agent_history", return_value=_FILE_OPS)
    pl = patch.object(DiffService, "_load_change_sets", side_effect=_load)
    ps = patch.object(DiffService, "_save_change_sets", side_effect=_save)
    with ph, pa, pl, ps:
        service = DiffService()
        turn = service.get_turn_diff(
            "sess-1",
            turn_index=1,
            project_dir="/proj",
            repo_context={
                "repo_root": "/proj",
                "branch": "feature/a",
                "base_head": "old-head",
            },
        )
        assert turn is not None
        cs_id = turn["change_set_id"]

        turn_after_branch_switch = service.get_turn_diff(
            "sess-1",
            change_set_id=cs_id,
            project_dir="/proj",
            repo_context={
                "repo_root": "/proj",
                "branch": "main",
                "base_head": "new-head",
            },
        )

    assert turn_after_branch_switch is not None
    assert turn_after_branch_switch["branch"] == "feature/a"
    assert turn_after_branch_switch["base_head"] == "old-head"


def test_parse_git_porcelain_status_maps_file_states():
    output = "\n".join(
        [
            " M modified.py",
            "A  added.py",
            "D  deleted.py",
            " D missing.py",
            "R  old.py -> renamed.py",
            "?? untracked.py",
        ]
    )

    assert DiffService._parse_git_porcelain_status(output) == {
        "modified.py": "modified",
        "added.py": "added",
        "deleted.py": "deleted",
        "missing.py": "missing",
        "renamed.py": "renamed",
        "untracked.py": "added",
    }
