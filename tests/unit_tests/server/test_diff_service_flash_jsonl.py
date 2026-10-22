# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""flash JSONL history 与 stock JSON 的消费面一致性（diff_service 合并适配）。

验收项（见 flash-editfile-io-analysis-and-optimization.md §7）：
- flash 编辑条目正确参与轮次 diff / /rewind 还原（get_files_to_restore）；
- 软删除截断（truncate soft=True）对 JSONL 打 rewound_out 标记：显示层
  不可见、还原层仍可用；
- restore / 硬删除对 JSONL 的行为与 stock JSON 完全一致。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from jiuwenswarm.agents.harness.flash.tools import flash_file_history
from jiuwenswarm.server.utils.diff_service import DiffService, _entry_sort_ts

TURN1_TS = 1_700_001_000.0
EDIT1_TS = TURN1_TS + 10   # stock JSON 记录的编辑（demo.txt）
EDIT2_TS = TURN1_TS + 20   # flash JSONL 记录的编辑（notes.txt）
EDIT3_TS = TURN1_TS + 30   # flash JSONL 记录的编辑（demo.txt，与 stock 同文件）

SESSION_ID = "sessflash01"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _history() -> list[dict]:
    return [
        {"role": "user", "content": "改两个文件", "timestamp": TURN1_TS},
        {"role": "assistant", "content": "done", "event_type": "chat.final",
         "timestamp": TURN1_TS + 60},
    ]


@pytest.fixture
def project(tmp_path, monkeypatch):
    """项目目录同时携带 stock JSON 与 flash JSONL 两本账本。"""
    demo = tmp_path / "demo.txt"
    notes = tmp_path / "notes.txt"
    demo.write_text("AFTER\n", encoding="utf-8")
    notes.write_text("CHANGED\n", encoding="utf-8")

    hist_dir = tmp_path / ".agent_history"
    hist_dir.mkdir()

    stock_ops = hist_dir / f"file_ops_jiuwenswarm_tui_{SESSION_ID}.json"
    stock_ops.write_text(json.dumps({
        str(demo): [{
            "action": "edit", "timestamp": _iso(EDIT1_TS),
            "old_content": "BEFORE\n", "new_content": "AFTER\n",
        }],
    }), encoding="utf-8")

    flash_ops = hist_dir / f"file_ops_flash_jiuwenswarm_tui_{SESSION_ID}.jsonl"
    flash_file_history.append_flash_history_entry(
        str(flash_ops), str(notes), {
            "action": "edit", "timestamp": _iso(EDIT2_TS),
            "old_content": "ORIGINAL\n", "new_content": "CHANGED\n",
        },
    )
    flash_file_history.append_flash_history_entry(
        str(flash_ops), str(demo), {
            "action": "edit", "timestamp": _iso(EDIT3_TS),
            "old_content": "AFTER\n", "new_content": "AFTER-FLASH\n",
        },
    )

    service = DiffService()
    monkeypatch.setattr(DiffService, "_read_history", staticmethod(lambda _sid: _history()))
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_agent_workspace_dir",
        lambda: tmp_path / "_agent_ws",
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_user_workspace_dir",
        lambda: tmp_path / "_user_ws",
    )
    return service, tmp_path, demo, notes, stock_ops, flash_ops


def test_jsonl_entries_merged_into_agent_history(project):
    service, proj_dir, demo, notes, _stock_ops, flash_ops = project

    history = service._read_agent_history(SESSION_ID, str(proj_dir))

    assert str(notes) in history, "flash JSONL 条目必须被合并读取"
    assert str(demo) in history
    demo_entries = history[str(demo)]
    assert len(demo_entries) == 2, "同文件的 stock+flash 条目应合并到同一列表"
    assert {e["old_content"] for e in demo_entries} == {"BEFORE\n", "AFTER\n"}


def test_naive_timestamps_are_interpreted_as_utc():
    """diff 侧排序键必须与 flash_bash_history 的 naive→UTC 策略一致。"""
    naive = _entry_sort_ts("2026-01-01T00:00:00")
    aware = _entry_sort_ts("2026-01-01T00:00:00+00:00")

    assert naive == aware
    assert naive == datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()


def test_turn_diffs_include_flash_edits(project):
    service, proj_dir, demo, notes, _stock_ops, _flash_ops = project

    diffs = service.get_turn_diffs(SESSION_ID, project_dir=str(proj_dir))

    assert len(diffs) == 1
    files = diffs[0]["files"]
    assert str(notes) in files, "flash 编辑必须出现在轮次 diff 中"
    assert str(demo) in files


def test_get_files_to_restore_sees_flash_entries(project):
    """/rewind code：flash 条目的 old_content 是还原依据，不得漏。"""
    service, proj_dir, demo, notes, _stock_ops, _flash_ops = project

    to_restore = service.get_files_to_restore(SESSION_ID, 1, project_dir=str(proj_dir))

    assert to_restore[str(demo)]["restore_content"] == "BEFORE\n"
    assert to_restore[str(notes)]["restore_content"] == "ORIGINAL\n"


def test_soft_truncate_marks_jsonl_entries(project):
    service, proj_dir, demo, notes, _stock_ops, flash_ops = project

    # 截断掉 turn 1 的全部编辑（soft：只对显示层隐藏，保留快照）。
    service.truncate_file_ops_by_timestamp(
        SESSION_ID, TURN1_TS, project_dir=str(proj_dir), soft=True,
    )

    # 显示层：条目被隐藏。
    visible = service._read_agent_history(SESSION_ID, str(proj_dir))
    assert visible == {} or not any(
        not e.get("rewound_out") for entries in visible.values() for e in entries
    )

    # 还原层：仍能拿到 old_content。
    to_restore = service.get_files_to_restore(SESSION_ID, 1, project_dir=str(proj_dir))
    assert to_restore[str(notes)]["restore_content"] == "ORIGINAL\n"

    # 落盘形态：JSONL 被原子重写为带标记的条目（不再是无标记原文）。
    on_disk = flash_file_history.load_flash_history(flash_ops)
    assert on_disk[str(notes)][0].get("rewound_out") is True


def test_restore_rewound_jsonl_entries(project):
    service, proj_dir, demo, notes, _stock_ops, flash_ops = project

    service.truncate_file_ops_by_timestamp(
        SESSION_ID, TURN1_TS, project_dir=str(proj_dir), soft=True,
    )
    service.restore_rewound_entries_by_timestamp(
        SESSION_ID, TURN1_TS, project_dir=str(proj_dir),
    )

    visible = service._read_agent_history(SESSION_ID, str(proj_dir))
    assert str(notes) in visible
    assert not visible[str(notes)][0].get("rewound_out")
    on_disk = flash_file_history.load_flash_history(flash_ops)
    assert not on_disk[str(notes)][0].get("rewound_out")


def test_hard_truncate_removes_jsonl_entries(project):
    service, proj_dir, demo, notes, _stock_ops, flash_ops = project

    service.truncate_file_ops_by_timestamp(
        SESSION_ID, TURN1_TS, project_dir=str(proj_dir), soft=False,
    )

    visible = service._read_agent_history(SESSION_ID, str(proj_dir))
    assert visible == {}
    on_disk = flash_file_history.load_flash_history(flash_ops)
    assert on_disk == {}


def test_get_files_to_redo_sees_flash_entries(project):
    """redo（discard 软删后的重做）：flash 条目的 new_content 是重做依据。

    同文件多代快照时取"最后一条"被标记条目——合并后的时间戳稳定排序
    保证该语义跨账本成立（demo 的 stock 与 flash 条目分属两本账本）。
    """
    service, proj_dir, demo, notes, _stock_ops, flash_ops = project

    service.truncate_file_ops_by_timestamp(
        SESSION_ID, TURN1_TS, project_dir=str(proj_dir), soft=True, discarded=True,
    )

    to_redo = service.get_files_to_redo(SESSION_ID, 1, project_dir=str(proj_dir))

    assert to_redo[str(notes)]["content"] == "CHANGED\n"
    # demo: stock 条目(BEFORE→AFTER, EDIT1) + flash 条目(AFTER→AFTER-FLASH,
    # EDIT3) 都被标记；重做应取时间戳最大的 flash 条目的 new_content。
    assert to_redo[str(demo)]["content"] == "AFTER-FLASH\n"
    on_disk = flash_file_history.load_flash_history(flash_ops)
    assert on_disk[str(demo)][-1].get("discarded_out") is True


def test_dirty_timestamps_sort_last_and_merge_survives(project):
    """缺省 / 非法 timestamp 条目：排序稳定落尾，不阻断合并（评审意见 2）。

    ``_entry_sort_ts`` 对无法解析的时间戳返回 inf（排最后）；合法条目的
    时间序不受脏数据影响。truncate 对解析失败条目"保留"是存量容忍行为，
    本测试同时钉住。
    """
    service, proj_dir, demo, notes, _stock_ops, flash_ops = project

    flash_file_history.append_flash_history_entry(
        str(flash_ops), str(notes),
        {"action": "edit", "old_content": "x", "new_content": "y"},  # 缺 timestamp
    )
    flash_file_history.append_flash_history_entry(
        str(flash_ops), str(notes),
        {
            "action": "edit", "timestamp": "not-a-date",
            "old_content": "x", "new_content": "y",
        },
    )

    history = service._read_agent_history(SESSION_ID, str(proj_dir))

    entries = history[str(notes)]
    assert len(entries) == 3
    assert entries[0]["timestamp"] == _iso(EDIT2_TS), "合法条目仍按时间序在前"
    assert set(e.get("timestamp") for e in entries[1:]) == {None, "not-a-date"}
    # demo 的合法跨账本条目不受 notes 的脏数据影响
    assert [e["old_content"] for e in history[str(demo)]] == ["BEFORE\n", "AFTER\n"]


def test_flash_module_missing_degrades_gracefully(project, monkeypatch):
    """flash 包被裁剪（ImportError）时 load/save 优雅降级（评审意见 1）。

    读：.jsonl 按空账本跳过（stock 条目照常可见），ImportError 不得穿透
    ``_read_agent_history`` 的窄 except 打断 diff 链；写：跳过重写不抛错。
    """
    import sys

    service, proj_dir, demo, notes, stock_ops, flash_ops = project

    monkeypatch.setitem(
        sys.modules,
        "jiuwenswarm.agents.harness.flash.tools.flash_file_history",
        None,
    )

    history = service._read_agent_history(SESSION_ID, str(proj_dir))
    assert str(demo) in history, "stock 账本条目必须仍然可见"
    assert str(notes) not in history, "flash-only 条目降级为不可见（不抛错）"

    # 截断 / 恢复路径同样不抛 ImportError，stock 账本照常处理
    service.truncate_file_ops_by_timestamp(
        SESSION_ID, TURN1_TS, project_dir=str(proj_dir), soft=False,
    )
    visible_after = service._read_agent_history(SESSION_ID, str(proj_dir))
    assert visible_after == {}, "stock 账本条目（>= cutoff）应被正常截断"


def test_is_valid_file_ops_file_jsonl_naming():
    valid = DiffService._is_valid_file_ops_file

    assert valid(f"file_ops_flash_jiuwenswarm_tui_{SESSION_ID}.jsonl", SESSION_ID)
    assert valid(f"file_ops_flash_jiuwenswarm_tui_{SESSION_ID}.jsonl", SESSION_ID, require_session=True)
    # 子 agent 会话的 JSONL 同样接受。
    assert valid(f"file_ops_flash_demo_{SESSION_ID}_sub_gp_abc123.jsonl", SESSION_ID)
    # 其它 session / 非约定前缀 / 未知扩展名一律拒绝；stock .json 命名继续有效。
    assert not valid("file_ops_flash_jiuwenswarm_tui_other_session.jsonl", SESSION_ID)
    assert not valid("notes_file_ops_flash_x.jsonl", SESSION_ID)
    assert not valid("file_ops_flash_x.jsonl.bak", SESSION_ID)
    assert valid(f"file_ops_jiuwenswarm_tui_{SESSION_ID}.json", SESSION_ID)
    # 无 session_id 时：任何约定命名的 file_ops 都算（require_session=False）。
    assert valid("file_ops_flash_jiuwenswarm_tui_sessflash01.jsonl", None)
    assert not valid("file_ops_flash_jiuwenswarm_tui_sessflash01.jsonl", None, require_session=True)
