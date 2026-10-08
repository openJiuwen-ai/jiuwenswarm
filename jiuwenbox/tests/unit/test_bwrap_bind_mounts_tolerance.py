# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Unit tests for cross-distro bind_mounts fault tolerance.

Pins the fix for the bug where a ``bind_mounts`` entry whose ``host_path``
does not exist on the host (e.g. ``/lib64`` on Debian-style arm64 layouts
when the default policy was derived from an x86_64 layout) caused bwrap to
fail outright with ``Can't find source path ...`` and drag the whole sandbox
down (``state=error`` → subsequent ops 409 Conflict).

``_apply_filesystem`` now skips missing ``host_path`` entries with a warning,
mirroring the existing fault-tolerance philosophy already in
``_apply_bind_root_entries``. These tests exercise the policy→config path
(:meth:`BwrapConfig._apply_filesystem`) directly so they run on any host
without a sandbox runtime.

Edge cases covered beyond the basic "path absent" scenario:

* **Broken symlink** — ``exists()`` follows links and returns ``False`` for a
  dangling symlink; ``is_symlink()`` (lstat) distinguishes it from a truly
  absent path so the warning message is more precise. Both are still skipped
  because bwrap cannot resolve the source either way.
* **Stat error (EACCES)** — ``Path.exists()`` only swallows a limited set of
  errno values; ``EACCES`` propagates as ``OSError``. The guard catches it so
  the tolerance check itself does not crash the sandbox startup.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from jiuwenbox.models.policy import BindMount, FilesystemPolicy
from jiuwenbox.models.policy import _expand_path
from jiuwenbox.supervisor.bwrap import BwrapConfig

_LOGGER_NAME = "jiuwenbox.supervisor.bwrap"


def _apply(bind_mounts: list[BindMount]) -> BwrapConfig:
    cfg = BwrapConfig(command=["/bin/sh"])
    fs = FilesystemPolicy(bind_mounts=bind_mounts)
    BwrapConfig._apply_filesystem(cfg, fs)
    return cfg


def _warned_about(caplog, missing: str) -> bool:
    """True if caplog saw a ``does not exist`` warning naming ``missing``.

    bwrap formats ``host_path`` with ``%r`` (repr), which escapes backslashes
    on Windows -- so match the path's basename (no separators to escape)
    rather than the raw path string.
    """
    token = os.path.basename(missing)
    return any(
        "does not exist" in rec.message and token in rec.message
        for rec in caplog.records
    )


def test_missing_ro_host_path_is_skipped(tmp_path, caplog):
    missing = str(tmp_path / "does-not-exist-ro")
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _apply(
            [BindMount(host_path=missing, sandbox_path="/lib64", mode="ro")]
        )

    assert cfg.ro_binds == []
    assert cfg.rw_binds == []
    assert _warned_about(caplog, missing)


def test_missing_rw_host_path_is_skipped(tmp_path, caplog):
    missing = str(tmp_path / "does-not-exist-rw")
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _apply([BindMount(host_path=missing, sandbox_path="/data", mode="rw")])

    assert cfg.rw_binds == []
    assert cfg.ro_binds == []
    assert _warned_about(caplog, missing)


def test_present_host_path_still_bound(tmp_path):
    present = tmp_path / "lib64"
    present.mkdir()
    sandbox = "/lib64"
    cfg = _apply(
        [BindMount(host_path=str(present), sandbox_path=sandbox, mode="ro")]
    )

    assert cfg.ro_binds == [(str(present), _expand_path(sandbox))]
    assert cfg.rw_binds == []


def test_missing_entries_skipped_but_present_entries_kept(tmp_path, caplog):
    """A single missing entry must not drag down the rest of the list."""
    present_ro = tmp_path / "bin"
    present_ro.mkdir()
    present_rw = tmp_path / "work"
    present_rw.mkdir()
    missing = str(tmp_path / "no-such-lib")

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _apply(
            [
                BindMount(host_path=str(present_ro), sandbox_path="/bin", mode="ro"),
                BindMount(host_path=missing, sandbox_path="/lib64", mode="ro"),
                BindMount(host_path=str(present_rw), sandbox_path="/work", mode="rw"),
            ]
        )

    assert cfg.ro_binds == [(str(present_ro), _expand_path("/bin"))]
    assert cfg.rw_binds == [(str(present_rw), _expand_path("/work"))]
    assert _warned_about(caplog, missing)


def test_warning_emitted_at_warning_level(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    missing = str(tmp_path / "absent")

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        _apply([BindMount(host_path=missing, sandbox_path="/lib64", mode="ro")])

    assert any(
        rec.levelno == logging.WARNING and rec.name == _LOGGER_NAME
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# Edge case: broken (dangling) symlink
# ---------------------------------------------------------------------------


def test_broken_symlink_host_path_is_skipped(tmp_path, caplog):
    """A dangling symlink must be skipped — bwrap cannot resolve it either.

    The warning message should say "broken symlink" (not "does not exist")
    so operators can distinguish a misconfigured symlink from a genuinely
    absent path. Symlink creation may be unavailable on some platforms
    (Windows without Developer Mode / admin), so the test skips gracefully.
    """
    link = tmp_path / "dangling-link"
    try:
        link.symlink_to(tmp_path / "nonexistent-target")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _apply(
            [BindMount(host_path=str(link), sandbox_path="/lib64", mode="ro")]
        )

    assert cfg.ro_binds == []
    assert cfg.rw_binds == []
    assert any(
        "broken symlink" in rec.message for rec in caplog.records
    )


def test_broken_symlink_does_not_mask_other_entries(tmp_path, caplog):
    """A broken symlink in the list must not prevent present entries from binding."""
    present = tmp_path / "bin"
    present.mkdir()
    link = tmp_path / "dangling"
    try:
        link.symlink_to(tmp_path / "no-such-target")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _apply(
            [
                BindMount(host_path=str(present), sandbox_path="/bin", mode="ro"),
                BindMount(host_path=str(link), sandbox_path="/lib64", mode="ro"),
            ]
        )

    assert cfg.ro_binds == [(str(present), _expand_path("/bin"))]
    assert cfg.rw_binds == []
    assert any(
        "broken symlink" in rec.message for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# Edge case: stat error (EACCES or other OSError)
# ---------------------------------------------------------------------------


def test_stat_error_does_not_crash(tmp_path, caplog, monkeypatch):
    """If ``Path.exists()`` raises ``OSError`` (e.g. EACCES), the guard must
    catch it and skip the entry — not let the exception propagate and crash
    the sandbox startup the way it was supposed to prevent.
    """
    host = str(tmp_path / "restricted")
    _original_exists = Path.exists

    def _raise_on_target(self, *args, **kwargs):
        if str(self) == host:
            raise PermissionError(13, "Permission denied (simulated)")
        return _original_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", _raise_on_target)

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _apply(
            [BindMount(host_path=host, sandbox_path="/lib64", mode="ro")]
        )

    assert cfg.ro_binds == []
    assert cfg.rw_binds == []
    assert any(
        "cannot stat" in rec.message for rec in caplog.records
    )
