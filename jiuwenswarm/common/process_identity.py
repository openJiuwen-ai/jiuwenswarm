# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""PID-reuse-safe identities on systems that restrict global process records."""

from __future__ import annotations

import math
from pathlib import Path
from typing import TypeAlias

import psutil

ProcessIdentity: TypeAlias = float | str
_TICKS_PREFIX = "proc_start_ticks:"


def proc_start_ticks(pid: int, *, proc_root: Path = Path("/proc")) -> int | None:
    """Read field 22 without the global ``/proc/stat`` boot-time record."""
    try:
        text = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
        closing_paren = text.rfind(")")
        if closing_paren < 0:
            return None
        # comm can contain spaces and parentheses. Field 3 follows its last ')'.
        fields = text[closing_paren + 2:].split()
        ticks = int(fields[19])
        return ticks if ticks > 0 else None
    except (OSError, IndexError, ValueError):
        return None


def read_process_identity(pid: int, *, process: psutil.Process | None = None) -> ProcessIdentity:
    """Prefer creation time, falling back to an explicitly tagged kernel counter.

    Raises:
        psutil.NoSuchProcess: The process has exited.
        psutil.AccessDenied: Neither identity source can be read.
    """
    try:
        observed = process if process is not None else psutil.Process(pid)
        return observed.create_time()
    except psutil.NoSuchProcess:
        raise
    except (psutil.Error, OSError) as exc:
        ticks = proc_start_ticks(pid)
        if ticks is not None:
            return f"{_TICKS_PREFIX}{ticks}"
        raise psutil.AccessDenied(
            pid=pid,
            msg=f"process identity unavailable: create_time failed ({exc}); "
                f"/proc/{pid}/stat start ticks unavailable",
        ) from exc


def process_identity_matches(
    pid: int,
    expected: ProcessIdentity,
    *,
    process: psutil.Process | None = None,
    tolerance: float = 0.0,
) -> bool:
    """Compare the same identity source, never timestamps against kernel ticks."""
    if isinstance(expected, str):
        ticks = proc_start_ticks(pid)
        if ticks is None:
            # Distinguish an exited process from an unobservable live process.
            psutil.Process(pid)
            raise psutil.AccessDenied(pid=pid, msg=f"cannot verify process identity: /proc/{pid}/stat unavailable")
        return expected == f"{_TICKS_PREFIX}{ticks}"
    if isinstance(expected, bool) or not isinstance(expected, (int, float)) or not math.isfinite(expected):
        return False
    observed = read_process_identity(pid, process=process)
    if isinstance(observed, str):
        raise psutil.AccessDenied(pid=pid, msg="cannot verify a legacy creation time using proc start ticks")
    return observed == expected if tolerance == 0 else abs(observed - expected) < tolerance


def process_start_marker(process: psutil.Process) -> float | None:
    """Read psutil's native start marker, or None when access is restricted.

    Compare this source only with ``process_started``; do not convert or copy
    it into the creation timestamp or integer kernel-counter fields.
    """
    try:
        if psutil.LINUX or psutil.OSX or psutil.NETBSD:
            native = getattr(process, "_proc", None)
            if callable(getattr(native, "create_time", None)):
                try:
                    return float(native.create_time(monotonic=True))
                except TypeError:
                    pass  # Older psutil does not accept the monotonic keyword.
        return float(process.create_time())
    except (psutil.Error, OSError, NotImplementedError):
        return None


def process_identity_fields(pid: int) -> dict[str, float | int | None]:
    """Build additive HELLO fields, retaining the legacy timestamp's units."""
    identity = read_process_identity(pid)
    try:
        started = process_start_marker(psutil.Process(pid))
    except (psutil.Error, OSError):
        started = None
    if isinstance(identity, str):
        return {"process_created": None, "process_start_ticks": int(identity[len(_TICKS_PREFIX):]),
                "process_started": started}
    # A peer may have stricter permissions than this process.
    return {"process_created": identity, "process_start_ticks": proc_start_ticks(pid),
            "process_started": started}


def validate_process_identity(
    pid: int, hello: dict, *, process: psutil.Process | None = None,
) -> ProcessIdentity:
    """Validate HELLO identity fields and retain the identity actually checked."""
    identity = read_process_identity(pid, process=process)
    created, ticks = hello.get("process_created"), hello.get("process_start_ticks")
    started = hello.get("process_started")
    if hello.get("protocol") == 2 and created is None and ticks is None:
        raise RuntimeError("Worker protocol 2 HELLO requires a process identity")
    if created is not None:
        invalid_type = isinstance(created, bool) or not isinstance(created, (int, float))
        if invalid_type:
            raise RuntimeError("Worker returned an invalid process creation time")
        if not math.isfinite(created) or created <= 0:
            raise RuntimeError("Worker returned an invalid process creation time")
    if started is not None:
        invalid_type = isinstance(started, bool) or not isinstance(started, (int, float))
        if invalid_type:
            raise RuntimeError("Worker returned an invalid process start marker")
        if not math.isfinite(started) or started < 0:
            raise RuntimeError("Worker returned an invalid process start marker")
    if ticks is not None:
        if type(ticks) is not int or ticks <= 0:
            raise RuntimeError("Worker returned invalid process start ticks")
        tick_identity = f"{_TICKS_PREFIX}{ticks}"
        try:
            matches = process_identity_matches(pid, tick_identity, process=process)
        except psutil.AccessDenied:
            if created is None or isinstance(identity, str):
                raise
            # Both peers still have comparable timestamps.
        else:
            if not matches:
                raise RuntimeError("Worker PID identity changed")
            # Retain the stable counter even if create_time is readable now.
            return tick_identity
    if started is not None:
        observed_process = process if process is not None else psutil.Process(pid)
        observed_marker = process_start_marker(observed_process)
        if observed_marker is None:
            if created is None or isinstance(identity, str):
                raise psutil.AccessDenied(pid=pid, msg="cannot verify the Worker process start marker")
            # The optional API must not block a verifiable creation timestamp.
        else:
            if started != observed_marker:
                raise RuntimeError("Worker PID identity changed")
            # Retain this observer's OS birth identity for lifecycle checks.
            return identity
    if created is not None and not process_identity_matches(pid, created, process=process):
        raise RuntimeError("Worker PID identity changed")
    # Protocol 1 Workers omitted birth fields; retain their OS-validated identity.
    return identity
