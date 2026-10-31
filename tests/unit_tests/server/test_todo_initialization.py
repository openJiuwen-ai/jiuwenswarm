"""会话元数据是初始化依据；文件丢失和旧队列写不能重新授予初始化。"""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from jiuwenswarm.server.runtime.session import session_metadata as metadata


def new_session(tmp_path):
    record = {"session_id": "s", "todo_initialized": False, "title": "new"}
    metadata._write_metadata_sync("s", record, sessions_root=str(tmp_path))
    return record


def test_first_initialization_is_consumed_before_commit_and_survives_reload(tmp_path):
    queued = new_session(tmp_path)
    metadata.check_todo_initialization("s", file_exists=False, sessions_root=tmp_path)
    metadata.check_todo_initialization("s", file_exists=False, committing=True, sessions_root=tmp_path)
    # An old asynchronous write, including repeated init, cannot reset the marker.
    metadata._write_metadata_sync("s", queued, sessions_root=str(tmp_path))
    path = tmp_path / "s/metadata.json"
    assert json.loads(path.read_text())["todo_initialized"] is True
    with pytest.raises(ValueError, match="first initialization"):
        metadata.check_todo_initialization("s", file_exists=False, sessions_root=tmp_path)
    metadata.check_todo_initialization("s", file_exists=True, committing=True, sessions_root=tmp_path)


def test_legacy_session_can_initialize_once_without_an_existing_marker(tmp_path):
    metadata._write_metadata_sync("s", {"session_id": "s"}, sessions_root=str(tmp_path))
    metadata._write_metadata_sync("s", {"todo_initialized": False}, sessions_root=str(tmp_path))
    metadata.check_todo_initialization("s", file_exists=False, sessions_root=tmp_path)
    assert "todo_initialized" not in json.loads((tmp_path / "s/metadata.json").read_text())
    metadata.check_todo_initialization("s", file_exists=False, committing=True, sessions_root=tmp_path)
    assert json.loads((tmp_path / "s/metadata.json").read_text())["todo_initialized"] is True
    with pytest.raises(ValueError, match="first initialization"):
        metadata.check_todo_initialization("s", file_exists=False, sessions_root=tmp_path)


def test_implicit_external_flash_chat_can_initialize_without_a_new_client_field(tmp_path, monkeypatch):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.handlers import _shared

    monkeypatch.setattr(_shared, "_sessions_dir_for_request", lambda _request: tmp_path)
    request = AgentRequest(
        request_id="external-request", channel_id="chat-service", session_id="external",
        req_method=ReqMethod.CHAT_SEND, params={"mode": "flash"},
    )
    _shared._sync_chat_request_metadata(request, None, "flash", explicit_mode_provided=True, adapter_mode="flash")
    stored = json.loads((tmp_path / "external/metadata.json").read_text())
    assert "todo_initialized" not in stored
    metadata.check_todo_initialization("external", file_exists=False, committing=True, sessions_root=tmp_path)
    assert json.loads((tmp_path / "external/metadata.json").read_text())["todo_initialized"] is True
    with pytest.raises(ValueError, match="first initialization"):
        metadata.check_todo_initialization("external", file_exists=False, sessions_root=tmp_path)


@pytest.mark.parametrize("value", [None, "false", 0, 1])
def test_invalid_initialization_marker_is_not_reset(tmp_path, value):
    path = tmp_path / "s/metadata.json"
    metadata._write_metadata_sync("s", {"session_id": "s", "todo_initialized": value}, sessions_root=str(tmp_path))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="marker is invalid"):
        metadata.check_todo_initialization("s", file_exists=False, committing=True, sessions_root=tmp_path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("has_marker", [False, True])
def test_only_one_competing_initializer_can_consume_the_record(tmp_path, has_marker):
    if has_marker:
        new_session(tmp_path)
    else:
        metadata._write_metadata_sync("s", {"session_id": "s"}, sessions_root=str(tmp_path))

    def claim():
        try:
            metadata.check_todo_initialization("s", file_exists=False, committing=True, sessions_root=tmp_path)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: claim(), range(2))) == [False, True]


def test_binding_rollback_preserves_consumed_origin(tmp_path):
    new_session(tmp_path)
    before = metadata.capture_session_team_binding_artifacts("s", sessions_root=tmp_path)
    metadata.check_todo_initialization("s", file_exists=False,
                                       committing=True, sessions_root=tmp_path)
    metadata.restore_session_team_binding_artifacts("s", before, sessions_root=tmp_path)
    with pytest.raises(ValueError, match="first initialization"):
        metadata.check_todo_initialization("s", file_exists=False,
                                           sessions_root=tmp_path)


def test_binding_rollback_can_remove_unused_origin(tmp_path):
    new_session(tmp_path)
    metadata.restore_session_team_binding_artifacts("s", (None, None), sessions_root=tmp_path)
    assert not (tmp_path / "s/metadata.json").exists()


def test_internal_initialization_record_is_not_returned_to_clients(tmp_path, monkeypatch):
    new_session(tmp_path)
    monkeypatch.setattr(metadata, "_apply_metadata_defaults_with_inference", lambda _sid, data, **kwargs: data)
    result = metadata.get_session_metadata("s", enable_writeback=False, sessions_root=tmp_path)
    assert "todo_initialized" not in result
    assert "todo_initialized" in json.loads((tmp_path / "s/metadata.json").read_text())


def test_processes_cannot_both_initialize_the_same_object(tmp_path):
    import os
    import subprocess
    import sys

    new_session(tmp_path)
    script = """
import sys
from jiuwenswarm.server.runtime.session.session_metadata import check_todo_initialization
try:
    check_todo_initialization('s', file_exists=False, committing=True,
                              sessions_root=sys.argv[1])
except ValueError:
    sys.exit(3)
"""
    children = [subprocess.Popen([sys.executable, "-B", "-c", script, str(tmp_path)],
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for _ in range(2)]
    try:
        for child in children:
            _out, error = child.communicate(timeout=20)
            assert child.returncode in (0, 3), error.decode()
        assert sorted(child.returncode for child in children) == [0, 3]
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
