# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for redo-related DiffService + session_ops methods.

与 ``test_diff_service_rewound_snapshots`` 对称,覆盖 redo 路径核心逻辑:
  - ``get_files_to_redo``: soft truncate(discarded=True) 后返回 discarded 条目的
    new_content,忽略未标记条目;new_content 为 None 时返回 delete action;
    同一 turn 内同一文件多次编辑时取最后一条(最终态)
  - ``restore_rewound_entries_by_timestamp``: 去掉软删除标记,
    条目内容保留,只影响 >= cutoff_ts 的条目;discarded=True 时只恢复
    discarded_out,不触碰 rewound_out(避免误暴露 rewind 软隐藏条目)
  - ``redo_session_files``: 写回 new_content(CRLF 原样) + delete action 删文件
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jiuwenswarm.server.utils.diff_service import DiffService

TURN1_TS = 1_700_000_000.0
TURN2_TS = 1_700_000_100.0
EDIT1_TS = TURN1_TS + 10
EDIT2_TS = TURN2_TS + 10


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _history() -> list[dict]:
    return [
        {"role": "user", "content": "edit demo.txt", "timestamp": TURN1_TS},
        {"role": "assistant", "content": "done", "event_type": "chat.final", "timestamp": TURN1_TS + 20},
        {"role": "user", "content": "edit notes.txt", "timestamp": TURN2_TS},
        {"role": "assistant", "content": "done", "event_type": "chat.final", "timestamp": TURN2_TS + 20},
    ]


@pytest.fixture
def project(tmp_path, monkeypatch):
    """搭一个带 history + file_ops 的项目目录,返回 (service, dir, ops_path, demo, notes)."""
    demo = tmp_path / "demo.txt"
    notes = tmp_path / "notes.txt"
    demo.write_text("MODIFIED-LINE-1\n", encoding="utf-8")
    notes.write_text("CHANGED-A\n", encoding="utf-8")

    hist_dir = tmp_path / ".agent_history"
    hist_dir.mkdir()
    ops_path = hist_dir / "file_ops_jiuwenswarm_tui_sess2241.json"
    ops_path.write_text(json.dumps({
        str(demo): [{"action": "edit", "timestamp": _iso(EDIT1_TS),
                     "old_content": "ORIGINAL-LINE-1\n", "new_content": "MODIFIED-LINE-1\n"}],
        str(notes): [{"action": "edit", "timestamp": _iso(EDIT2_TS),
                      "old_content": "ORIGINAL-A\n", "new_content": "CHANGED-A\n"}],
    }), encoding="utf-8")

    service = DiffService()
    monkeypatch.setattr(DiffService, "_read_history", staticmethod(lambda _sid: _history()))
    monkeypatch.setattr("jiuwenswarm.server.utils.diff_service.get_agent_workspace_dir", lambda: tmp_path / "_agent_ws")
    monkeypatch.setattr("jiuwenswarm.server.utils.diff_service.get_user_workspace_dir", lambda: tmp_path / "_user_ws")
    return service, tmp_path, ops_path, demo, notes


def test_get_files_to_redo_after_soft_truncate(project):
    """soft truncate(discarded=True) 后 get_files_to_redo 返回 discarded 条目的 new_content,忽略未标记条目。"""
    service, proj_dir, _ops, demo, notes = project

    service.truncate_file_ops_by_timestamp("sess2241", TURN2_TS, project_dir=str(proj_dir), soft=True, discarded=True)
    to_redo = service.get_files_to_redo("sess2241", 2, project_dir=str(proj_dir))

    # notes.txt(turn 2,被 discard)返回 new_content
    assert str(notes) in to_redo
    assert to_redo[str(notes)]["content"] == "CHANGED-A\n"
    assert to_redo[str(notes)]["action"] == "write"
    # demo.txt(turn 1,未 discard)不在结果中
    assert str(demo) not in to_redo


def test_get_files_to_redo_delete_action(project):
    """new_content 为 None(agent 删除了文件)时 redo 返回 action=delete。"""
    service, proj_dir, ops_path, _demo, _notes = project

    deleted_file = str(Path(str(proj_dir)) / "temp.py")
    data = json.loads(ops_path.read_text(encoding="utf-8"))
    data[deleted_file] = [{"action": "delete", "timestamp": _iso(EDIT2_TS + 5),
                           "old_content": "TEMP\n", "new_content": None}]
    ops_path.write_text(json.dumps(data), encoding="utf-8")

    service.truncate_file_ops_by_timestamp("sess2241", TURN2_TS, project_dir=str(proj_dir), soft=True, discarded=True)
    to_redo = service.get_files_to_redo("sess2241", 2, project_dir=str(proj_dir))

    assert deleted_file in to_redo
    assert to_redo[deleted_file]["content"] is None
    assert to_redo[deleted_file]["action"] == "delete"


def test_restore_rewound_entries_removes_markers(project):
    """restore 去掉 rewound_out 标记,条目内容保留,只影响 >= cutoff_ts 的条目。"""
    service, proj_dir, ops_path, demo, notes = project

    # 同时 soft-truncate 两个 turn
    service.truncate_file_ops_by_timestamp("sess2241", TURN1_TS, project_dir=str(proj_dir), soft=True)
    data = json.loads(ops_path.read_text(encoding="utf-8"))
    assert data[str(demo)][0].get("rewound_out") is True
    assert data[str(notes)][0].get("rewound_out") is True

    # 只 restore turn 2
    service.restore_rewound_entries_by_timestamp("sess2241", TURN2_TS, project_dir=str(proj_dir))

    data = json.loads(ops_path.read_text(encoding="utf-8"))
    # turn 2 的 notes.txt 标记移除,内容保留
    assert "rewound_out" not in data[str(notes)][0]
    assert data[str(notes)][0]["new_content"] == "CHANGED-A\n"
    # turn 1 的 demo.txt 标记保留(timestamp < TURN2_TS)
    assert data[str(demo)][0].get("rewound_out") is True


def test_redo_session_files_write_and_delete(tmp_path: Path):
    """redo_session_files: 写回 new_content(CRLF 原样) + delete action 删文件。"""
    from jiuwenswarm.agents.harness.common.session_ops_service import redo_session_files

    write_target = tmp_path / "notes.txt"
    write_target.write_bytes(b"OLD\r\n")
    delete_target = tmp_path / "temp.py"
    delete_target.write_text("TEMP\n", encoding="utf-8")
    assert delete_target.exists()

    new_crlf = "NEW-A\r\nNEW-B\r\n"
    files_to_redo = {
        str(write_target): {"content": new_crlf, "action": "write"},
        str(delete_target): {"content": None, "action": "delete"},
    }

    mock_diff = MagicMock()
    mock_diff.get_files_to_redo.return_value = files_to_redo

    with patch("jiuwenswarm.server.utils.diff_service.get_diff_service", return_value=mock_diff):
        result = redo_session_files(session_id="sess-1", turn_index=1)

    assert result["errors"] == []
    # write: CRLF 原样写回
    assert str(write_target) in result["redone_files"]
    assert write_target.read_bytes() == new_crlf.encode("utf-8")
    assert b"\r\r\n" not in write_target.read_bytes()
    # delete: 文件被删除
    assert str(delete_target) in result["deleted_files"]
    assert not delete_target.exists()


def test_redo_session_files_delete_missing_file_still_recorded(tmp_path: Path):
    """delete action 目标文件不存在时仍记为已处理(目标状态已满足)。

    避免被 handler 误判成 REDO_HISTORY_MISSING:文件不存在 == discard 前状态,
    redo 后状态一致,属于正常完成,不应保留 discarded 状态。
    """
    from jiuwenswarm.agents.harness.common.session_ops_service import redo_session_files

    missing_target = tmp_path / "already_gone.py"
    assert not missing_target.exists()

    files_to_redo = {
        str(missing_target): {"content": None, "action": "delete"},
    }

    mock_diff = MagicMock()
    mock_diff.get_files_to_redo.return_value = files_to_redo

    with patch("jiuwenswarm.server.utils.diff_service.get_diff_service", return_value=mock_diff):
        result = redo_session_files(session_id="sess-1", turn_index=1)

    assert result["errors"] == []
    # 即使文件不存在,delete action 仍记入 deleted_files
    # (handler 据此判断 redo 有处理项,不会误返回 REDO_HISTORY_MISSING)
    assert str(missing_target) in result["deleted_files"]
    assert not missing_target.exists()


def test_get_files_to_redo_takes_last_entry_for_final_state(project):
    """同一 turn 内同一文件多次编辑时,get_files_to_redo 取最后一条(最终态)。

    不能取第一条就 break——那会写回中间态,导致 redo 后文件内容与 discard 前不一致。
    """
    service, proj_dir, ops_path, _demo, _notes = project

    # 在 turn 2 内对 notes.txt 做两次编辑(中间态 → 最终态)
    multi_file = str(proj_dir / "multi.txt")
    data = json.loads(ops_path.read_text(encoding="utf-8"))
    data[multi_file] = [
        {"action": "edit", "timestamp": _iso(EDIT2_TS + 1),
         "old_content": "V0\n", "new_content": "V1-INTERMEDIATE\n"},
        {"action": "edit", "timestamp": _iso(EDIT2_TS + 5),
         "old_content": "V1-INTERMEDIATE\n", "new_content": "V2-FINAL\n"},
    ]
    ops_path.write_text(json.dumps(data), encoding="utf-8")

    service.truncate_file_ops_by_timestamp(
        "sess2241", TURN2_TS, project_dir=str(proj_dir), soft=True, discarded=True,
    )
    to_redo = service.get_files_to_redo("sess2241", 2, project_dir=str(proj_dir))

    # 应取最后一条 entry 的 new_content(最终态),而非第一条(中间态)
    assert multi_file in to_redo
    assert to_redo[multi_file]["content"] == "V2-FINAL\n"
    assert to_redo[multi_file]["action"] == "write"


def test_restore_discarded_only_restores_discarded_out(project):
    """restore(discarded=True) 只恢复 discarded_out,不触碰 rewound_out。

    场景:conversation rewind 先软隐藏了 turn 1(demo.txt 打 rewound_out),
    然后 discard 软隐藏了 turn 2(notes.txt 打 discarded_out)。
    redo turn 2 时只应恢复 discarded_out,rewound_out 保持隐藏,
    避免 last turn diff 混入不属于当前 history 的修改。
    """
    service, proj_dir, ops_path, demo, notes = project

    # turn 1: conversation rewind 路径 → rewound_out
    service.truncate_file_ops_by_timestamp(
        "sess2241", TURN1_TS, project_dir=str(proj_dir), soft=True,
    )
    # turn 2: discard 路径 → discarded_out
    service.truncate_file_ops_by_timestamp(
        "sess2241", TURN2_TS, project_dir=str(proj_dir), soft=True, discarded=True,
    )

    data = json.loads(ops_path.read_text(encoding="utf-8"))
    assert data[str(demo)][0].get("rewound_out") is True
    assert data[str(notes)][0].get("discarded_out") is True

    # redo turn 2: 只恢复 discarded_out
    service.restore_rewound_entries_by_timestamp(
        "sess2241", TURN2_TS, project_dir=str(proj_dir), discarded=True,
    )

    data = json.loads(ops_path.read_text(encoding="utf-8"))
    # turn 2 的 discarded_out 已移除,内容保留
    assert "discarded_out" not in data[str(notes)][0]
    assert data[str(notes)][0]["new_content"] == "CHANGED-A\n"
    # turn 1 的 rewound_out 仍然保留(redo 不应暴露 rewind 软隐藏条目)
    assert data[str(demo)][0].get("rewound_out") is True
    assert "discarded_out" not in data[str(demo)][0]


def test_unmark_turn_discarded_sets_status_completed(tmp_path, monkeypatch):
    """unmark_turn_discarded 显式写回 status=completed,而非 pop 掉。"""
    from jiuwenswarm.server.utils.diff_service import DiffService

    cs_id = "cs_test_123"
    saved_entries: list[dict] = [
        {"change_set_id": cs_id, "turn_index": 1, "status": "discarded",
         "timestamp": _iso(TURN1_TS), "start_timestamp": TURN1_TS,
         "end_timestamp": TURN1_TS + 100, "user_prompt_preview": "",
         "request_id": "", "assistant_message_id": "", "user_message_id": "",
         "stats": {"filesChanged": 0, "linesAdded": 0, "linesRemoved": 0}},
    ]
    snapshots: dict[str, dict] = {
        cs_id: {"turnIndex": 1, "change_set_id": cs_id, "status": "discarded",
                "files": {}, "stats": {"filesChanged": 0, "linesAdded": 0, "linesRemoved": 0},
                "timestamp": _iso(TURN1_TS), "start_timestamp": TURN1_TS,
                "end_timestamp": TURN1_TS + 100, "userPromptPreview": "",
                "request_id": "", "assistant_message_id": "", "user_message_id": ""},
    }

    monkeypatch.setattr(DiffService, "_read_history", staticmethod(lambda _sid: [
        {"role": "user", "content": "hi", "timestamp": TURN1_TS},
        {"role": "assistant", "content": "done", "event_type": "chat.final",
         "timestamp": TURN1_TS + 20},
    ]))
    monkeypatch.setattr(DiffService, "_read_agent_history", lambda *a, **k: {})
    monkeypatch.setattr(DiffService, "_load_change_sets", lambda self, sid: list(saved_entries))
    monkeypatch.setattr(DiffService, "_save_change_sets", lambda self, sid, cs: (saved_entries.clear(), saved_entries.extend(cs))[1])
    monkeypatch.setattr(DiffService, "_load_turn_snapshot", lambda self, sid, csid: snapshots.get(csid))
    monkeypatch.setattr(DiffService, "_save_turn_snapshot", lambda self, sid, snap: snapshots.__setitem__(snap["change_set_id"], dict(snap)))
    monkeypatch.setattr(DiffService, "restore_rewound_entries_by_timestamp", lambda *a, **k: None)

    service = DiffService()
    result = service.unmark_turn_discarded("sess-1", 1, project_dir="/proj")
    assert result == cs_id

    # status 应被显式设为 "completed",而非被 pop 掉(缺少字段)
    assert saved_entries[0].get("status") == "completed"
    assert snapshots[cs_id].get("status") == "completed"
