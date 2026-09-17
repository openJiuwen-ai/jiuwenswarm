# Copyright (c) Huawei Technologies, Co., Ltd. 2025. All rights reserved.
"""conversation 回退不得销毁文件回滚能力.

复现场景（两个 turn 各改一个文件）：

    turn 1  编辑 demo.txt   ORIGINAL -> MODIFIED
    turn 2  编辑 notes.txt  ORIGINAL -> CHANGED
    /rewind 2 选 conversation   （只截断对话，**不动**工作区文件）
    /rewind 1 选 code           （应把两个文件都还原到 turn 1 之前）

修复前 conversation 回退会**物理删除** notes.txt 的 file_ops 快照，
notes.txt 从此处于"已被修改、但系统不再持有其原始内容"的状态：
后续 /rewind 找不到它，却仍报告成功。

修复后改为软删除（打 ``rewound_out`` 标记）：
显示层（turn diff）照旧看不到这些条目，还原层仍能用。
"""

import json
from datetime import datetime, timezone

import pytest

from jiuwenswarm.server.utils.diff_service import DiffService

TURN1_TS = 1_700_000_000.0
TURN2_TS = 1_700_000_100.0
# 每个 turn 内的编辑发生在该 turn 的 user 消息之后
EDIT1_TS = TURN1_TS + 10
EDIT2_TS = TURN2_TS + 10


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _history() -> list[dict]:
    return [
        {"role": "user", "content": "把 demo.txt 改成 MODIFIED", "timestamp": TURN1_TS},
        {"role": "assistant", "content": "done", "event_type": "chat.final",
         "timestamp": TURN1_TS + 20},
        {"role": "user", "content": "把 notes.txt 改成 CHANGED", "timestamp": TURN2_TS},
        {"role": "assistant", "content": "done", "event_type": "chat.final",
         "timestamp": TURN2_TS + 20},
    ]


@pytest.fixture
def project(tmp_path, monkeypatch):
    """搭一个带 history + file_ops 的项目目录，返回 (service, dir, ops_path)."""
    demo = tmp_path / "demo.txt"
    notes = tmp_path / "notes.txt"
    demo.write_text("MODIFIED-LINE-1\n", encoding="utf-8")
    notes.write_text("CHANGED-A\n", encoding="utf-8")

    hist_dir = tmp_path / ".agent_history"
    hist_dir.mkdir()
    ops_path = hist_dir / "file_ops_jiuwenswarm_tui_sess2241.json"
    ops_path.write_text(json.dumps({
        str(demo): [{
            "action": "edit", "timestamp": _iso(EDIT1_TS),
            "old_content": "ORIGINAL-LINE-1\n", "new_content": "MODIFIED-LINE-1\n",
        }],
        str(notes): [{
            "action": "edit", "timestamp": _iso(EDIT2_TS),
            "old_content": "ORIGINAL-A\n", "new_content": "CHANGED-A\n",
        }],
    }), encoding="utf-8")

    service = DiffService()
    monkeypatch.setattr(DiffService, "_read_history", staticmethod(lambda _sid: _history()))
    # 隔离掉真实 workspace 下的全局/其它 session file_ops，避免污染断言
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_agent_workspace_dir",
        lambda: tmp_path / "_agent_ws",
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_user_workspace_dir",
        lambda: tmp_path / "_user_ws",
    )
    return service, tmp_path, ops_path, demo, notes


def test_soft_truncate_keeps_files_restorable(project):
    """核心回归：conversation 回退后，被回退 turn 的文件仍能被 code 回退还原。"""
    service, proj_dir, _ops, demo, notes = project

    # 步骤 5：/rewind 2 选 conversation —— 只截断对话，不动文件
    service.truncate_file_ops_by_timestamp(
        "sess2241", TURN2_TS, project_dir=str(proj_dir), soft=True,
    )

    # 步骤 6：/rewind 1 选 code —— 两个文件都应可还原
    to_restore = service.get_files_to_restore("sess2241", 1, project_dir=str(proj_dir))

    assert str(demo) in to_restore, "demo.txt 应可还原"
    assert str(notes) in to_restore, "notes.txt 的快照被软删除后仍应可还原（#2241）"
    assert to_restore[str(demo)]["restore_content"] == "ORIGINAL-LINE-1\n"
    assert to_restore[str(notes)]["restore_content"] == "ORIGINAL-A\n"


def test_soft_truncate_hides_rewound_entries_from_turn_diff(project):
    """软删除必须保住原有的显示层语义：被回退的 turn 不再出现在 turn diff 中。"""
    service, proj_dir, _ops, demo, notes = project

    before = service.get_turn_diffs("sess2241", project_dir=str(proj_dir))
    assert {t["turnIndex"] for t in before} == {1, 2}

    service.truncate_file_ops_by_timestamp(
        "sess2241", TURN2_TS, project_dir=str(proj_dir), soft=True,
    )

    after = service.get_turn_diffs("sess2241", project_dir=str(proj_dir))
    assert {t["turnIndex"] for t in after} == {1}, "turn 2 的改动不应再显示"
    assert str(notes) not in after[0]["files"]
    assert str(demo) in after[0]["files"]


def test_soft_truncate_marks_but_does_not_drop_entries(project):
    """软删除落盘形态：条目保留、old_content 保留，只是多了标记。"""
    service, proj_dir, ops_path, _demo, notes = project

    service.truncate_file_ops_by_timestamp(
        "sess2241", TURN2_TS, project_dir=str(proj_dir), soft=True,
    )

    data = json.loads(ops_path.read_text(encoding="utf-8"))
    assert str(notes) in data, "软删除不得整个丢掉文件的 key"
    entry = data[str(notes)][0]
    assert entry["rewound_out"] is True
    assert entry["old_content"] == "ORIGINAL-A\n"


def test_hard_truncate_still_removes_entries(project):
    """硬删除路径（discard_turn_changes：文件已被写回）行为不变。"""
    service, proj_dir, ops_path, demo, notes = project

    service.truncate_file_ops_by_timestamp(
        "sess2241", TURN2_TS, project_dir=str(proj_dir),
    )

    data = json.loads(ops_path.read_text(encoding="utf-8"))
    assert str(notes) not in data
    assert str(demo) in data


def test_repeated_soft_truncate_is_idempotent(project):
    """重复 conversation 回退不应叠加副作用，也不应丢失快照。"""
    service, proj_dir, ops_path, _demo, notes = project

    for _ in range(3):
        service.truncate_file_ops_by_timestamp(
            "sess2241", TURN2_TS, project_dir=str(proj_dir), soft=True,
        )

    data = json.loads(ops_path.read_text(encoding="utf-8"))
    assert len(data[str(notes)]) == 1
    assert data[str(notes)][0]["old_content"] == "ORIGINAL-A\n"


def test_later_edit_becomes_new_baseline(project, monkeypatch):
    """软删除的快照不会"复活"成错误的还原目标.

    若 conversation 回退后用户又在新 turn 里改了同一个文件，
    新一轮的 /rewind 应还原到**新**快照，而非更早的软删除快照。
    """
    service, proj_dir, ops_path, _demo, notes = project

    service.truncate_file_ops_by_timestamp(
        "sess2241", TURN2_TS, project_dir=str(proj_dir), soft=True,
    )

    # 新 turn 2：再次编辑 notes.txt（时间戳晚于软删除的那条）
    new_turn2_ts = TURN2_TS + 1000
    data = json.loads(ops_path.read_text(encoding="utf-8"))
    data[str(notes)].append({
        "action": "edit", "timestamp": _iso(new_turn2_ts + 10),
        "old_content": "CHANGED-A\n", "new_content": "REWRITTEN-A\n",
    })
    ops_path.write_text(json.dumps(data), encoding="utf-8")

    history = _history()[:2] + [
        {"role": "user", "content": "再改 notes.txt", "timestamp": new_turn2_ts},
        {"role": "assistant", "content": "done", "event_type": "chat.final",
         "timestamp": new_turn2_ts + 20},
    ]
    monkeypatch.setattr(DiffService, "_read_history", staticmethod(lambda _sid: history))

    to_restore = service.get_files_to_restore("sess2241", 2, project_dir=str(proj_dir))
    assert to_restore[str(notes)]["restore_content"] == "CHANGED-A\n"
