import time

import pytest

from jiuwenswarm.server.runtime.session import session_history


def _wait_history(session_id: str, *, min_count: int = 1):
    deadline = time.time() + 5
    while time.time() < deadline:
        data = session_history.load_history_records(session_id)
        if len(data) >= min_count:
            return data
        time.sleep(0.05)
    return session_history.load_history_records(session_id)


def test_append_history_skips_empty_chat_final_and_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)

    session_history.append_history_record(
        session_id="s-empty",
        request_id="r1",
        channel_id="web",
        role="assistant",
        event_type="chat.final",
        content="   ",
        timestamp=1.0,
    )
    session_history.append_history_record(
        session_id="s-empty",
        request_id="r2",
        channel_id="web",
        role="assistant",
        event_type="chat.final",
        content="HEARTBEAT_OK",
        timestamp=2.0,
    )
    session_history.append_history_record(
        session_id="heartbeat_abc",
        request_id="r3",
        channel_id="heartbeat",
        role="user",
        content="heartbeat prompt",
        timestamp=3.0,
    )
    session_history.append_history_record(
        session_id="s-empty",
        request_id="r4",
        channel_id="web",
        role="assistant",
        event_type="chat.file",
        content="",
        timestamp=4.0,
        extra={"files": [{"path": "/tmp/a.md", "name": "a.md"}]},
    )
    session_history.append_history_record(
        session_id="s-empty",
        request_id="r5",
        channel_id="web",
        role="assistant",
        event_type="chat.final",
        content="hello",
        timestamp=5.0,
    )

    data = _wait_history("s-empty", min_count=2)
    assert [item.get("event_type") for item in data] == ["chat.file", "chat.final"]
    assert data[1]["content"] == "hello"
    assert session_history.load_history_records("heartbeat_abc") == []


def test_empty_cancel_marker_survives_history_reload(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    session_history.append_history_record(
        session_id="s-stopped", request_id="picture", channel_id="xiaoyi",
        role="assistant", event_type="chat.final", content="", timestamp=1.0,
        extra={"aborted": True},
    )
    records = _wait_history("s-stopped")
    assert len(records) == 1
    assert records[0]["aborted"] is True
    assert records[0]["request_id"] == "picture"


def test_resumed_same_request_final_replaces_stop_marker():
    records = [
        {"id": "picture:assistant", "event_type": "chat.final", "content": "", "aborted": True},
        {"id": "picture:assistant", "event_type": "chat.final", "content": "识别完成"},
    ]
    restored = session_history._dedup_records_last_wins(records)
    assert restored == [records[1]]


def test_resume_keeps_distinct_partial_text_but_clears_its_stop_flag():
    records = [
        {"id": "picture:assistant", "event_type": "chat.final", "content": "识别了一部分", "aborted": True},
        {"id": "picture:assistant", "event_type": "chat.final", "content": "继续识别的结果"},
    ]
    restored = session_history._dedup_records_last_wins(records)
    assert [r["content"] for r in restored] == ["识别了一部分", "继续识别的结果"]
    assert not any(r.get("aborted") for r in restored)
    assert records[0]["aborted"] is True


@pytest.mark.parametrize("event_type,content,extra", [
    ("chat.final", "正在识别图片", {}),
    ("chat.tool_call", "", {"tool_call": {"name": "bash", "tool_call_id": "picture-call"}}),
])
def test_empty_stop_marker_merges_into_existing_round_record(event_type, content, extra):
    records = [
        {"id": "picture:assistant", "event_type": event_type, "content": content, **extra},
        {"id": "weather:user", "role": "user", "content": "深圳天气"},
        {"id": "picture:assistant", "event_type": "chat.final", "content": "", "aborted": True},
    ]
    restored = session_history._dedup_records_last_wins(records)
    assert len(restored) == 2
    assert restored[0]["aborted"] is True
    assert restored[0]["content"] == content
    assert restored[1] == records[1]
    assert "aborted" not in records[0]


def test_resume_clears_tool_stop_flag_after_history_rewrite():
    records = [
        {"id": "picture:assistant", "event_type": "chat.tool_call", "content": "",
         "tool_call": {"name": "bash", "tool_call_id": "picture-call"}},
        {"id": "picture:assistant", "event_type": "chat.final", "content": "", "aborted": True},
    ]
    rewritten = session_history._dedup_records_last_wins(records)
    rewritten.append({"id": "picture:assistant", "event_type": "chat.final", "content": "识别完成"})
    restored = session_history._dedup_records_last_wins(rewritten)
    assert not any(r.get("aborted") for r in restored)
    assert restored[0]["tool_call"]["tool_call_id"] == "picture-call"


def test_assistant_file_event_is_restored_for_team_history() -> None:
    assert session_history._is_team_relevant(
        {
            "event_type": "chat.file",
            "role": "assistant",
            "files": [
                {
                    "name": "report.xlsx",
                    "download_url": "/file-api/download?token=signed",
                }
            ],
        }
    )


def test_has_persistable_assistant_payload_tool_result_with_tool_call_id():
    assert session_history._has_persistable_assistant_payload(
        content_text="",
        event_type="chat.tool_result",
        extra={"tool_call_id": "call_abc", "tool_name": "list_files", "result": "ok"},
    ) is True


def test_has_persistable_assistant_payload_tool_result_with_nested_tool_result():
    assert session_history._has_persistable_assistant_payload(
        content_text="",
        event_type="chat.tool_result",
        extra={"tool_result": {"tool_name": "test", "tool_call_id": "call_abc", "result": "ok"}},
    ) is True


def test_has_persistable_assistant_payload_tool_result_empty_rejected():
    assert session_history._has_persistable_assistant_payload(
        content_text="",
        event_type="chat.tool_result",
        extra={},
    ) is False


def test_has_persistable_assistant_payload_tool_result_falsy_values_rejected():
    assert session_history._has_persistable_assistant_payload(
        content_text="",
        event_type="chat.tool_result",
        extra={"tool_call_id": "", "tool_result": None},
    ) is False


def test_has_persistable_assistant_payload_processing_status_still_rejected():
    assert session_history._has_persistable_assistant_payload(
        content_text="",
        event_type="chat.processing_status",
        extra={"is_processing": True},
    ) is False


def test_has_persistable_assistant_payload_tool_update_still_rejected():
    assert session_history._has_persistable_assistant_payload(
        content_text="",
        event_type="chat.tool_update",
        extra={"tool_call_id": "call_abc", "beam_search": {}},
    ) is False


def test_append_history_persists_tool_result(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)

    session_history.append_history_record(
        session_id="s-tool-result",
        request_id="r1",
        channel_id="web",
        role="assistant",
        event_type="chat.tool_call",
        content="",
        timestamp=1.0,
        extra={"tool_call": {"name": "list_files", "id": "call_abc", "arguments": "{}"}},
    )
    session_history.append_history_record(
        session_id="s-tool-result",
        request_id="r1",
        channel_id="web",
        role="assistant",
        event_type="chat.tool_result",
        content="",
        timestamp=2.0,
        extra={"tool_call_id": "call_abc", "tool_name": "list_files", "result": "file1.txt", "success": True},
    )
    session_history.append_history_record(
        session_id="s-tool-result",
        request_id="r1",
        channel_id="web",
        role="assistant",
        event_type="chat.final",
        content="Done",
        timestamp=3.0,
    )

    data = _wait_history("s-tool-result", min_count=3)
    event_types = [item.get("event_type") for item in data]
    assert event_types == ["chat.tool_call", "chat.tool_result", "chat.final"]

    tool_result_record = data[1]
    assert tool_result_record["event_type"] == "chat.tool_result"
    assert tool_result_record["tool_call_id"] == "call_abc"
    assert tool_result_record["tool_name"] == "list_files"


def test_append_history_persists_tool_result_with_nested_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)

    session_history.append_history_record(
        session_id="s-nested",
        request_id="r1",
        channel_id="web",
        role="assistant",
        event_type="chat.tool_result",
        content="cancelled",
        timestamp=1.0,
        extra={
            "tool_result": {
                "tool_name": "code",
                "tool_call_id": "call_xyz",
                "result": "cancelled",
                "status": "error",
            },
        },
    )

    data = _wait_history("s-nested", min_count=1)
    assert len(data) == 1
    assert data[0]["event_type"] == "chat.tool_result"


def test_append_history_tool_result_empty_payload_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)

    session_history.append_history_record(
        session_id="s-empty-tr",
        request_id="r1",
        channel_id="web",
        role="assistant",
        event_type="chat.tool_result",
        content="",
        timestamp=1.0,
        extra={},
    )

    import time as _t
    _t.sleep(0.3)
    data = session_history.load_history_records("s-empty-tr")
    assert data == []


def test_dedup_records_last_wins_keeps_distinct_bubbles():
    """多气泡：同 id 但 bubble_seq 不同的 chat.final 各泡保留；
    同泡快照按内容前缀语义收敛（草稿是终稿前缀才丢弃）。"""
    records = [
        {"id": "r1:assistant", "event_type": "chat.final", "content": "泡0草稿", "bubble_seq": 0},
        {"id": "r1:assistant", "event_type": "chat.final", "content": "泡0草稿补充", "bubble_seq": 0},
        {"id": "r1:assistant", "event_type": "chat.final", "content": "泡1", "bubble_seq": 1},
        {"id": "r1:assistant", "event_type": "chat.final", "content": "泡2", "bubble_seq": 2},
    ]
    out = session_history._dedup_records_last_wins(records)
    contents = [r["content"] for r in out]
    assert contents == ["泡0草稿补充", "泡1", "泡2"]


def test_dedup_records_last_wins_legacy_records_without_bubble_seq_unchanged():
    """存量无 bubble_seq 的同 id chat.final：独立正文段（非终稿前缀）保留——
    穿插时间线重建依赖中间正文段；前缀快照仍收敛为终稿。"""
    records = [
        {"id": "r1:assistant", "event_type": "chat.final", "content": "草稿"},
        {"id": "r1:assistant", "event_type": "chat.final", "content": "草稿补充"},
        {"id": "r1:assistant", "event_type": "chat.final", "content": "独立段"},
        {"id": "r1:assistant", "event_type": "chat.tool_result", "content": "工具"},
    ]
    out = session_history._dedup_records_last_wins(records)
    contents = [r["content"] for r in out]
    assert contents == ["草稿补充", "独立段", "工具"]


# ===========================================================================
# 不可落盘 session_id 守卫：空 id 和 "default" 兜底占位符都不应落盘，
# 避免凭空 mkdir 空会话目录。
# ===========================================================================
@pytest.mark.parametrize("bad_sid", [None, "", "   ", "\t", "default"])
def test_append_history_non_persistable_session_id_skips_and_no_dir(
    tmp_path, monkeypatch, bad_sid
):
    """空 id 或 "default" 占位符不应落盘，也不应凭空创建 default 空目录。

    回归保护：曾因 ``sid = (session_id or "default")`` 把空 id 兜底成 "default"，
    导致进程在 mkdir 后、异步 flush 写盘前退出时残留空 default 目录。
    现在连上游已兜底成字面量 "default" 的也一并拒绝。
    """
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)

    session_history.append_history_record(
        session_id=bad_sid,
        request_id="r1",
        channel_id="web",
        role="user",
        content="hello",
        timestamp=1.0,
    )
    session_history.flush_history_writes()

    # 不应出现 default 目录，sessions 根下不应有任何子目录
    assert not (tmp_path / "default").exists()
    assert list(tmp_path.iterdir()) == []


def test_append_history_valid_session_id_still_writes(tmp_path, monkeypatch):
    """守卫不影响正常会话：真实 session_id 仍正常落盘。"""
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)

    session_history.append_history_record(
        session_id="s-real",
        request_id="r1",
        channel_id="web",
        role="user",
        content="hello",
        timestamp=1.0,
    )
    data = _wait_history("s-real", min_count=1)
    assert len(data) == 1
    assert data[0]["content"] == "hello"


@pytest.mark.parametrize("bad_sid", [None, "", "   ", "default"])
def test_truncate_history_non_persistable_session_id_no_dir(
    tmp_path, monkeypatch, bad_sid
):
    """空 id 或 "default" 截断历史不应建目录。"""
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)

    result = session_history.truncate_history_records(session_id=bad_sid, cut_index=0)
    assert result == {"remaining_records": 0, "removed_records": 0}
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("bad_sid", [None, "", "default"])
def test_write_history_records_non_persistable_session_id_raises(
    tmp_path, monkeypatch, bad_sid
):
    """空 id 或 "default" 重写历史应显式拒绝（不静默建空目录）。"""
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)

    with pytest.raises(ValueError):
        session_history.write_history_records(bad_sid, [{"role": "user", "content": "x"}])
    assert list(tmp_path.iterdir()) == []
