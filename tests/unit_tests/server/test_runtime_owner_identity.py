"""Regression tests for PID-reuse-safe runtime ownership."""

from __future__ import annotations

import psutil
import pytest

from jiuwenswarm.server.runtime.session import lifecycle as lc


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


def test_runtime_owner_alive_rejects_stale_started_at_when_ticks_match(
    monkeypatch,
) -> None:
    class _Process:
        def create_time(self) -> float:
            return 100.0

    monkeypatch.setattr(psutil, "Process", lambda _pid: _Process())
    monkeypatch.setattr(lc, "_proc_start_ticks", lambda _pid: 4242)

    owner = {"pid": 123, "started_at": 100.0, "start_ticks": 4242}
    assert lc.runtime_owner_alive(owner)
    assert not lc.runtime_owner_alive({**owner, "started_at": 99.0})


def test_runtime_owner_alive_uses_ticks_when_started_at_is_unreadable(
    monkeypatch,
) -> None:
    class _DeniedProcess:
        def create_time(self) -> float:
            raise psutil.AccessDenied(pid=123, name="create_time")

    monkeypatch.setattr(psutil, "Process", lambda _pid: _DeniedProcess())
    monkeypatch.setattr(lc, "_proc_start_ticks", lambda _pid: 4242)

    assert lc.runtime_owner_alive(
        {"pid": 123, "started_at": 100.0, "start_ticks": 4242}
    )


def test_assert_runtime_owner_compares_proc_start_ticks(monkeypatch) -> None:
    monkeypatch.setattr(
        lc,
        "state",
        lambda _kind, _resource_id: {
            "runtime_owner": {"pid": 123, "start_ticks": 4242}
        },
    )
    monkeypatch.setattr(lc.os, "getpid", lambda: 456)
    monkeypatch.setattr(psutil, "Process", lambda _pid: object())
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
    monkeypatch.setattr(psutil, "Process", lambda _pid: object())
    monkeypatch.setattr(lc, "_proc_start_ticks", lambda _pid: 9898)

    lc.assert_runtime_owner("session-1")


def test_assert_runtime_owner_fails_closed_when_proc_start_ticks_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        lc,
        "state",
        lambda _kind, _resource_id: {
            "runtime_owner": {"pid": 123, "start_ticks": 4242}
        },
    )
    monkeypatch.setattr(lc.os, "getpid", lambda: 456)
    monkeypatch.setattr(psutil, "Process", lambda _pid: object())
    monkeypatch.setattr(lc, "_proc_start_ticks", lambda _pid: None)

    with pytest.raises(lc.LifecycleError, match="cannot verify the previous runtime owner"):
        lc.assert_runtime_owner("session-1")
