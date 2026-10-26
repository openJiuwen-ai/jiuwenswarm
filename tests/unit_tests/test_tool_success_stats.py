"""工具调用成败统计单元测试（session_history 内联实现）：增量扫描 / 不重复统计 / 截断重扫 / 判定口径."""

import json
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.session import session_history as tss


def _write_history(sess_dir: Path, records: list[dict]) -> None:
    with (sess_dir / "history.json").open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _scan(tmp_path: Path, session_id: str = "sess_t") -> dict:
    tss._tool_stats_scan_once(session_id, str(tmp_path))
    state_path = tmp_path / session_id / tss._TOOL_STATS_FILENAME
    return json.loads(state_path.read_text(encoding="utf-8"))


def test_scan_counts_success_and_failed(tmp_path: Path):
    sess = tmp_path / "sess_t"
    sess.mkdir()
    _write_history(sess, [
        {"event_type": "chat.tool_call", "tool_call": {"name": "bash", "tool_call_id": "c1"}},
        {"event_type": "chat.tool_result", "tool_call_id": "c1", "result": "success=True data=ok"},
        {"event_type": "chat.tool_call", "tool_call": {"name": "web_search", "tool_call_id": "c2"}},
        {"event_type": "chat.tool_result", "tool_call_id": "c2",
         "result": "success=False data=None error='boom'"},
        {"event_type": "chat.tool_result", "tool_call_id": "c3", "status": "error", "result": "x"},
    ])
    state = _scan(tmp_path)
    stats = state["stats"]
    assert stats["total"] == 3
    assert stats["success"] == 1
    assert stats["failed"] == 2
    assert stats["by_tool"]["bash"]["success"] == 1
    assert stats["by_tool"]["bash"]["failed"] == 0
    assert stats["by_tool"]["web_search"]["failed"] == 1


def test_incremental_no_double_count(tmp_path: Path):
    sess = tmp_path / "sess_t"
    sess.mkdir()
    _write_history(sess, [
        {"event_type": "chat.tool_call", "tool_call": {"name": "bash", "tool_call_id": "c1"}},
        {"event_type": "chat.tool_result", "tool_call_id": "c1", "result": "success=True"},
    ])
    first = _scan(tmp_path)["stats"]
    # 追加新事件后再扫，不能重复统计旧事件
    with (sess / "history.json").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"event_type": "chat.tool_call",
                             "tool_call": {"name": "bash", "tool_call_id": "c2"}}) + "\n")
        fh.write(json.dumps({"event_type": "chat.tool_result",
                             "tool_call_id": "c2", "result": "success=True"}) + "\n")
    second = _scan(tmp_path)["stats"]
    assert second["total"] == first["total"] + 1
    assert second["success"] == first["success"] + 1


def test_truncate_rescans_from_zero(tmp_path: Path):
    sess = tmp_path / "sess_t"
    sess.mkdir()
    records = [
        {"event_type": "chat.tool_call", "tool_call": {"name": "bash", "tool_call_id": "c1"}},
        {"event_type": "chat.tool_result", "tool_call_id": "c1", "result": "success=True"},
    ]
    _write_history(sess, records * 20)
    _scan(tmp_path)
    # rewind：截断回 2 行
    _write_history(sess, records)
    state = _scan(tmp_path)
    assert state["stats"]["total"] == 1
    assert state["offset"] <= (sess / "history.json").stat().st_size


def test_pending_open_calls(tmp_path: Path):
    sess = tmp_path / "sess_t"
    sess.mkdir()
    _write_history(sess, [
        {"event_type": "chat.tool_call", "tool_call": {"name": "bash", "tool_call_id": "c1"}},
        {"event_type": "chat.tool_result", "tool_call_id": "c1", "result": "success=True"},
        {"event_type": "chat.tool_call", "tool_call": {"name": "web_search", "tool_call_id": "c2"}},
    ])
    stats = _scan(tmp_path)["stats"]
    assert stats["pending"] == 1
    assert stats["total"] == 1


def test_schedule_debounce_dedup():
    tss._TOOL_STATS_PENDING.clear()
    tss.schedule_tool_stats_update("s1", None)
    tss.schedule_tool_stats_update("s1", None)
    tss.schedule_tool_stats_update("s1", None)
    assert len(tss._TOOL_STATS_PENDING) == 1
    tss._TOOL_STATS_PENDING.clear()


def test_success_rate_and_worst_tool(tmp_path: Path):
    sess = tmp_path / "sess_t"
    sess.mkdir()
    records: list[dict] = []
    # bash: 8 成功 2 失败（失败率 0.2，失败次数 2）
    for i in range(10):
        records.append({"event_type": "chat.tool_call",
                        "tool_call": {"name": "bash", "tool_call_id": f"b{i}"}})
        ok = i >= 2
        records.append({"event_type": "chat.tool_result", "tool_call_id": f"b{i}",
                        "result": "success=True" if ok else "success=False error='x'"})
    # web_search: 3 成功 3 失败（失败率 0.5，失败次数 3 → 按失败次数最多者优先）
    for i in range(6):
        records.append({"event_type": "chat.tool_call",
                        "tool_call": {"name": "web_search", "tool_call_id": f"w{i}"}})
        ok = i < 3
        records.append({"event_type": "chat.tool_result", "tool_call_id": f"w{i}",
                        "result": "success=True" if ok else "success=False error='y'"})
    _write_history(sess, records)
    stats = _scan(tmp_path)["stats"]
    assert stats["total"] == 16
    assert stats["completed_total"] == 16
    assert stats["success_rate"] == round(11 / 16, 4)
    worst = stats["worst_tool"]
    assert worst["name"] == "web_search"
    assert worst["failed"] == 3
    assert worst["total"] == 6
    assert worst["failure_rate"] == 0.5
    # per-tool 成功率
    assert stats["by_tool"]["bash"]["success_rate"] == 0.8
    assert stats["by_tool"]["web_search"]["success_rate"] == 0.5


def test_worst_tool_absent_when_no_failure(tmp_path: Path):
    sess = tmp_path / "sess_t"
    sess.mkdir()
    _write_history(sess, [
        {"event_type": "chat.tool_call", "tool_call": {"name": "bash", "tool_call_id": "c1"}},
        {"event_type": "chat.tool_result", "tool_call_id": "c1", "result": "success=True"},
    ])
    stats = _scan(tmp_path)["stats"]
    assert stats["success_rate"] == 1.0
    assert stats["worst_tool"] is None


