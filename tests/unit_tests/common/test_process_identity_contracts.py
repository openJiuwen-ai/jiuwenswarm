# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Identity permission failures cannot authorize signalling or imply exit."""

import json
import logging
import time
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest

from jiuwenswarm import debug_launcher
from jiuwenswarm.common import process_identity as identity
from jiuwenswarm.server.jiuwenmate import dingtalk_cli_local_provider as dingtalk
from jiuwenswarm.server.runtime.session import lifecycle


@pytest.fixture
def restricted_process(monkeypatch):
    process = SimpleNamespace(
        pid=123, create_time=Mock(side_effect=psutil.AccessDenied(123)),
        is_running=lambda: True, status=lambda: psutil.STATUS_RUNNING,
        terminate=Mock(), kill=Mock(), wait=Mock(),
    )
    monkeypatch.setattr(psutil, "Process", lambda pid=None: process)
    monkeypatch.setattr(identity, "proc_start_ticks", lambda pid: 4242)
    return process


@pytest.mark.parametrize("protocol", [1, 2])
def test_hello_birth_fields_are_required_only_for_protocol_2(restricted_process, protocol):
    if protocol == 1:
        assert identity.validate_process_identity(123, {"protocol": protocol}) == "proc_start_ticks:4242"
    else:
        with pytest.raises(RuntimeError, match="protocol 2 HELLO requires"):
            identity.validate_process_identity(123, {"protocol": protocol})


@pytest.mark.parametrize("created", [True, "123", 0, -1, float("nan"), float("inf")])
def test_hello_rejects_invalid_creation_time_even_with_ticks(restricted_process, created):
    with pytest.raises(RuntimeError, match="invalid process creation time"):
        identity.validate_process_identity(123, {
            "protocol": 2, "process_created": created, "process_start_ticks": 4242,
        })


def test_debug_state_roundtrip_preserves_tick_identity():
    state = debug_launcher.DebugState(123, time.time(), "log", ["python"], ".", "proc_start_ticks:4242")
    assert debug_launcher.DebugState.from_dict(state.to_dict()) == state


@pytest.mark.parametrize("ticks, expected", [(4242, True), (4243, False), (None, None)])
def test_debug_liveness_distinguishes_matching_reused_and_unknown_identity(
    monkeypatch, restricted_process, ticks, expected,
):
    state = debug_launcher.DebugState(123, time.time(), "log", ["python"], ".", "proc_start_ticks:4242")
    monkeypatch.setattr(debug_launcher, "is_process_alive", lambda pid: True)
    monkeypatch.setattr(identity, "proc_start_ticks", lambda pid: ticks)
    assert debug_launcher.is_debug_service_alive(state) is expected


def test_debug_stop_keeps_unverified_state_and_does_not_signal(monkeypatch, restricted_process):
    state = debug_launcher.DebugState(123, 100.0, "log", ["python"], ".")
    tree, worker, clear = Mock(), Mock(), Mock()
    monkeypatch.setattr(debug_launcher, "read_debug_state", lambda: state)
    monkeypatch.setattr(debug_launcher, "is_process_alive", lambda pid: True)
    monkeypatch.setattr(debug_launcher, "_stop_process_tree", tree)
    monkeypatch.setattr(debug_launcher, "_stop_resident_worker", worker)
    monkeypatch.setattr(debug_launcher, "clear_debug_state", clear)
    assert debug_launcher.stop_debug_service(timeout=0) == 1
    tree.assert_not_called()
    worker.assert_not_called()
    clear.assert_not_called()


@pytest.mark.parametrize("recorded_identity", [None, "proc_start_ticks:4242"])
@pytest.mark.parametrize("worker_stopped", [False, True])
def test_debug_selected_worker_cleanup_retains_unknown_front(
    monkeypatch, tmp_path, restricted_process, recorded_identity, worker_stopped,
):
    monkeypatch.setattr(debug_launcher, "REPO_ROOT", tmp_path)
    state = debug_launcher.DebugState(123, 100.0, "log", ["python"], ".", recorded_identity)
    debug_launcher.write_debug_state(state)
    state_path = debug_launcher.get_debug_state_path()
    original = state_path.read_bytes()
    tree, worker = Mock(), Mock(return_value=worker_stopped)
    monkeypatch.setattr(debug_launcher, "is_process_alive", lambda pid: True)
    monkeypatch.setattr(debug_launcher, "_stop_process_tree", tree)
    monkeypatch.setattr(debug_launcher, "_stop_resident_worker", worker)
    if recorded_identity is not None:
        monkeypatch.setattr(identity, "proc_start_ticks", lambda pid: None)

    assert debug_launcher.stop_debug_service(timeout=0, legacy_worker_pid=7777) == 1
    worker.assert_called_once_with(timeout=0, legacy_worker_pid=7777)
    tree.assert_not_called()
    assert state_path.read_bytes() == original
    restricted_process.terminate.assert_not_called()
    restricted_process.kill.assert_not_called()


@pytest.mark.parametrize("latest_ticks", [4243, None])
def test_debug_group_rechecks_identity_before_forced_kill(
    monkeypatch, restricted_process, caplog, latest_ticks,
):
    import signal

    ticks = iter([4242, latest_ticks])
    killpg = Mock()
    monkeypatch.setattr(identity, "proc_start_ticks", lambda pid: next(ticks))
    monkeypatch.setattr(debug_launcher.platform, "system", lambda: "Linux")
    monkeypatch.setattr(debug_launcher.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(debug_launcher.os, "killpg", killpg, raising=False)
    monkeypatch.setattr(debug_launcher, "_process_group_alive", lambda pid: True)
    caplog.set_level(logging.INFO)
    assert not debug_launcher._stop_process_tree(123, timeout=0, expected_created="proc_start_ticks:4242")
    killpg.assert_called_once_with(123, signal.SIGTERM)
    assert "before forced termination pid=123" in caplog.text
    assert "sending SIGKILL" not in caplog.text


@pytest.mark.parametrize("ticks, expected", [(4242, True), (4243, False), (None, None)])
def test_oauth_liveness_distinguishes_matching_reused_and_unknown_identity(
    monkeypatch, tmp_path, restricted_process, ticks, expected,
):
    pid_file = tmp_path / "auth.pid"
    pid_file.write_text(json.dumps({"pid": 123, "process_identity": "proc_start_ticks:4242"}), encoding="utf-8")
    monkeypatch.setattr(identity, "proc_start_ticks", lambda pid: ticks)
    assert dingtalk.LocalDingTalkCliRuntime._oauth_process_running(pid_file) is expected


def test_oauth_cancel_rejects_unverifiable_legacy_identity(tmp_path, restricted_process):
    pid_file = tmp_path / "auth.pid"
    pid_file.write_text("123", encoding="utf-8")
    runtime = object.__new__(dingtalk.LocalDingTalkCliRuntime)
    assert runtime._oauth_process_running(pid_file) is None
    with pytest.raises(dingtalk.PersistentAgentError, match="Failed to stop"):
        runtime._stop_oauth_process(pid_file)
    assert pid_file.exists()
    restricted_process.terminate.assert_not_called()
    restricted_process.kill.assert_not_called()


def test_oauth_stop_rechecks_identity_before_forced_kill(monkeypatch, tmp_path, restricted_process):
    pid_file = tmp_path / "auth.pid"
    pid_file.write_text(json.dumps({"pid": 123, "process_identity": "proc_start_ticks:4242"}), encoding="utf-8")

    def replaced_during_wait(timeout):
        monkeypatch.setattr(identity, "proc_start_ticks", lambda pid: 4243)
        raise psutil.TimeoutExpired(timeout)

    restricted_process.wait.side_effect = replaced_during_wait
    runtime = object.__new__(dingtalk.LocalDingTalkCliRuntime)
    runtime._stop_oauth_process(pid_file)
    restricted_process.terminate.assert_called_once()
    restricted_process.kill.assert_not_called()


@pytest.mark.parametrize("error", [psutil.AccessDenied(123), PermissionError("/proc/stat")])
def test_runtime_owner_record_handles_both_error_families(monkeypatch, restricted_process, error):
    restricted_process.create_time.side_effect = error
    monkeypatch.setattr(lifecycle, "_proc_start_ticks", lambda pid: 4242)
    assert lifecycle._runtime_owner_record(123) == {"pid": 123, "start_ticks": 4242}


@pytest.mark.parametrize("ticks", [4242, None])
def test_runtime_owner_permission_error_uses_ticks_or_controlled_failure(
    monkeypatch, restricted_process, ticks,
):
    restricted_process.create_time.side_effect = PermissionError("/proc/stat")
    monkeypatch.setattr(lifecycle, "_proc_start_ticks", lambda pid: ticks)
    owner = {"pid": 123, "started_at": 100.0}
    if ticks is not None:
        owner["start_ticks"] = ticks
        assert lifecycle.runtime_owner_alive(owner)
    else:
        with pytest.raises(lifecycle.LifecycleError, match="cannot verify"):
            lifecycle.runtime_owner_alive(owner)
