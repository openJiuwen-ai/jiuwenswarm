# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real independent writers must preserve fields and their queued revisions."""

from __future__ import annotations

import copy
import importlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import time

import pytest


def test_front_fields_survive_worker_cache_and_queued_updates(tmp_path):
    env = os.environ.copy()
    env["JIUWENSWARM_HOME"] = str(tmp_path / "home")
    env["JIUWENSWARM_DATA_DIR"] = str(tmp_path / "data")
    worker_code = """
import json, sys
from jiuwenswarm.server.runtime.session import session_metadata as sm
sm.init_session_metadata(session_id='same-session', title='initial')
sm.update_session_metadata(session_id='same-session', accent_color='default', sync=True)
sm.get_session_metadata('same-session')
print('ready', flush=True)
sys.stdin.readline()
for _ in range(32):
    sm.update_session_metadata(session_id='same-session', increment_message_count=True)
assert sm.flush_pending_writes(10)
value=sm.get_session_metadata('same-session', cache_bust=True)
print(json.dumps({key:value.get(key) for key in ('accent_color','message_count','title')}), flush=True)
"""
    worker = subprocess.Popen([sys.executable, "-X", "utf8", "-c", worker_code], env=env,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, encoding="utf-8")
    try:
        assert worker.stdout.readline().strip() == "ready"
        front_code = """
from jiuwenswarm.server.runtime.session.session_metadata import patch_session_metadata
patch_session_metadata('same-session', {'accent_color':'blue', 'title':'renamed by front'})
"""
        front = subprocess.run([sys.executable, "-X", "utf8", "-c", front_code], env=env,
                               capture_output=True, text=True, encoding="utf-8", timeout=20)
        assert front.returncode == 0, front.stderr
        out, err = worker.communicate("continue\n", timeout=20)
        assert worker.returncode == 0, err
        result = json.loads(out.strip().splitlines()[-1])
        assert result == {"accent_color": "blue", "message_count": 32, "title": "renamed by front"}
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.communicate()


@pytest.mark.parametrize("side,field,original,concurrent", [
    ("server", "moa_enabled", False, True),
    ("server", "session_harness_checkpoint", {"cursor": "old"}, {"cursor": "new"}),
    ("server", "group_members", [{"name": "old"}], [{"name": "new"}]),
])
@pytest.mark.parametrize("write_mode", ["sync", "queued", "full"])
def test_explicit_same_snapshot_value_remains_an_owned_update(
    monkeypatch, tmp_path, side, field, original, concurrent, write_mode,
):
    sm = importlib.import_module(f"jiuwenswarm.{side}.runtime.session.session_metadata")
    lc = importlib.import_module(f"jiuwenswarm.{side}.runtime.session.lifecycle")
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(sm, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(lc, "get_agent_sessions_dir", lambda: sessions)
    for name in ("_METADATA_CACHE", "_METADATA_CACHE_GENERATIONS", "_CACHE_DISK_STAMPS", "_PENDING_FIELDS"):
        monkeypatch.setattr(sm, name, {})
    monkeypatch.setattr(sm, "_METADATA_QUEUE", queue.Queue(maxsize=1))
    monkeypatch.setattr(sm, "_ensure_worker_started", lambda: None)
    stale = {
        "session_id": "explicit-update", "title": "original", "message_count": 7,
        "mode": "agent.work.normal", "work_mode": "work", field: original,
    }
    sm._write_metadata_sync("explicit-update", stale)
    sm._write_metadata_sync(
        "explicit-update", {field: concurrent},
        sm._MetadataWriteOptions(
            merge_fields=frozenset({field}), owns_moa_enabled=True,
            **({"replace_harness_checkpoint": True} if side == "server" else {}),
        ),
    )
    monkeypatch.setattr(sm, "_read_metadata", lambda *args, **kwargs: copy.deepcopy(stale))
    if write_mode == "full":
        sm._METADATA_QUEUE.put_nowait(("occupied", {}, None))
    sm.update_session_metadata(
        session_id="explicit-update", touch_last_message_at=False,
        sync_write=write_mode == "sync", **{field: original},
    )
    if write_mode == "queued":
        session_id, metadata, options = sm._METADATA_QUEUE.get_nowait()
        sm._write_metadata_sync(session_id, metadata, options)
        sm._METADATA_QUEUE.task_done()
    persisted = lc.raw_metadata("explicit-update")
    assert persisted[field] == original
    assert persisted["title"] == "original"
    assert persisted["message_count"] == 7


def _process(code: str, *args: str) -> subprocess.Popen:
    root = Path(__file__).resolve().parents[3]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.Popen(
        [sys.executable, "-c", code, *args], cwd=root, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
    )


def _stop_processes(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_server_processes_preserve_disjoint_metadata_updates():
    from jiuwenswarm.server.runtime.session import lifecycle as lc
    from jiuwenswarm.server.runtime.session import session_metadata as sm

    sm.init_session_metadata(
        session_id="cross-process", channel_id="web", mode="agent.work.normal",
    )
    code = """
import json, sys
role = sys.argv[1]
from jiuwenswarm.server.runtime.session import session_metadata as sm
sm.get_session_metadata('cross-process')
print('ready', flush=True)
sys.stdin.readline()
for index in range(40):
    if role == 'fields':
        sm.update_session_metadata(
            session_id='cross-process', moa_enabled=index % 2 == 1,
            session_harness_checkpoint={'index': index}, group_members=[{'name': str(index)}],
            sync_write=True, touch_last_message_at=False,
        )
    else:
        sm.update_session_metadata(
            session_id='cross-process', title='writer-' + str(index),
            increment_message_count=True, sync_write=True, touch_last_message_at=False,
        )
assert sm.flush_pending_writes(5)
print('completed', flush=True)
"""
    processes = [_process(code, role) for role in ("fields", "title")]
    try:
        for process in processes:
            assert process.stdout.readline().strip() == "ready"
        for process in processes:
            process.stdin.write("go\n")
            process.stdin.flush()
        path = sm.get_agent_sessions_dir() / "cross-process" / "metadata.json"
        deadline = time.monotonic() + 20
        reads = 0
        while any(process.poll() is None for process in processes):
            assert time.monotonic() < deadline, "metadata child process did not finish"
            with lc.resource_lock("session", "cross-process"):
                current = json.loads(path.read_text(encoding="utf-8"))
            assert current["session_id"] == "cross-process"
            assert "created_at" in current
            reads += 1
            time.sleep(0.001)
        for process in processes:
            output, errors = process.communicate(timeout=5)
            assert process.returncode == 0, errors
            assert output.strip() == "completed"
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert reads > 0
        assert saved["message_count"] == 40
        assert saved["title"] == "writer-39"
        assert saved["moa_enabled"] is True
        assert saved["session_harness_checkpoint"] == {"index": 39}
        assert saved["group_members"] == [{"name": "39"}]
    finally:
        _stop_processes(processes)


@pytest.mark.parametrize("side", ["server"])
def test_resource_file_lock_serializes_real_processes(tmp_path, side):
    path = str(tmp_path / "contended.json")
    holder_code = """
import importlib, pathlib, sys
store = importlib.import_module('jiuwenswarm.' + sys.argv[1] + '.runtime.session.project_store')
with store.file_lock(pathlib.Path(sys.argv[2])):
    print('held', flush=True)
    sys.stdin.readline()
"""
    waiter_code = """
import importlib, pathlib, sys
store = importlib.import_module('jiuwenswarm.' + sys.argv[1] + '.runtime.session.project_store')
print('attempting', flush=True)
with store.file_lock(pathlib.Path(sys.argv[2])):
    print('acquired', flush=True)
"""
    holder = _process(holder_code, side, path)
    processes = [holder]
    try:
        assert holder.stdout.readline().strip() == "held"
        waiter = _process(waiter_code, side, path)
        processes.append(waiter)
        assert waiter.stdout.readline().strip() == "attempting"
        with pytest.raises(subprocess.TimeoutExpired):
            waiter.wait(timeout=0.15)
        holder.stdin.write("release\n")
        holder.stdin.flush()
        _, errors = holder.communicate(timeout=5)
        assert holder.returncode == 0, errors
        output, errors = waiter.communicate(timeout=5)
        assert waiter.returncode == 0, errors
        assert output.strip() == "acquired"
    finally:
        _stop_processes(processes)
