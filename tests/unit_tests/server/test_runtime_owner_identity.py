"""Regression tests for PID-reuse-safe runtime ownership."""

from __future__ import annotations

from types import SimpleNamespace

import psutil
import pytest

from jiuwenswarm.server.runtime.session import lifecycle as lc


@pytest.mark.parametrize("status", [psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD])
@pytest.mark.parametrize("identity", [{"started_at": 100.0}, {"start_ticks": 4242}])
def test_exited_owner_with_matching_identity_does_not_block_reclaim(monkeypatch, status, identity):
    process = SimpleNamespace(create_time=lambda: 100.0, status=lambda: status, num_threads=lambda: 1)
    monkeypatch.setattr(psutil, "Process", lambda _pid: process)
    monkeypatch.setattr(lc, "_proc_start_ticks", lambda _pid: 4242)

    assert not lc.runtime_owner_alive({"pid": 123, **identity})


@pytest.mark.parametrize("status", [psutil.STATUS_RUNNING, psutil.STATUS_SLEEPING, psutil.STATUS_STOPPED])
def test_live_owner_with_matching_identity_remains_protected(monkeypatch, status):
    process = SimpleNamespace(create_time=lambda: 100.0, status=lambda: status)
    monkeypatch.setattr(psutil, "Process", lambda _pid: process)
    monkeypatch.setattr(lc, "_proc_start_ticks", lambda _pid: 4242)

    assert lc.runtime_owner_alive({"pid": 123, "started_at": 100.0, "start_ticks": 4242})


def test_owner_status_access_denied_fails_closed(monkeypatch):
    def denied():
        raise psutil.AccessDenied(pid=123)

    process = SimpleNamespace(create_time=lambda: 100.0, status=denied)
    monkeypatch.setattr(psutil, "Process", lambda _pid: process)
    monkeypatch.setattr(lc, "_proc_start_ticks", lambda _pid: 4242)
    with pytest.raises(lc.LifecycleError, match="cannot verify the previous runtime owner"):
        lc.runtime_owner_alive({"pid": 123, "start_ticks": 4242})


@pytest.mark.parametrize("threads", [2, 4])
def test_zombie_leader_with_other_threads_still_protects_ownership(monkeypatch, threads):
    owner = {"pid": lc.os.getpid() + 1, "start_ticks": 4242}
    process = SimpleNamespace(status=lambda: psutil.STATUS_ZOMBIE, num_threads=lambda: threads)
    monkeypatch.setattr(psutil, "Process", lambda _pid: process)
    monkeypatch.setattr(lc, "_proc_start_ticks", lambda _pid: 4242)
    monkeypatch.setattr(lc, "state", lambda *_args: {"runtime_owner": owner})
    with pytest.raises(lc.LifecycleError, match="another live AgentServer"):
        lc.assert_runtime_owner("session-1")


def test_zombie_owner_with_unreadable_thread_count_fails_closed(monkeypatch):
    def denied():
        raise psutil.AccessDenied(pid=123)

    process = SimpleNamespace(status=lambda: psutil.STATUS_ZOMBIE, num_threads=denied)
    monkeypatch.setattr(psutil, "Process", lambda _pid: process)
    monkeypatch.setattr(lc, "_proc_start_ticks", lambda _pid: 4242)
    with pytest.raises(lc.LifecycleError, match="cannot verify the previous runtime owner"):
        lc.runtime_owner_alive({"pid": 123, "start_ticks": 4242})


def _proc_stat_line(start_ticks: int) -> str:
    fields = ["S", *("0" for _ in range(18)), str(start_ticks)]
    return f"123 (python3) {' '.join(fields)}\n"


def test_proc_start_ticks_reads_field_22(tmp_path) -> None:
    stat_dir = tmp_path / "123"
    stat_dir.mkdir()
    (stat_dir / "stat").write_text(_proc_stat_line(4242), encoding="utf-8")

    assert lc._proc_start_ticks(123, proc_root=tmp_path) == 4242


def test_runtime_owner_record_falls_back_to_proc_start_ticks(monkeypatch) -> None:
    class _DeniedProcess:
        def create_time(self) -> float:
            raise psutil.AccessDenied(pid=123, name="create_time")

    monkeypatch.setattr(psutil, "Process", lambda _pid: _DeniedProcess())
    monkeypatch.setattr(lc, "_proc_start_ticks", lambda _pid: 4242)

    assert lc._runtime_owner_record(123) == {"pid": 123, "start_ticks": 4242}


def test_assert_runtime_owner_compares_proc_start_ticks(monkeypatch) -> None:
    monkeypatch.setattr(
        lc,
        "state",
        lambda _kind, _resource_id: {
            "runtime_owner": {"pid": 123, "start_ticks": 4242}
        },
    )
    monkeypatch.setattr(lc.os, "getpid", lambda: 456)
    monkeypatch.setattr(psutil, "Process", lambda _pid: SimpleNamespace(status=lambda: psutil.STATUS_SLEEPING))
    monkeypatch.setattr(lc, "_proc_start_ticks", lambda _pid: 4242)

    with pytest.raises(lc.LifecycleError, match="another live AgentServer"):
        lc.assert_runtime_owner("session-1")


def test_assert_runtime_owner_allows_different_proc_start_ticks(monkeypatch) -> None:
    monkeypatch.setattr(
        lc,
        "state",
        lambda _kind, _resource_id: {
            "runtime_owner": {"pid": 123, "start_ticks": 4242}
        },
    )
    monkeypatch.setattr(lc.os, "getpid", lambda: 456)
    monkeypatch.setattr(psutil, "Process", lambda _pid: SimpleNamespace(status=lambda: psutil.STATUS_SLEEPING))
    monkeypatch.setattr(lc, "_proc_start_ticks", lambda _pid: 9898)

    lc.assert_runtime_owner("session-1")
