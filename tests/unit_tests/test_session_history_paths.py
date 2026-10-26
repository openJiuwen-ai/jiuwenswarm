import json
import time

from jiuwenswarm.server.runtime.session import session_history


def _wait_history(session_id: str, *, min_count: int = 1):
    deadline = time.time() + 5
    while time.time() < deadline:
        data = session_history.load_history_records(session_id)
        if len(data) >= min_count:
            return data
        time.sleep(0.05)
    return session_history.load_history_records(session_id)


def test_read_history_paths_do_not_create_missing_session_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)

    session_id = "sess_missing"
    session_dir = tmp_path / session_id

    read_path = session_history.get_read_history_path(session_id)

    assert read_path == session_dir / "history.json"
    assert not session_dir.exists()
    assert not session_history.history_exists(session_id)
    assert session_history.load_history_records(session_id) == []
    assert not session_dir.exists()


def test_write_history_path_still_creates_session_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)

    session_id = "sess_new"
    session_dir = tmp_path / session_id

    write_path = session_history.get_write_history_path(session_id)

    assert write_path == session_dir / "history.json"
    assert session_dir.is_dir()


def test_append_writes_jsonl_into_history_json(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)

    session_history.append_history_record(
        session_id="s-jsonl",
        request_id="r1",
        channel_id="web",
        role="user",
        content="hello",
        timestamp=1.0,
        task_id="todo:1",
    )
    session_history.append_history_record(
        session_id="s-jsonl",
        request_id="r1",
        channel_id="web",
        role="assistant",
        event_type="chat.final",
        content="world",
        timestamp=2.0,
        task_id="todo:1",
    )

    data = _wait_history("s-jsonl", min_count=2)
    path = tmp_path / "s-jsonl" / "history.json"
    raw = path.read_text(encoding="utf-8")

    assert not raw.lstrip().startswith("[")
    lines = [line for line in raw.splitlines() if line.strip()]
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["content"] == "hello"
    assert parsed[0]["task_id"] == "todo:1"
    assert parsed[1]["event_type"] == "chat.final"
    assert parsed[1]["task_id"] == "todo:1"
    assert data == parsed


def test_load_history_reads_legacy_json_array(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)

    session_dir = tmp_path / "s-array"
    session_dir.mkdir()
    records = [
        {"id": "r1:user", "role": "user", "request_id": "r1", "content": "old"},
        {"id": "r1:assistant", "role": "assistant", "request_id": "r1", "event_type": "chat.final", "content": "array"},
    ]
    (session_dir / "history.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    loaded = session_history.load_history_records("s-array")
    assert [item["content"] for item in loaded] == ["old", "array"]


def test_append_converts_legacy_json_array_then_appends_jsonl(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)

    session_dir = tmp_path / "s-migrate"
    session_dir.mkdir()
    (session_dir / "history.json").write_text(
        json.dumps(
            [{"id": "r0:user", "role": "user", "request_id": "r0", "content": "legacy"}],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    session_history.append_history_record(
        session_id="s-migrate",
        request_id="r1",
        channel_id="web",
        role="user",
        content="new",
        timestamp=1.0,
    )

    data = _wait_history("s-migrate", min_count=2)
    raw = (session_dir / "history.json").read_text(encoding="utf-8")
    assert not raw.lstrip().startswith("[")
    assert [item["content"] for item in data] == ["legacy", "new"]
    assert [json.loads(line)["content"] for line in raw.splitlines() if line.strip()] == [
        "legacy",
        "new",
    ]
