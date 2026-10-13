# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real independent writers must preserve fields and their queued revisions."""

from __future__ import annotations

import json
import os
import subprocess
import sys

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


def test_gateway_cached_rename_preserves_worker_updates(tmp_path):
    """The Gateway fallback may retain its cache while the Worker keeps running."""
    env = {
        **os.environ,
        "JIUWENSWARM_HOME": str(tmp_path / "home"),
        "JIUWENSWARM_DATA_DIR": str(tmp_path / "data"),
    }
    gateway_code = """
import json, sys
from jiuwenswarm.gateway.embedded.runtime.session import session_metadata as sm
from jiuwenswarm.gateway.embedded.runtime.session.session_rename import apply_session_rename
sm.init_session_metadata(session_id='same-session', title='initial', mode='agent.work.normal', work_mode='work')
sm.update_session_metadata(session_id='same-session', accent_color='red', sync=True)
print('ready', flush=True)
sys.stdin.readline()
before=sm.get_session_metadata('same-session', enable_writeback=False)
result=apply_session_rename({'session_id':'same-session','title':'gateway renamed'}, 'same-session')
assert result[0], result
assert sm.flush_pending_writes(10)
after=sm.get_session_metadata('same-session', cache_bust=True, enable_writeback=False)
print(json.dumps({'before':before,'after':after}), flush=True)
"""
    gateway = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-c", gateway_code], env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
    )
    try:
        assert gateway.stdout.readline().strip() == "ready"
        worker_code = """
from jiuwenswarm.server.runtime.session import session_metadata as sm
for _ in range(32):
    sm.update_session_metadata(session_id='same-session', increment_message_count=True, accent_color='blue', sync=True)
assert sm.flush_pending_writes(10)
"""
        worker = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", worker_code], env=env,
            capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        assert worker.returncode == 0, worker.stderr
        out, err = gateway.communicate("continue\n", timeout=20)
        assert gateway.returncode == 0, err
        value = json.loads(out.strip().splitlines()[-1])
        assert value["before"]["message_count"] == 32
        assert value["before"]["accent_color"] == "blue"
        assert {key: value["after"][key] for key in ("message_count", "accent_color", "title")} == {
            "message_count": 32, "accent_color": "blue", "title": "gateway renamed",
        }
    finally:
        if gateway.poll() is None:
            gateway.kill()
        gateway.communicate()


@pytest.mark.parametrize("gateway_operation", ["pin", "rename"])
def test_gateway_and_worker_concurrent_updates_preserve_owned_fields(tmp_path, gateway_operation):
    """Real independent processes keep message counts during Gateway mutations."""
    env = {
        **os.environ,
        "JIUWENSWARM_HOME": str(tmp_path / "home"),
        "JIUWENSWARM_DATA_DIR": str(tmp_path / "data"),
    }
    child = """
import json, sys, time
role, operation = sys.argv[1:]
prefix = 'jiuwenswarm.gateway.embedded.runtime.session' if role=='gateway' else 'jiuwenswarm.server.runtime.session'
import importlib
sm=importlib.import_module(prefix+'.session_metadata')
if role=='gateway':
    sm.init_session_metadata(session_id='same-session', title='initial', mode='agent.work.normal', work_mode='work')
    sm.update_session_metadata(session_id='same-session', accent_color='red', sync=True)
print('ready', flush=True)
sys.stdin.readline()
started=time.perf_counter_ns()
for index in range(32):
    if role=='worker':
        sm.update_session_metadata(session_id='same-session', increment_message_count=True, accent_color='blue', sync=True)
    elif operation=='pin':
        assert sm.set_session_pinned('same-session', bool(index%2)) is not None
    else:
        sm.update_session_metadata(session_id='same-session', title='gateway-'+str(index), touch_last_message_at=False, sync=True)
    time.sleep(.002)
assert sm.flush_pending_writes(10)
finished=time.perf_counter_ns()
value=sm.get_session_metadata('same-session', cache_bust=True, enable_writeback=False)
print(json.dumps({'started_ns':started,'finished_ns':finished,'metadata':value}), flush=True)
"""
    processes = []
    try:
        for role in ("gateway", "worker"):
            process = subprocess.Popen(
                [sys.executable, "-X", "utf8", "-c", child, role, gateway_operation], env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8",
            )
            processes.append(process)
            assert process.stdout.readline().strip() == "ready"
        for process in processes:
            process.stdin.write("continue\n")
            process.stdin.flush()
        observations = []
        for process in processes:
            out, err = process.communicate(timeout=20)
            assert process.returncode == 0, err
            observations.append(json.loads(out.strip().splitlines()[-1]))
        overlap = min(row["finished_ns"] for row in observations) - max(
            row["started_ns"] for row in observations
        )
        assert overlap > 0, "metadata writers did not actually overlap"
        metadata = json.loads(
            (tmp_path / "data/agent/sessions/same-session/metadata.json").read_text(encoding="utf-8")
        )
        assert metadata["message_count"] == 32
        assert metadata["accent_color"] == "blue"
        if gateway_operation == "pin":
            assert metadata["pinned"] is True
            assert metadata["pin_order"] == 1
        else:
            assert metadata["title"] == "gateway-31"
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate()
