# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Worker PID identity must survive clock adjustments between processes.

psutil corrects ``create_time()`` with the boot time cached when the calling
process imported psutil. A long-lived Worker and a freshly started stop helper
therefore disagree after NTP or a manual clock change, which used to abort
``jiuwenswarm-stop`` with "Worker PID identity changed".
"""
from __future__ import annotations

import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import psutil
import pytest

from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.ipc.protocol import PROTOCOL_VERSION
from jiuwenswarm.server.ipc.startup import startup_acceptance
from jiuwenswarm.server.supervisor import identity, identity_probe
from jiuwenswarm.server.supervisor import runtime_supervisor as supervisor_module
from jiuwenswarm.server.supervisor.resident_lifecycle import worker_process
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor

PID = 987654
BOOT_TIME = 1_795_000_000.0
RAW_START = 186_846.56624


class _NativeProcess:
    """Mimic psutil's macOS/NetBSD backend for one observing process."""

    def __init__(self, raw_start: float, cached_boot_time: float, current_boot_time: float) -> None:
        self._raw_start = raw_start
        self._shift = cached_boot_time - current_boot_time

    def create_time(self, monotonic: bool = False) -> float:
        if monotonic or abs(self._shift) < 1:
            return BOOT_TIME + self._raw_start
        return BOOT_TIME + self._raw_start + self._shift


def _view(tmp_path, *, cached_boot_time: float, raw_start: float = RAW_START):
    """The same Worker as seen by a process that imported psutil at some point."""
    native = _NativeProcess(raw_start, cached_boot_time, BOOT_TIME)
    return SimpleNamespace(
        pid=PID,
        _proc=native,
        create_time=lambda: native.create_time(),
        username=lambda: "test-user",
        exe=lambda: sys.executable,
        cmdline=lambda: [sys.executable, "-m", "jiuwenswarm.server.worker.main", "--socket", str(tmp_path / "worker")],
        wait=lambda **kw: 0,
        kill=Mock(),
    )


def _hello_from(process) -> dict:
    return {
        "kind": "hello",
        "protocol": PROTOCOL_VERSION,
        "pid": PID,
        "process_created": process.create_time(),
        "process_started": identity.process_start_marker(process),
        "deployment_scope": identity.deployment_scope(),
        "data_scope": identity.data_scope(),
        "code_identity": "a" * 64,
        "stub": True,
    }


@pytest.fixture
def boot_time_platform(monkeypatch):
    monkeypatch.setattr(psutil, "LINUX", False)
    monkeypatch.setattr(psutil, "OSX", True)
    monkeypatch.setattr(psutil, "NETBSD", False)


def _patch_lookup(monkeypatch, process) -> None:
    def lookup(value=None):
        if value is None:
            return SimpleNamespace(username=lambda: "test-user", exe=lambda: sys.executable)
        return process

    monkeypatch.setattr(psutil, "Process", lookup)


def test_start_marker_ignores_the_observers_cached_boot_time(boot_time_platform, tmp_path):
    worker_view = _view(tmp_path, cached_boot_time=BOOT_TIME - 1.0)
    helper_view = _view(tmp_path, cached_boot_time=BOOT_TIME)

    assert worker_view.create_time() - helper_view.create_time() == pytest.approx(-1.0)
    assert identity.process_start_marker(worker_view) == identity.process_start_marker(helper_view)
    assert identity.hello_matches_process(_hello_from(worker_view), helper_view)


def test_start_marker_rejects_a_reused_pid(boot_time_platform, tmp_path):
    original = _view(tmp_path, cached_boot_time=BOOT_TIME)
    reused = _view(tmp_path, cached_boot_time=BOOT_TIME, raw_start=RAW_START + 0.01)

    assert not identity.hello_matches_process(_hello_from(original), reused)


@pytest.mark.parametrize(
    ("created", "accepted"),
    [(None, False), (BOOT_TIME + RAW_START, True),
     (BOOT_TIME + RAW_START - 1.0, False), (BOOT_TIME + RAW_START + 30.0, False),
     (True, False), ("1795186846.5", False)],
)
def test_legacy_hello_requires_comparable_birth_identity(boot_time_platform, tmp_path, created, accepted):
    hello = {"kind": "hello", "protocol": 2, "pid": PID, "process_created": created}

    assert identity.hello_matches_process(hello, _view(tmp_path, cached_boot_time=BOOT_TIME)) is accepted


def test_protocol_one_can_omit_birth_fields(boot_time_platform, tmp_path):
    process = _view(tmp_path, cached_boot_time=BOOT_TIME)
    assert identity.hello_matches_process({"protocol": 1}, process)


def test_optional_marker_cannot_replace_protocol_two_birth_fields(boot_time_platform, tmp_path):
    process = _view(tmp_path, cached_boot_time=BOOT_TIME)
    hello = {"protocol": 2, "process_started": identity.process_start_marker(process)}
    assert not identity.hello_matches_process(hello, process)


@pytest.mark.parametrize("failure", [psutil.AccessDenied(PID), PermissionError("restricted native API")])
def test_marker_permission_failure_keeps_tick_fallback(boot_time_platform, monkeypatch, failure):
    from jiuwenswarm.common import process_identity as common

    process = SimpleNamespace(
        pid=PID, create_time=Mock(side_effect=failure),
        _proc=SimpleNamespace(create_time=Mock(side_effect=failure)),
    )
    monkeypatch.setattr(psutil, "Process", lambda pid: process)
    monkeypatch.setattr(common, "proc_start_ticks", lambda pid: 4242)
    fields = common.process_identity_fields(PID)
    assert fields == {"process_created": None, "process_start_ticks": 4242, "process_started": None}
    assert common.validate_process_identity(PID, {"protocol": 2, **fields}, process=process) == "proc_start_ticks:4242"


def test_ticks_take_priority_over_a_different_optional_marker(boot_time_platform, monkeypatch, tmp_path):
    from jiuwenswarm.common import process_identity as common

    process = _view(tmp_path, cached_boot_time=BOOT_TIME)
    hello = _hello_from(process)
    hello.update(process_start_ticks=4242, process_started=hello["process_started"] + 1)
    monkeypatch.setattr(common, "proc_start_ticks", lambda pid: 4242)
    assert common.validate_process_identity(PID, hello, process=process) == "proc_start_ticks:4242"


def test_optional_marker_failure_keeps_comparable_creation_time(boot_time_platform, monkeypatch):
    from jiuwenswarm.common import process_identity as common

    process = SimpleNamespace(
        pid=PID, create_time=lambda: 1000.0,
        _proc=SimpleNamespace(create_time=Mock(side_effect=psutil.AccessDenied(PID))),
    )
    monkeypatch.setattr(common, "proc_start_ticks", lambda pid: None)
    hello = {"protocol": 2, "process_created": 1000.0, "process_started": 1000.0}
    assert common.validate_process_identity(PID, hello, process=process) == 1000.0


@pytest.mark.parametrize("operation", ["fields", "hello"])
def test_readable_optional_marker_does_not_replace_unavailable_birth_sources(
    boot_time_platform, monkeypatch, operation,
):
    from jiuwenswarm.common import process_identity as common

    process = SimpleNamespace(
        pid=PID, create_time=Mock(side_effect=psutil.AccessDenied(PID)),
        _proc=SimpleNamespace(create_time=lambda **kwargs: 1000.0),
    )
    monkeypatch.setattr(psutil, "Process", lambda pid: process)
    monkeypatch.setattr(common, "proc_start_ticks", lambda pid: None)
    with pytest.raises(psutil.AccessDenied, match="process identity unavailable"):
        if operation == "fields":
            common.process_identity_fields(PID)
        else:
            common.validate_process_identity(
                PID, {"protocol": 2, "process_created": 1000.0, "process_started": 1000.0}, process=process,
            )


@pytest.mark.parametrize("started", [True, "1", -1, float("nan"), float("inf")])
def test_invalid_marker_is_rejected_even_with_valid_ticks(boot_time_platform, monkeypatch, tmp_path, started):
    from jiuwenswarm.common import process_identity as common

    process = _view(tmp_path, cached_boot_time=BOOT_TIME)
    monkeypatch.setattr(common, "proc_start_ticks", lambda pid: 4242)
    hello = {"protocol": 2, "process_created": process.create_time(),
             "process_start_ticks": 4242, "process_started": started}
    with pytest.raises(RuntimeError, match="invalid process start marker"):
        common.validate_process_identity(PID, hello, process=process)


@pytest.mark.parametrize("started", [True, "1", BOOT_TIME + RAW_START + 1.0])
def test_invalid_or_mismatched_process_started_is_refused(boot_time_platform, tmp_path, started):
    process = _view(tmp_path, cached_boot_time=BOOT_TIME)
    hello = {"process_started": started, "process_created": process.create_time()}

    assert not identity.hello_matches_process(hello, process)


def test_platforms_without_clock_correction_use_create_time(monkeypatch):
    for name in ("LINUX", "OSX", "NETBSD"):
        monkeypatch.setattr(psutil, name, False)
    process = SimpleNamespace(_proc=Mock(side_effect=AssertionError), create_time=lambda: 12.5)

    assert identity.process_start_marker(process) == 12.5


def test_psutil_without_monotonic_keyword_falls_back(boot_time_platform):
    class OldNative:
        def create_time(self):
            return 1.0

    process = SimpleNamespace(_proc=OldNative(), create_time=lambda: 7.0)

    assert identity.process_start_marker(process) == 7.0


@pytest.mark.skipif(not (psutil.OSX or psutil.LINUX), reason="boot-relative start time is POSIX specific")
def test_real_process_marker_is_unaffected_by_clock_correction(monkeypatch):
    process = psutil.Process()
    created = process.create_time()
    marker = identity.process_start_marker(process)
    if psutil.OSX:
        from psutil import _psosx

        monkeypatch.setattr(_psosx, "INIT_BOOT_TIME", _psosx.boot_time() - 5.0)
    else:
        from psutil import _pslinux

        # psutil.boot_time() delegates to _pslinux.boot_time; capture the
        # original first so the patched function does not call itself.
        original_boot_time = _pslinux.boot_time
        monkeypatch.setattr(_pslinux, "boot_time", lambda: original_boot_time() + 5.0)
    shifted = psutil.Process()

    assert abs(shifted.create_time() - created) >= 4.0
    assert identity.process_start_marker(shifted) == marker


def test_stop_helper_accepts_worker_with_older_boot_time_cache(boot_time_platform, monkeypatch, tmp_path):
    hello = _hello_from(_view(tmp_path, cached_boot_time=BOOT_TIME - 1.0))
    helper_view = _view(tmp_path, cached_boot_time=BOOT_TIME)
    _patch_lookup(monkeypatch, helper_view)

    process, created = worker_process(hello, str(tmp_path / "worker"))

    assert process is helper_view and created == helper_view.create_time()
    assert hello["process_created"] != created


def test_stop_helper_still_refuses_a_reused_pid(boot_time_platform, monkeypatch, tmp_path):
    hello = _hello_from(_view(tmp_path, cached_boot_time=BOOT_TIME))
    _patch_lookup(monkeypatch, _view(tmp_path, cached_boot_time=BOOT_TIME, raw_start=RAW_START + 5.0))

    with pytest.raises(RuntimeError, match="Worker PID identity changed"):
        worker_process(hello, str(tmp_path / "worker"))


@pytest.mark.asyncio
async def test_front_attach_accepts_worker_with_older_boot_time_cache(boot_time_platform, monkeypatch, tmp_path):
    hello = _hello_from(_view(tmp_path, cached_boot_time=BOOT_TIME - 1.0))
    front_view = _view(tmp_path, cached_boot_time=BOOT_TIME)
    peer = SimpleNamespace(recv=AsyncMock(return_value=hello), send=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(identity_probe, "probe_identity", AsyncMock(return_value=("a" * 64, {})))
    monkeypatch.setattr(supervisor_module, "try_connect", AsyncMock(return_value=peer))
    monkeypatch.setattr(supervisor_module, "endpoint_is_listening", lambda _path: True)
    _patch_lookup(monkeypatch, front_view)
    front = RuntimeSupervisor(Readiness(), socket_path=str(tmp_path / "worker"), stub=True)
    monkeypatch.setattr(front, "_start_side_tasks", Mock())

    await front._bring_up()

    assert front._worker_pid == PID
    assert front._worker_create_time == front_view.create_time()
    peer.send.assert_awaited_once_with(startup_acceptance(hello))
    front._reap_validated_worker(deadline=time.monotonic() + .1)
    front_view.kill.assert_not_called()
